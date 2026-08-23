"""Hash-verified, idempotent device asset deployment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .artifact_store import atomic_write_json, sha256_file
from .transport import Transport, TransportError


class DeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class AssetSpec:
    local: Path
    remote: PurePosixPath
    executable: bool = False
    required: bool = True
    kind: str = "asset"


class DeploymentManager:
    def __init__(self, transport: Transport):
        self.transport = transport

    def deploy(
        self,
        assets: Iterable[AssetSpec],
        *,
        force: bool = False,
        verify_hashes: bool = True,
        manifest_path: Path | None = None,
        clean_stale: bool = False,
        previous_manifest: dict[str, Any] | None = None,
        allowed_remote_root: PurePosixPath | None = None,
    ) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        for asset in assets:
            local = asset.local.expanduser().resolve(strict=False)
            if not local.exists() or not local.is_file():
                if asset.required:
                    raise DeploymentError(f"required local asset missing: {local}")
                records.append(self._record(asset, local, action="missing-optional", verified=False))
                continue
            local_hash = sha256_file(local)
            remote_hash: str | None
            try:
                remote_hash = self.transport.sha256(asset.remote)
            except TransportError:
                remote_hash = None
            if not force and remote_hash == local_hash:
                action = "unchanged"
            else:
                self.transport.mkdir(asset.remote.parent)
                transfer = self.transport.push(local, asset.remote)
                if not transfer.success:
                    raise DeploymentError(f"push failed for {local} -> {asset.remote}: {transfer.message}")
                action = "pushed"
            if asset.executable:
                self.transport.chmod(asset.remote, "0755")
            verified_hash: str | None = None
            verified = False
            if verify_hashes:
                try:
                    verified_hash = self.transport.sha256(asset.remote)
                except TransportError as exc:
                    raise DeploymentError(str(exc)) from exc
                verified = verified_hash == local_hash
                if not verified:
                    raise DeploymentError(
                        f"remote hash mismatch for {asset.remote}: local={local_hash} remote={verified_hash}"
                    )
            record = self._record(asset, local, action=action, verified=verified)
            record.update(
                {
                    "size": local.stat().st_size,
                    "local_sha256": local_hash,
                    "remote_sha256": verified_hash or remote_hash,
                    "mode": "0755" if asset.executable else None,
                }
            )
            records.append(record)
        removed: list[str] = []
        if clean_stale:
            if allowed_remote_root is None:
                raise DeploymentError("clean_stale requires an allowed_remote_root")
            active = {record["remote"] for record in records if record["action"] != "missing-optional"}
            for old_record in (previous_manifest or {}).get("assets", []):
                remote_value = old_record.get("remote") if isinstance(old_record, dict) else None
                if not isinstance(remote_value, str) or remote_value in active:
                    continue
                remote = PurePosixPath(remote_value)
                if not remote.is_absolute() or not remote.is_relative_to(allowed_remote_root) or remote == allowed_remote_root:
                    raise DeploymentError(f"refusing to remove stale path outside managed root: {remote}")
                result = self.transport.invoke(("rm", "-f", "--", str(remote)))
                if not result.success:
                    raise DeploymentError(f"failed to remove stale asset {remote}: {result.stderr or result.stdout}")
                removed.append(str(remote))
        manifest = {
            "schema_version": 1,
            "producer": {"name": "vmin_judge", "component": "DeploymentManager"},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "transport": self.transport.name,
            "assets": records,
            "complete": all(record["action"] != "missing-optional" or not record["required"] for record in records),
            "verified": verify_hashes and all(record.get("verified", False) for record in records if record["action"] != "missing-optional"),
            "removed_stale": removed,
        }
        if manifest_path is not None:
            atomic_write_json(manifest_path, manifest)
        return manifest

    @staticmethod
    def _record(
        asset: AssetSpec,
        local: Path,
        *,
        action: str,
        verified: bool,
    ) -> dict[str, Any]:
        return {
            "kind": asset.kind,
            "local": str(local),
            "remote": str(asset.remote),
            "required": asset.required,
            "executable": asset.executable,
            "action": action,
            "verified": verified,
        }

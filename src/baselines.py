"""Immutable approved baseline documents and mutable lifecycle registry metadata."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .artifact_store import atomic_write_json, sha256_file
from .config_loader import document_sha256, require_schema_version


BASELINE_STATES = {"draft", "approved", "deprecated", "invalid"}


class BaselineError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise BaselineError(f"invalid baseline id: {value!r}")
    return value


@dataclass(frozen=True)
class Baseline:
    id: str
    profile: str
    target: str
    platform: str
    status: str
    fingerprints: dict[str, str]
    golden: dict[str, Any]
    thresholds: dict[str, Any]
    calibration: dict[str, Any]
    approval: dict[str, Any] | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Baseline":
        try:
            require_schema_version(data, "baseline")
        except ValueError as exc:
            raise BaselineError(str(exc)) from exc
        required_strings = ("id", "profile", "target", "platform", "status")
        for key in required_strings:
            if not isinstance(data.get(key), str) or not data[key]:
                raise BaselineError(f"baseline.{key} must be a non-empty string")
        _safe_id(data["id"])
        if data["target"] not in {"cpu", "gpu"}:
            raise BaselineError("baseline.target must be cpu or gpu")
        if data["status"] not in BASELINE_STATES:
            raise BaselineError(f"baseline.status must be one of {sorted(BASELINE_STATES)}")
        mappings: dict[str, dict[str, Any]] = {}
        for key in ("fingerprints", "golden", "thresholds", "calibration"):
            value = data.get(key)
            if not isinstance(value, dict):
                raise BaselineError(f"baseline.{key} must be a mapping")
            mappings[key] = dict(value)
        fingerprints = mappings["fingerprints"]
        if not fingerprints or not all(isinstance(key, str) and isinstance(value, str) for key, value in fingerprints.items()):
            raise BaselineError("baseline.fingerprints must contain string hashes")
        approval = data.get("approval")
        if approval is not None and not isinstance(approval, dict):
            raise BaselineError("baseline.approval must be a mapping or null")
        return cls(
            id=data["id"],
            profile=data["profile"],
            target=data["target"],
            platform=data["platform"],
            status=data["status"],
            fingerprints=fingerprints,
            golden=mappings["golden"],
            thresholds=mappings["thresholds"],
            calibration=mappings["calibration"],
            approval=dict(approval) if approval is not None else None,
            raw=dict(data),
        )

    @property
    def sha256(self) -> str:
        return document_sha256(self.raw)


class BaselineRegistry:
    """Manage draft/approved/deprecated state without mutating approved evidence."""

    def __init__(self, state_root: Path):
        self.root = state_root.expanduser().resolve() / "baselines"
        self.index_path = self.root / "registry.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def create_draft(self, proposal: Mapping[str, Any], *, baseline_id: str | None = None) -> Baseline:
        identifier = _safe_id(baseline_id or str(proposal.get("id", "")))
        index = self._load_index()
        if identifier in index["baselines"]:
            raise BaselineError(f"baseline already exists: {identifier}")
        document = dict(proposal)
        document.update({"schema_version": 1, "id": identifier, "status": "draft", "approval": None})
        baseline = Baseline.from_mapping(document)
        directory = self.root / identifier
        directory.mkdir(parents=True, exist_ok=False)
        atomic_write_json(directory / "draft.json", document)
        index["baselines"][identifier] = {
            "status": "draft",
            "profile": baseline.profile,
            "target": baseline.target,
            "platform": baseline.platform,
            "created_at": _now(),
            "events": [{"status": "draft", "at": _now()}],
        }
        self._write_index(index)
        return baseline

    def approve(self, baseline_id: str, approver: str) -> Baseline:
        identifier = _safe_id(baseline_id)
        if not approver.strip():
            raise BaselineError("approver must not be empty")
        index = self._load_index()
        entry = self._entry(index, identifier)
        if entry["status"] != "draft":
            raise BaselineError(f"only a draft may be approved; current status={entry['status']}")
        directory = self.root / identifier
        draft_path = directory / "draft.json"
        if not draft_path.exists():
            raise BaselineError(f"draft artifact missing: {draft_path}")
        document = json.loads(draft_path.read_text(encoding="utf-8"))
        approval = {"approver": approver.strip(), "approved_at": _now(), "draft_sha256": sha256_file(draft_path)}
        document.update({"status": "approved", "approval": approval})
        baseline = Baseline.from_mapping(document)
        approved_path = directory / "baseline.json"
        if approved_path.exists():
            raise BaselineError(f"approved baseline is immutable and already exists: {approved_path}")
        self._exclusive_json_write(approved_path, document)
        atomic_write_json(directory / "approval.json", approval)
        entry["status"] = "approved"
        entry["approved_sha256"] = sha256_file(approved_path)
        entry["events"].append({"status": "approved", "at": approval["approved_at"], "approver": approver.strip()})
        self._write_index(index)
        return baseline

    def deprecate(self, baseline_id: str, reason: str) -> Baseline:
        identifier = _safe_id(baseline_id)
        if not reason.strip():
            raise BaselineError("deprecation reason must not be empty")
        index = self._load_index()
        entry = self._entry(index, identifier)
        if entry["status"] != "approved":
            raise BaselineError(f"only an approved baseline may be deprecated; current status={entry['status']}")
        entry["status"] = "deprecated"
        entry["events"].append({"status": "deprecated", "at": _now(), "reason": reason.strip()})
        self._write_index(index)
        return self.get(identifier)

    def get(self, baseline_id: str) -> Baseline:
        identifier = _safe_id(baseline_id)
        index = self._load_index()
        entry = self._entry(index, identifier)
        filename = "baseline.json" if (self.root / identifier / "baseline.json").exists() else "draft.json"
        document = json.loads((self.root / identifier / filename).read_text(encoding="utf-8"))
        document["status"] = entry["status"]
        return Baseline.from_mapping(document)

    def list(self, *, status: str | None = None, profile: str | None = None) -> list[dict[str, Any]]:
        index = self._load_index()
        records: list[dict[str, Any]] = []
        for identifier, entry in index["baselines"].items():
            if status is not None and entry["status"] != status:
                continue
            if profile is not None and entry["profile"] != profile:
                continue
            records.append({"id": identifier, **entry})
        return sorted(records, key=lambda item: item["id"])

    def resolve(self, profile: str, fingerprints: Mapping[str, str]) -> Baseline:
        matches: list[Baseline] = []
        for entry in self.list(status="approved", profile=profile):
            baseline = self.get(entry["id"])
            if all(baseline.fingerprints.get(key) == value for key, value in fingerprints.items()):
                matches.append(baseline)
        if not matches:
            raise BaselineError(f"no approved compatible baseline for profile={profile}")
        if len(matches) > 1:
            raise BaselineError(f"multiple approved compatible baselines for profile={profile}; select an explicit ID")
        return matches[0]

    def verify_immutable(self, baseline_id: str) -> bool:
        identifier = _safe_id(baseline_id)
        index = self._load_index()
        entry = self._entry(index, identifier)
        expected = entry.get("approved_sha256")
        path = self.root / identifier / "baseline.json"
        return isinstance(expected, str) and path.exists() and sha256_file(path) == expected

    def export_bundle(self, baseline_id: str, destination: Path) -> Path:
        identifier = _safe_id(baseline_id)
        baseline = self.get(identifier)
        if baseline.status not in {"approved", "deprecated"} or not self.verify_immutable(identifier):
            raise BaselineError(f"only a hash-verified approved baseline can be exported: {identifier}")
        source = self.root / identifier
        target = destination.expanduser().resolve(strict=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise BaselineError(f"export destination already exists: {target}")
        with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    archive.write(path, arcname=f"{identifier}/{path.relative_to(source).as_posix()}")
            archive.writestr(
                "bundle-manifest.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "baseline_id": identifier,
                        "baseline_sha256": sha256_file(source / "baseline.json"),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
        return target

    def import_bundle(self, bundle: Path) -> Baseline:
        source = bundle.expanduser().resolve(strict=True)
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            try:
                with zipfile.ZipFile(source, "r") as archive:
                    for member in archive.infolist():
                        member_path = Path(member.filename)
                        if member_path.is_absolute() or ".." in member_path.parts:
                            raise BaselineError(f"unsafe baseline bundle member: {member.filename}")
                    archive.extractall(temporary)
            except zipfile.BadZipFile as exc:
                raise BaselineError(f"invalid baseline bundle: {source}") from exc
            manifest_path = temporary / "bundle-manifest.json"
            if not manifest_path.exists():
                raise BaselineError("baseline bundle has no bundle-manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            identifier = _safe_id(str(manifest.get("baseline_id", "")))
            imported_dir = temporary / identifier
            baseline_path = imported_dir / "baseline.json"
            if not baseline_path.exists() or sha256_file(baseline_path) != manifest.get("baseline_sha256"):
                raise BaselineError("baseline bundle hash verification failed")
            document = json.loads(baseline_path.read_text(encoding="utf-8"))
            baseline = Baseline.from_mapping(document)
            if baseline.id != identifier or baseline.status != "approved":
                raise BaselineError("bundle must contain one approved matching baseline")
            index = self._load_index()
            if identifier in index["baselines"] or (self.root / identifier).exists():
                raise BaselineError(f"baseline already exists: {identifier}")
            shutil.copytree(imported_dir, self.root / identifier)
            index["baselines"][identifier] = {
                "status": "approved",
                "profile": baseline.profile,
                "target": baseline.target,
                "platform": baseline.platform,
                "created_at": _now(),
                "approved_sha256": sha256_file(self.root / identifier / "baseline.json"),
                "events": [{"status": "imported-approved", "at": _now(), "bundle": str(source)}],
            }
            self._write_index(index)
            return baseline

    def _load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"schema_version": 1, "baselines": {}}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BaselineError(f"invalid baseline registry: {exc}") from exc
        if data.get("schema_version") != 1 or not isinstance(data.get("baselines"), dict):
            raise BaselineError("unsupported or malformed baseline registry")
        return data

    def _write_index(self, index: Mapping[str, Any]) -> None:
        atomic_write_json(self.index_path, dict(index))

    @staticmethod
    def _entry(index: dict[str, Any], identifier: str) -> dict[str, Any]:
        try:
            return index["baselines"][identifier]
        except KeyError as exc:
            raise BaselineError(f"unknown baseline: {identifier}") from exc

    @staticmethod
    def _exclusive_json_write(path: Path, data: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(data), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise BaselineError(f"immutable artifact already exists: {path}") from exc

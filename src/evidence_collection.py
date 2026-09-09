"""Bounded, hash-verified device artifact selection (never removes remote data)."""

import json
from pathlib import Path, PurePosixPath
from typing import Any

from .artifact_store import sha256_file
from .transport import Transport, TransportError


ARTIFACT_SETS = {
    "minimal": {"workload-summary.json", "final.json"},
    "calibration": {"workload-summary.json", "final.json", "telemetry.jsonl", "telemetry-agent.log"},
    "failure": {"workload-summary.json", "final.json", "workload.log", "workload-stderr.log",
                "workload-diagnostics.log", "events.jsonl", "telemetry.jsonl", "telemetry-agent.log", "relay.log"},
    "protocol": {"workload-summary.json", "final.json", "events.jsonl", "relay.log"},
}


def validated_hash_entries(document: Any) -> dict[str, str]:
    hashes = document.get("sha256") if isinstance(document, dict) else None
    if not isinstance(hashes, dict) or not hashes:
        raise TransportError("device artifact hash manifest is empty or malformed")
    for relative, digest in hashes.items():
        if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
            raise TransportError("unsafe path in device hash manifest")
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or str(path) != relative:
            raise TransportError(f"unsafe path in device hash manifest: {relative}")
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise TransportError(f"invalid artifact SHA-256: {relative}")
    return hashes


def collect_subset(transport: Transport, remote: PurePosixPath, local: Path, artifact_set: str) -> dict[str, Any]:
    selection = ARTIFACT_SETS[artifact_set]
    spool = local / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    manifest_path = spool / "artifact-hashes.json"
    transfer = transport.pull(remote / "spool" / "artifact-hashes.json", manifest_path)
    if not transfer.success:
        raise TransportError("subset collection requires completed device artifact-hashes.json; use full for interrupted runs")
    try:
        hashes = validated_hash_entries(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (ValueError, OSError) as exc:
        raise TransportError(f"invalid device artifact hash manifest: {exc}") from exc
    required = {"final.json"}
    if artifact_set == "calibration":
        required.update({"workload-summary.json", "telemetry.jsonl"})
    missing = required - hashes.keys()
    if missing:
        raise TransportError(f"{artifact_set} evidence missing required files: {sorted(missing)}")
    selected = sorted(selection & hashes.keys())
    total_bytes = 0
    for name in selected:
        destination = spool / name
        result = transport.pull(remote / "spool" / name, destination)
        if not result.success or not destination.is_file() or sha256_file(destination) != hashes[name]:
            raise TransportError(f"selected artifact missing or hash mismatch: {name}")
        total_bytes += destination.stat().st_size
    return {"artifact_set": artifact_set, "selected_files": selected, "omitted_files": sorted(hashes.keys() - set(selected)),
            "bytes_transferred": total_bytes, "verified": True, "verification_scope": "selected-files-only"}

"""Atomic qualification/run artifact storage with hashes."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO, Mapping

from .events import EventEnvelope


class ArtifactError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, data: Mapping[str, Any] | list[Any]) -> None:
    payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
    atomic_write_bytes(path, payload)


@dataclass
class ArtifactStore:
    run_id: str
    run_dir: Path
    _streams: dict[str, IO[str]] = field(default_factory=dict, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    STREAM_FILES = {
        "events": "events.jsonl",
        "telemetry": "telemetry.jsonl",
        "kernel": "kernel-events.jsonl",
    }

    @classmethod
    def create(
        cls,
        output_root: Path,
        run_id: str,
        *,
        test_id: str | None = None,
        allow_existing_empty: bool = False,
    ) -> "ArtifactStore":
        for name, value in (("run_id", run_id), ("test_id", test_id)):
            if value is not None and (not value or any(character in value for character in "\\/:*?\"<>|")):
                raise ArtifactError(f"unsafe {name}: {value!r}")
        root = output_root.expanduser().absolute()
        run_dir = root / test_id / run_id if test_id is not None else root / run_id
        if run_dir.exists() and any(run_dir.iterdir()) and not allow_existing_empty:
            raise ArtifactError(f"run directory already contains artifacts: {run_dir}")
        run_dir.mkdir(parents=True, exist_ok=True)
        return cls(run_id=run_id, run_dir=run_dir)

    def write_json(self, name: str, data: Mapping[str, Any] | list[Any]) -> Path:
        self._ensure_open()
        path = self.run_dir / name
        atomic_write_json(path, data)
        return path

    def write_bytes(self, name: str, data: bytes) -> Path:
        self._ensure_open()
        path = self.run_dir / name
        atomic_write_bytes(path, data)
        return path

    def append_event(self, event: EventEnvelope) -> None:
        self._append("events", event.to_dict(include_crc=True))
        if event.type == "telemetry":
            self._append("telemetry", event.to_dict(include_crc=True))
        elif event.type == "kernel":
            self._append("kernel", event.to_dict(include_crc=True))

    def append_raw_serial(self, data: bytes) -> None:
        self._ensure_open()
        path = self.run_dir / "serial.raw"
        with path.open("ab") as stream:
            stream.write(data)

    def finalize(self, result: Mapping[str, Any]) -> Path:
        self._close_streams()
        hashes = self._hash_artifacts(exclude={"artifact-hashes.json", "result.json"})
        self.write_json("artifact-hashes.json", {"schema_version": 1, "sha256": hashes})
        completed = dict(result)
        completed.setdefault("schema_version", 1)
        completed.setdefault("run_id", self.run_id)
        completed.setdefault("completed_at", datetime.now(timezone.utc).isoformat())
        completed["artifacts"] = {
            "complete": True,
            "hash_manifest": "artifact-hashes.json",
            "hashed_file_count": len(hashes),
        }
        path = self.run_dir / "result.json"
        atomic_write_json(path, completed)
        self._closed = True
        return path

    def _append(self, stream_name: str, record: Mapping[str, Any]) -> None:
        self._ensure_open()
        stream = self._streams.get(stream_name)
        if stream is None:
            filename = self.STREAM_FILES[stream_name]
            stream = (self.run_dir / filename).open("a", encoding="utf-8", newline="\n")
            self._streams[stream_name] = stream
        stream.write(json.dumps(dict(record), separators=(",", ":"), ensure_ascii=False) + "\n")
        stream.flush()

    def _close_streams(self) -> None:
        for stream in self._streams.values():
            stream.flush()
            stream.close()
        self._streams.clear()

    def _hash_artifacts(self, *, exclude: set[str]) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for path in sorted(self.run_dir.rglob("*")):
            if path.is_file() and path.name not in exclude and ".tmp-" not in path.name:
                hashes[path.relative_to(self.run_dir).as_posix()] = sha256_file(path)
        return hashes

    def _ensure_open(self) -> None:
        if self._closed:
            raise ArtifactError("artifact store is finalized")

    def close_incomplete(self, reason: str) -> Path:
        self._close_streams()
        path = self.run_dir / "result.json"
        atomic_write_json(
            path,
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "verdict": "INFRA_ERROR",
                "exit_code": 3,
                "infrastructure_reasons": [{"code": "ARTIFACT_FINALIZATION_FAILED", "message": reason}],
                "artifacts": {"complete": False},
            },
        )
        self._closed = True
        return path

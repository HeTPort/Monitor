"""Normalize PC and collected device artifacts for qualification consumers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .qualification import QualificationError


@dataclass(frozen=True)
class QualificationRunArtifacts:
    requested_path: Path
    pc_run_dir: Path | None
    spool_dir: Path | None
    result_path: Path | None
    events_path: Path | None
    telemetry_path: Path | None
    readback_path: Path | None
    summary: dict[str, Any]


def _first_existing(candidates: list[Path]) -> Path | None:
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve(strict=False)
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            return candidate
    return None


def _paired_pc_dir(path: Path) -> Path | None:
    if path.name == "spool" and path.parent.parent.name == "device-evidence":
        candidate = path.parent.parent.parent / path.parent.name
        if candidate.is_dir():
            return candidate.resolve()
    return None


def _paired_spool_dir(path: Path) -> Path | None:
    candidates = [
        path / "spool",
        path / "device-evidence" / "spool",
        path / "device-evidence" / path.name / "spool",
        path.parent / "device-evidence" / path.name / "spool",
    ]
    return _first_existing(candidates)


def _native_summary(workload_log: Path) -> dict[str, Any] | None:
    summary: dict[str, Any] | None = None
    for line_number, line in enumerate(workload_log.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("type") == "summary":
            summary = dict(record)
    return summary


def resolve_qualification_run(path: Path) -> QualificationRunArtifacts:
    requested = path.expanduser().resolve(strict=True)
    if not requested.is_dir():
        raise QualificationError(f"qualification run path is not a directory: {requested}")

    if requested.name == "spool":
        spool_dir = requested
        pc_run_dir = _paired_pc_dir(requested)
    else:
        spool_dir = _paired_spool_dir(requested)
        pc_run_dir = requested if (requested / "result.json").exists() else None

    result_path = _first_existing(
        [candidate / "result.json" for candidate in (pc_run_dir, requested) if candidate is not None]
    )
    events_path = _first_existing(
        [
            *( [spool_dir / "events.jsonl"] if spool_dir is not None else [] ),
            requested / "events.jsonl",
        ]
    )
    telemetry_path = _first_existing(
        [
            *( [spool_dir / "telemetry.jsonl"] if spool_dir is not None else [] ),
            requested / "telemetry.jsonl",
        ]
    )
    readback_path = _first_existing(
        [
            *( [spool_dir / "gpu-golden.rgba"] if spool_dir is not None else [] ),
            requested / "gpu-golden.rgba",
        ]
    )

    summary: dict[str, Any] = {}
    workload_log = spool_dir / "workload.log" if spool_dir is not None else None
    if workload_log is not None and workload_log.exists():
        summary = _native_summary(workload_log) or {}
    if not summary:
        summary_path = _first_existing(
            [
                *( [pc_run_dir / "workload-summary-full.json"] if pc_run_dir is not None else [] ),
                *( [pc_run_dir / "workload-summary.json"] if pc_run_dir is not None else [] ),
                requested / "workload-summary-full.json",
                requested / "workload-summary.json",
            ]
        )
        if summary_path is not None:
            try:
                loaded = json.loads(summary_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise QualificationError(f"invalid workload summary {summary_path}: {exc}") from exc
            if isinstance(loaded, dict):
                summary = dict(loaded)

    return QualificationRunArtifacts(
        requested_path=requested,
        pc_run_dir=pc_run_dir,
        spool_dir=spool_dir,
        result_path=result_path,
        events_path=events_path,
        telemetry_path=telemetry_path,
        readback_path=readback_path,
        summary=summary,
    )

"""Golden consistency checks and repeatable calibration statistics."""

from __future__ import annotations

import hashlib
import math
import shutil
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .artifact_store import atomic_write_json, sha256_file
from .config_loader import document_sha256


class QualificationError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def correctness_fingerprint(fields: Mapping[str, Any]) -> str:
    return document_sha256(dict(fields))


class GoldenService:
    """Validate repeated known-good correctness outputs and persist manifests."""

    def __init__(self, qualification_root: Path):
        self.root = qualification_root.expanduser().resolve()

    def create_cpu(
        self,
        *,
        qualification_id: str,
        profile: str,
        fingerprint_fields: Mapping[str, Any],
        golden_records: Iterable[Mapping[str, Any]],
        board_ids: Iterable[str],
    ) -> dict[str, Any]:
        records = [dict(record) for record in golden_records]
        boards = list(board_ids)
        if not records:
            raise QualificationError("CPU golden requires at least one record")
        checksums = [record.get("checksum") for record in records]
        if any(not isinstance(checksum, str) or not checksum for checksum in checksums):
            raise QualificationError("CPU golden record is missing checksum")
        if len(set(checksum.lower() for checksum in checksums)) != 1:
            raise QualificationError("CPU golden checksums are inconsistent across repeats")
        manifest = {
            "schema_version": 1,
            "kind": "cpu-checksum",
            "qualification_id": qualification_id,
            "profile": profile,
            "created_at": _now(),
            "correctness_fingerprint": correctness_fingerprint(fingerprint_fields),
            "fingerprint_fields": dict(fingerprint_fields),
            "checksum": checksums[0].lower(),
            "repeat_count": len(records),
            "board_ids": boards,
            "records": records,
        }
        path = self._golden_dir(qualification_id) / "cpu-golden.json"
        if path.exists():
            raise QualificationError(f"CPU golden destination already exists: {path}")
        atomic_write_json(path, manifest)
        manifest["manifest_path"] = str(path)
        manifest["manifest_sha256"] = sha256_file(path)
        return manifest

    def create_gpu(
        self,
        *,
        qualification_id: str,
        profile: str,
        fingerprint_fields: Mapping[str, Any],
        golden_records: Iterable[Mapping[str, Any]],
        readback_files: Iterable[Path],
        board_ids: Iterable[str],
    ) -> dict[str, Any]:
        records = [dict(record) for record in golden_records]
        files = [path.expanduser().resolve(strict=True) for path in readback_files]
        if not records or not files or len(records) != len(files):
            raise QualificationError("GPU golden requires one record and readback file per repeat")
        hashes = [sha256_file(path) for path in files]
        if len(set(hashes)) != 1:
            raise QualificationError("GPU raw readbacks are inconsistent across repeats")
        destination_dir = self._golden_dir(qualification_id)
        destination = destination_dir / "gpu-golden.rgba"
        if destination.exists():
            raise QualificationError(f"GPU golden destination already exists: {destination}")
        shutil.copy2(files[0], destination)
        manifest = {
            "schema_version": 1,
            "kind": "gpu-readback",
            "qualification_id": qualification_id,
            "profile": profile,
            "created_at": _now(),
            "correctness_fingerprint": correctness_fingerprint(fingerprint_fields),
            "fingerprint_fields": dict(fingerprint_fields),
            "readback_file": destination.name,
            "readback_sha256": hashes[0],
            "readback_size": destination.stat().st_size,
            "repeat_count": len(records),
            "board_ids": list(board_ids),
            "records": records,
        }
        path = destination_dir / "gpu-golden.json"
        atomic_write_json(path, manifest)
        manifest["manifest_path"] = str(path)
        manifest["manifest_sha256"] = sha256_file(path)
        return manifest

    def _golden_dir(self, qualification_id: str) -> Path:
        if not qualification_id or any(character in qualification_id for character in "\\/:*?\"<>|"):
            raise QualificationError(f"unsafe qualification id: {qualification_id!r}")
        path = self.root / qualification_id / "golden"
        path.mkdir(parents=True, exist_ok=True)
        return path


@dataclass(frozen=True)
class CalibrationSample:
    run_id: str
    board_id: str
    summary: dict[str, Any]
    temperature_c: float | None = None
    environment_compliant: bool = True
    telemetry_complete: bool = True
    throttled: bool = False
    rejection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CalibrationPolicy:
    minimum_boards: int = 2
    minimum_accepted_samples: int = 20
    throughput_margin_percent: float = 5.0
    latency_margin_percent: float = 10.0
    reject_telemetry_gaps: bool = True
    reject_throttled_samples: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CalibrationPolicy":
        rejection = data.get("rejection", {})
        limits = data.get("limits", {})
        throughput = limits.get("throughput", {}) if isinstance(limits, dict) else {}
        latency = limits.get("latency", {}) if isinstance(limits, dict) else {}
        return cls(
            minimum_boards=int(data.get("minimum_boards", 2)),
            minimum_accepted_samples=int(data.get("minimum_accepted_samples", 20)),
            throughput_margin_percent=float(throughput.get("margin_percent", 5.0)),
            latency_margin_percent=float(latency.get("margin_percent", 10.0)),
            reject_telemetry_gaps=bool(rejection.get("reject_telemetry_gaps", True)),
            reject_throttled_samples=bool(rejection.get("reject_throttled_samples", True)),
        )


class CalibrationService:
    """Reject uncontrolled runs, aggregate distributions, and propose fail-closed limits."""

    DEFAULT_METRICS = {
        "cpu": {"throughput": "operations_per_sec_avg", "latency": "batch_time_ms_p99"},
        "gpu": {"throughput": "fps_avg", "latency": "frame_time_p99_ms"},
    }

    def calibrate(
        self,
        *,
        profile: str,
        target: str,
        platform: str,
        fingerprints: Mapping[str, str],
        golden: Mapping[str, Any],
        samples: Iterable[CalibrationSample],
        policy: CalibrationPolicy,
        temperature_range: tuple[float, float] | None = None,
        metric_names: Mapping[str, str] | None = None,
        baseline_id: str,
    ) -> dict[str, Any]:
        if target not in self.DEFAULT_METRICS:
            raise QualificationError(f"unsupported calibration target: {target}")
        metrics = dict(metric_names or self.DEFAULT_METRICS[target])
        accepted: list[CalibrationSample] = []
        rejected: list[dict[str, Any]] = []
        for sample in samples:
            reasons = self._rejection_reasons(sample, policy, temperature_range)
            for metric in metrics.values():
                value = sample.summary.get(metric)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                    reasons.append(f"missing_or_invalid_metric:{metric}")
            if reasons:
                rejected.append({"run_id": sample.run_id, "board_id": sample.board_id, "reasons": sorted(set(reasons))})
            else:
                accepted.append(sample)
        boards = sorted({sample.board_id for sample in accepted})
        if len(boards) < policy.minimum_boards:
            raise QualificationError(
                f"accepted cohort has {len(boards)} boards; minimum is {policy.minimum_boards}; "
                f"{self._rejection_summary(rejected)}"
            )
        if len(accepted) < policy.minimum_accepted_samples:
            raise QualificationError(
                f"accepted sample count is {len(accepted)}; minimum is {policy.minimum_accepted_samples}; "
                f"{self._rejection_summary(rejected)}"
            )
        distributions: dict[str, Any] = {}
        for role, metric in metrics.items():
            values = [float(sample.summary[metric]) for sample in accepted]
            distributions[metric] = self._distribution(values)
        throughput_metric = metrics["throughput"]
        latency_metric = metrics["latency"]
        minimum_throughput = distributions[throughput_metric]["min"] * (1.0 - policy.throughput_margin_percent / 100.0)
        maximum_latency = distributions[latency_metric]["max"] * (1.0 + policy.latency_margin_percent / 100.0)
        proposal = {
            "schema_version": 1,
            "id": baseline_id,
            "profile": profile,
            "target": target,
            "platform": platform,
            "status": "draft",
            "fingerprints": dict(fingerprints),
            "golden": dict(golden),
            "thresholds": {
                "performance": {
                    throughput_metric: {"min": minimum_throughput},
                    latency_metric: {"max": maximum_latency},
                }
            },
            "calibration": {
                "created_at": _now(),
                "policy": {
                    "minimum_boards": policy.minimum_boards,
                    "minimum_accepted_samples": policy.minimum_accepted_samples,
                    "throughput_margin_percent": policy.throughput_margin_percent,
                    "latency_margin_percent": policy.latency_margin_percent,
                },
                "accepted_count": len(accepted),
                "rejected_count": len(rejected),
                "board_ids": boards,
                "accepted_runs": [sample.run_id for sample in accepted],
                "rejected_runs": rejected,
                "distributions": distributions,
                "temperature_range_c": list(temperature_range) if temperature_range else None,
            },
            "approval": None,
        }
        proposal["proposal_sha256"] = document_sha256(proposal)
        return proposal

    @staticmethod
    def _rejection_summary(rejected: list[dict[str, Any]]) -> str:
        if not rejected:
            return "no samples were rejected; add boards or accepted runs"
        details = "; ".join(
            f"{item['run_id']}[{item['board_id']}]:{','.join(item['reasons'])}"
            for item in rejected
        )
        return f"rejected runs: {details}"

    @staticmethod
    def _rejection_reasons(
        sample: CalibrationSample,
        policy: CalibrationPolicy,
        temperature_range: tuple[float, float] | None,
    ) -> list[str]:
        reasons = list(sample.rejection_reasons)
        if sample.summary.get("result") != "PASS" or sample.summary.get("exit_code") != 0:
            reasons.append("workload_not_pass")
        if not sample.environment_compliant:
            reasons.append("environment_noncompliant")
        if policy.reject_telemetry_gaps and not sample.telemetry_complete:
            reasons.append("telemetry_incomplete")
        if policy.reject_throttled_samples and sample.throttled:
            reasons.append("throttled")
        if temperature_range is not None:
            if sample.temperature_c is None:
                reasons.append("temperature_missing")
            elif not (temperature_range[0] <= sample.temperature_c <= temperature_range[1]):
                reasons.append("temperature_out_of_range")
        return reasons

    @staticmethod
    def _distribution(values: list[float]) -> dict[str, float | int]:
        ordered = sorted(values)
        mean = statistics.fmean(ordered)
        stddev = statistics.pstdev(ordered) if len(ordered) > 1 else 0.0
        return {
            "count": len(ordered),
            "min": ordered[0],
            "max": ordered[-1],
            "mean": mean,
            "median": statistics.median(ordered),
            "p05": CalibrationService._percentile(ordered, 0.05),
            "p95": CalibrationService._percentile(ordered, 0.95),
            "stddev": stddev,
            "cv_percent": (stddev / mean * 100.0) if mean else 0.0,
        }

    @staticmethod
    def _percentile(ordered: list[float], fraction: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

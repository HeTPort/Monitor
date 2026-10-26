"""Shared, fail-closed verification evidence checks for live runs and calibration."""

from __future__ import annotations

from typing import Any, Mapping

from .target_adapters import get_target


CONTRACT_VERSION = 2
REQUIRED_FEATURES = frozenset({
    "verify_interval", "success_log_interval", "verify_count", "failure_verify", "live_heartbeat",
})
VERIFICATION_FIELDS = frozenset({
    "contract_version", "verify_count", "verify_fail_count", "verify_mode", "verify_interval",
    "success_log_interval", "batch_count", "frame_count", "inference_count", "verify_pass",
    "backend", "fallback_used", "checksum", "golden_checksum", "device_time_valid",
    "inferences_per_sec_avg", "host_inference_time_ms_p99", "device_inference_time_ms_p99",
})


def verification_requirement(target: str, config: Mapping[str, Any], *, strict: bool = False) -> dict[str, Any]:
    mode = config.get("verify_mode", "none")
    interval = config.get("verify_interval", config.get("checksum_interval", 1))
    if strict and (mode == "none" or interval != 1):
        raise ValueError("golden/baseline AVS runs require verification enabled and verify_interval=1")
    if strict and any(config.get(name) for name in ("per_batch_log", "per_frame_log", get_target(target).per_unit_log)):
        raise ValueError("formal AVS runs require per_batch_log/per_frame_log/per_inference_log=false; use bounded success verify logging")
    return {
        "required": mode != "none",
        "contract_version": CONTRACT_VERSION,
        "mode": mode,
        "verify_interval": interval,
        "success_log_interval": config.get("success_log_interval", 60),
        "unit_metric": get_target(target).unit_metric,
    }


def verification_issues(summary: Mapping[str, Any], requirement: Mapping[str, Any]) -> list[str]:
    """A PASS must prove that the configured number of actual checks happened.

    Successful-event sampling is deliberately irrelevant to this calculation.
    Golden generation is exempted by the caller: it generates, not verifies.
    """
    if not requirement.get("required"):
        return []
    issues: list[str] = []
    for key, expected in (
        ("contract_version", requirement.get("contract_version", CONTRACT_VERSION)),
        ("verify_mode", requirement.get("mode")),
        ("verify_interval", requirement.get("verify_interval")),
        ("success_log_interval", requirement.get("success_log_interval")),
    ):
        if key not in summary:
            issues.append(f"missing:{key}")
        elif type(summary[key]) is not type(expected) or summary[key] != expected:
            issues.append(f"mismatch:{key}")
    unit_metric = str(requirement.get("unit_metric", "batch_count"))
    for key in ("verify_count", "verify_fail_count", unit_metric):
        value = summary.get(key)
        if type(value) is not int or value < 0:
            issues.append(f"invalid_or_missing:{key}")
    if summary.get("verify_pass") is not True:
        issues.append("verification_not_pass")
    if not issues:
        count = summary["verify_count"]
        failures = summary["verify_fail_count"]
        interval = requirement.get("verify_interval")
        if type(interval) is not int or interval < 1:
            issues.append("invalid_required_interval")
        elif count == 0 or count != summary[unit_metric] // interval:
            issues.append("verification_coverage_mismatch")
        if failures != 0 or failures > count:
            issues.append("verification_failures_present")
    checksum = requirement.get("expected_checksum")
    if checksum and (str(summary.get("golden_checksum", "")).lower() != checksum or str(summary.get("checksum", "")).lower() != checksum):
        issues.append("golden_checksum_mismatch")
    if requirement.get("real_npu") and (summary.get("backend") != "tflite_delegate" or summary.get("fallback_used") is not False):
        issues.append("real_npu_execution_evidence_missing")
    return issues


def validate_capabilities(data: Mapping[str, Any], target: str, mode: str) -> None:
    if data.get("workload") != target or type(data.get("contract_version")) is not int or data["contract_version"] != CONTRACT_VERSION:
        raise ValueError(f"{target} workload verification contract v{CONTRACT_VERSION} is required; rebuild and redeploy workload")
    features = data.get("features")
    if not isinstance(features, list) or not all(isinstance(item, str) for item in features) or not REQUIRED_FEATURES.issubset(features):
        raise ValueError("workload lacks mandatory verification/heartbeat capabilities; rebuild and redeploy")
    if target == "npu" and ("success_log_rate_limit" not in features or type(data.get("success_log_min_period_ms")) is not int or data["success_log_min_period_ms"] != 1000):
        raise ValueError("NPU workload must advertise the bounded success-log rate limit")
    modes = data.get("verify_modes")
    if not isinstance(modes, list) or mode not in modes:
        raise ValueError(f"deployed workload does not support verify_mode={mode}")

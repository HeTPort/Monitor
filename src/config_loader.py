"""Structured configuration loading and lightweight schema validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


SUPPORTED_SCHEMA_VERSION = 1
VALID_TARGETS = {"cpu", "gpu"}
VALID_VERIFY_MODES = {"cpu": {"none", "checksum"}, "gpu": {"none", "golden-image"}}


class ConfigError(ValueError):
    """Raised for missing dependencies, malformed data, or schema violations."""


def load_document(path: Path) -> dict[str, Any]:
    """Load a JSON or YAML mapping with a clear dependency error for YAML."""
    resolved = path.expanduser().resolve(strict=True)
    text = resolved.read_text(encoding="utf-8")
    suffix = resolved.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON in {resolved}: {exc}") from exc
    elif suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise ConfigError("PyYAML is required to load YAML; install requirements.txt") from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {resolved}: {exc}") from exc
    else:
        raise ConfigError(f"unsupported configuration extension: {resolved.suffix}")
    if not isinstance(data, dict):
        raise ConfigError(f"configuration root must be a mapping: {resolved}")
    return data


def canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def document_sha256(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def _require(mapping: Mapping[str, Any], key: str, expected: type, context: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{context}: missing required field '{key}'")
    value = mapping[key]
    if not isinstance(value, expected):
        raise ConfigError(f"{context}.{key}: expected {expected.__name__}, got {type(value).__name__}")
    return value


def require_schema_version(data: Mapping[str, Any], context: str) -> int:
    version = _require(data, "schema_version", int, context)
    if version != SUPPORTED_SCHEMA_VERSION:
        raise ConfigError(
            f"{context}.schema_version: unsupported major version {version}; "
            f"supported={SUPPORTED_SCHEMA_VERSION}"
        )
    return version


def validate_workload_config(
    path: Path,
    target: str,
    *,
    device_root: PurePosixPath = PurePosixPath("/data/local/tmp/avs"),
) -> dict[str, Any]:
    """Validate the workload JSON contract used by deployed CPU/GPU binaries."""

    resolved = path.expanduser().resolve(strict=True)
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid workload JSON {resolved}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"workload configuration root must be a mapping: {resolved}")
    if target not in VALID_VERIFY_MODES:
        raise ConfigError(f"unsupported workload target: {target}")
    api = document.get("api")
    expected_api = "cpu" if target == "cpu" else "vulkan"
    if api != expected_api:
        raise ConfigError(f"workload {resolved}.api must be {expected_api!r} for target {target}")
    verify_mode = document.get("verify_mode")
    if verify_mode not in VALID_VERIFY_MODES[target]:
        raise ConfigError(
            f"workload {resolved}.verify_mode must be one of {sorted(VALID_VERIFY_MODES[target])}"
        )
    if document.get("output_format") != "jsonl":
        raise ConfigError(f"workload {resolved}.output_format must be 'jsonl'")
    positive_fields = ["duration", "timeout", "iterations", "heartbeat_interval"]
    positive_fields.extend(["threads", "working_set_kb"] if target == "cpu" else ["width", "height", "gpu_timeout_ms"])
    for name in positive_fields:
        value = document.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"workload {resolved}.{name} must be a positive integer")
    warmup = document.get("warmup")
    if not isinstance(warmup, int) or isinstance(warmup, bool) or warmup < 0:
        raise ConfigError(f"workload {resolved}.warmup must be a non-negative integer")
    if target == "gpu" and verify_mode == "golden-image":
        golden_value = document.get("golden_file")
        if not isinstance(golden_value, str) or not golden_value:
            raise ConfigError(f"workload {resolved}.golden_file is required for golden-image verification")
        golden_path = PurePosixPath(golden_value)
        if not golden_path.is_absolute() or not golden_path.is_relative_to(device_root):
            raise ConfigError(
                f"workload {resolved}.golden_file must be an absolute path below {device_root}"
            )
    return document


@dataclass(frozen=True)
class ProfileConfig:
    schema_version: int
    name: str
    target: str
    platform: str
    workload: dict[str, Any]
    environment: dict[str, Any]
    baseline: str | None
    telemetry: dict[str, Any]
    kernel_monitor: str
    kernel_options: dict[str, Any]
    source_path: Path
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_file(cls, path: Path) -> "ProfileConfig":
        data = load_document(path)
        return cls.from_mapping(data, source_path=path.expanduser().resolve())

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, source_path: Path) -> "ProfileConfig":
        context = f"profile {source_path}"
        version = require_schema_version(data, context)
        name = _require(data, "name", str, context).strip()
        target = _require(data, "target", str, context).lower()
        if target not in VALID_TARGETS:
            raise ConfigError(f"{context}.target: expected one of {sorted(VALID_TARGETS)}")
        platform = _require(data, "platform", str, context).strip()
        if not platform:
            raise ConfigError(f"{context}.platform: must not be empty")
        workload = dict(_require(data, "workload", dict, context))
        binary = _require(workload, "binary", str, f"{context}.workload")
        if not binary.strip():
            raise ConfigError(f"{context}.workload.binary: must not be empty")
        argv = workload.get("argv", [])
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise ConfigError(f"{context}.workload.argv: expected a list of strings")
        assets = workload.get("assets", [])
        if not isinstance(assets, list):
            raise ConfigError(f"{context}.workload.assets: expected a list")
        for index, asset in enumerate(assets):
            asset_context = f"{context}.workload.assets[{index}]"
            if not isinstance(asset, dict):
                raise ConfigError(f"{asset_context}: expected a mapping")
            for field_name in ("local", "remote"):
                if not isinstance(asset.get(field_name), str) or not asset[field_name]:
                    raise ConfigError(f"{asset_context}.{field_name}: expected a non-empty string")
            for field_name in ("required", "executable"):
                if field_name in asset and not isinstance(asset[field_name], bool):
                    raise ConfigError(f"{asset_context}.{field_name}: expected a boolean")
        scheduler_requirements = data.get("scheduler_requirements")
        legacy_environment = data.get("environment")
        if scheduler_requirements is not None and legacy_environment is not None:
            raise ConfigError(f"{context}: use scheduler_requirements or legacy environment, not both")
        environment = scheduler_requirements if scheduler_requirements is not None else (legacy_environment or {})
        telemetry = data.get("telemetry") or {}
        if not isinstance(environment, dict) or not isinstance(telemetry, dict):
            raise ConfigError(f"{context}: scheduler_requirements/environment and telemetry must be mappings")
        required = telemetry.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ConfigError(f"{context}.telemetry.required: expected a list of strings")
        baseline = data.get("baseline")
        if baseline is not None and not isinstance(baseline, str):
            raise ConfigError(f"{context}.baseline: expected string or null")
        kernel_monitor = data.get("kernel_monitor", "critical")
        if kernel_monitor not in {"off", "critical", "full-local"}:
            raise ConfigError(f"{context}.kernel_monitor: expected off, critical, or full-local")
        kernel_options = data.get("kernel_options", {})
        if not isinstance(kernel_options, dict):
            raise ConfigError(f"{context}.kernel_options: expected a mapping")
        if "dedupe_window_ms" in kernel_options and (
            not isinstance(kernel_options["dedupe_window_ms"], int)
            or isinstance(kernel_options["dedupe_window_ms"], bool)
            or kernel_options["dedupe_window_ms"] < 0
        ):
            raise ConfigError(f"{context}.kernel_options.dedupe_window_ms: expected a non-negative integer")
        if "max_events_per_second" in kernel_options and (
            not isinstance(kernel_options["max_events_per_second"], int)
            or isinstance(kernel_options["max_events_per_second"], bool)
            or kernel_options["max_events_per_second"] < 1
        ):
            raise ConfigError(f"{context}.kernel_options.max_events_per_second: expected a positive integer")
        return cls(
            schema_version=version,
            name=name,
            target=target,
            platform=platform,
            workload=workload,
            environment=dict(environment),
            baseline=baseline,
            telemetry=dict(telemetry),
            kernel_monitor=kernel_monitor,
            kernel_options=dict(kernel_options),
            source_path=source_path,
            raw=dict(data),
        )

    @property
    def fingerprint(self) -> str:
        return document_sha256(self.raw)


@dataclass(frozen=True)
class PlatformConfig:
    schema_version: int
    name: str
    identity: dict[str, Any]
    transport: dict[str, Any]
    serial: dict[str, Any]
    cpu: dict[str, Any]
    gpu: dict[str, Any]
    thermal: dict[str, Any]
    source_path: Path
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_file(cls, path: Path) -> "PlatformConfig":
        data = load_document(path)
        context = f"platform {path}"
        version = require_schema_version(data, context)
        name = _require(data, "name", str, context)
        sections: dict[str, dict[str, Any]] = {}
        for section in ("identity", "transport", "serial", "cpu", "gpu", "thermal"):
            value = data.get(section) or {}
            if not isinstance(value, dict):
                raise ConfigError(f"{context}.{section}: expected mapping")
            sections[section] = dict(value)
        identity = sections["identity"]
        identity_required = identity.get("required", False)
        if not isinstance(identity_required, bool):
            raise ConfigError(f"{context}.identity.required: expected boolean")
        identity_fields = identity.get("fields", {})
        if not isinstance(identity_fields, dict):
            raise ConfigError(f"{context}.identity.fields: expected mapping")
        if identity_required and not identity_fields:
            raise ConfigError(f"{context}.identity.fields: required identity needs at least one field")
        for field_name, definition in identity_fields.items():
            field_context = f"{context}.identity.fields.{field_name}"
            if not isinstance(field_name, str) or not field_name or not isinstance(definition, dict):
                raise ConfigError(f"{field_context}: expected named mapping")
            path_value = definition.get("path")
            if not isinstance(path_value, str) or not path_value:
                raise ConfigError(f"{field_context}.path: expected non-empty string")
            parser = definition.get("parser", "text")
            if parser not in {"text", "kernel_cmdline"}:
                raise ConfigError(f"{field_context}.parser: expected text or kernel_cmdline")
            if parser == "kernel_cmdline" and (
                not isinstance(definition.get("key"), str) or not definition["key"].strip()
            ):
                raise ConfigError(f"{field_context}.key: expected non-empty string for kernel_cmdline")
            accepted = definition.get("accepted", [])
            if not isinstance(accepted, list) or not accepted or any(
                not isinstance(value, str) or not value for value in accepted
            ):
                raise ConfigError(f"{field_context}.accepted: expected non-empty string list")
            if "required" in definition and not isinstance(definition["required"], bool):
                raise ConfigError(f"{field_context}.required: expected boolean")
        serial = sections["serial"]
        baudrate = serial.get("baudrate")
        if baudrate is not None and (not isinstance(baudrate, int) or isinstance(baudrate, bool) or baudrate <= 0):
            raise ConfigError(f"{context}.serial.baudrate: expected a positive integer")
        uart_candidates = serial.get("uart_candidates", [])
        if not isinstance(uart_candidates, list) or any(
            not isinstance(candidate, str) or not candidate.strip()
            for candidate in uart_candidates
        ):
            raise ConfigError(f"{context}.serial.uart_candidates: expected a list of non-empty strings")
        protocol = serial.get("protocol", "uart-v2")
        if protocol != "uart-v2":
            raise ConfigError(f"{context}.serial.protocol: expected uart-v2")
        max_frame_bytes = serial.get("max_frame_bytes", 512)
        if (
            not isinstance(max_frame_bytes, int)
            or isinstance(max_frame_bytes, bool)
            or not 64 <= max_frame_bytes <= 4096
        ):
            raise ConfigError(f"{context}.serial.max_frame_bytes: expected integer from 64 to 4096")
        tail_guard_bytes = serial.get("tail_guard_bytes", 64)
        if (
            not isinstance(tail_guard_bytes, int)
            or isinstance(tail_guard_bytes, bool)
            or not 0 <= tail_guard_bytes <= 4096
        ):
            raise ConfigError(f"{context}.serial.tail_guard_bytes: expected integer from 0 to 4096")
        safe_utilization = serial.get("safe_utilization", 0.70)
        if (
            not isinstance(safe_utilization, (int, float))
            or isinstance(safe_utilization, bool)
            or not 0 < float(safe_utilization) <= 1
        ):
            raise ConfigError(f"{context}.serial.safe_utilization: expected number greater than 0 and at most 1")
        relay = serial.get("relay", {})
        if not isinstance(relay, dict):
            raise ConfigError(f"{context}.serial.relay: expected mapping")
        for key in ("local_asset", "remote_asset"):
            if key in relay and (not isinstance(relay[key], str) or not relay[key].strip()):
                raise ConfigError(f"{context}.serial.relay.{key}: expected non-empty string")
        policy_glob = sections["cpu"].get("policy_glob")
        if policy_glob is not None and (not isinstance(policy_glob, str) or not policy_glob):
            raise ConfigError(f"{context}.cpu.policy_glob: expected non-empty string")
        for domain in ("cpu", "gpu"):
            interfaces = sections[domain].get("interfaces", {})
            if not isinstance(interfaces, dict):
                raise ConfigError(f"{context}.{domain}.interfaces: expected mapping")
            for interface_name, definition in interfaces.items():
                if not isinstance(definition, dict):
                    raise ConfigError(f"{context}.{domain}.interfaces.{interface_name}: expected mapping")
                available_candidates = definition.get("available_values_candidates")
                if available_candidates is not None and (
                    not isinstance(available_candidates, list)
                    or not available_candidates
                    or any(not isinstance(value, str) or not value for value in available_candidates)
                ):
                    raise ConfigError(
                        f"{context}.{domain}.interfaces.{interface_name}.available_values_candidates: "
                        "expected non-empty string list"
                    )
                if "require_requested_value" in definition and not isinstance(
                    definition["require_requested_value"], bool
                ):
                    raise ConfigError(
                        f"{context}.{domain}.interfaces.{interface_name}.require_requested_value: expected boolean"
                    )
        thermal = sections["thermal"]
        allowed_temperature_units = {"auto", "degree_celsius", "celsius", "millidegree_celsius", "millicelsius"}
        temperature_unit = str(thermal.get("temperature_unit", "millidegree_celsius")).lower()
        if temperature_unit not in allowed_temperature_units:
            raise ConfigError(
                f"{context}.thermal.temperature_unit: expected one of {sorted(allowed_temperature_units)}"
            )
        unit_overrides = thermal.get("temperature_unit_by_type", {})
        if not isinstance(unit_overrides, dict) or any(
            not isinstance(pattern, str)
            or str(unit).lower() not in allowed_temperature_units
            for pattern, unit in unit_overrides.items()
        ):
            raise ConfigError(
                f"{context}.thermal.temperature_unit_by_type: expected a mapping of patterns to supported units"
            )
        plausible = thermal.get("plausible_range_c", {"min": -40.0, "max": 200.0})
        if not isinstance(plausible, dict):
            raise ConfigError(f"{context}.thermal.plausible_range_c: expected mapping")
        try:
            plausible_min = float(plausible.get("min", -40.0))
            plausible_max = float(plausible.get("max", 200.0))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{context}.thermal.plausible_range_c: min/max must be numeric") from exc
        if plausible_min >= plausible_max:
            raise ConfigError(f"{context}.thermal.plausible_range_c: min must be less than max")
        return cls(
            schema_version=version,
            name=name,
            source_path=path.expanduser().resolve(),
            raw=dict(data),
            **sections,
        )

    @property
    def fingerprint(self) -> str:
        return document_sha256(self.raw)


def validate_required_strings(values: Sequence[Any], context: str) -> list[str]:
    if not isinstance(values, (list, tuple)) or not all(isinstance(item, str) and item for item in values):
        raise ConfigError(f"{context}: expected non-empty strings")
    return list(values)

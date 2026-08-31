"""Transport-independent platform and telemetry interface discovery."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from .config_loader import PlatformConfig


class ProbeError(RuntimeError):
    pass


class ProbeBackend(ABC):
    """Minimal filesystem/device identity surface used by :class:`PlatformProbe`."""

    @abstractmethod
    def glob(self, pattern: str) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def exists(self, path: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def read_text(self, path: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def identity(self) -> Mapping[str, Any]:
        raise NotImplementedError


@dataclass
class MappingProbeBackend(ProbeBackend):
    """Deterministic fake backend for simulations and office-PC tests."""

    files: dict[str, str]
    device_identity: dict[str, Any] = field(default_factory=lambda: {"serial": "FAKE", "build": "test"})

    def glob(self, pattern: str) -> list[str]:
        import fnmatch

        pattern_parts = PurePosixPath(pattern).parts
        paths = self._known_paths()
        return sorted(
            path
            for path in paths
            if len(PurePosixPath(path).parts) == len(pattern_parts)
            and all(
                fnmatch.fnmatchcase(part, expected)
                for part, expected in zip(PurePosixPath(path).parts, pattern_parts)
            )
        )

    def exists(self, path: str) -> bool:
        return str(PurePosixPath(path)) in self._known_paths()

    def read_text(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def identity(self) -> Mapping[str, Any]:
        return dict(self.device_identity)

    def _known_paths(self) -> set[str]:
        paths: set[str] = set()
        for filename in self.files:
            current = PurePosixPath(filename)
            paths.add(str(current))
            paths.update(str(parent) for parent in current.parents if str(parent) != ".")
        return paths


class PlatformProbe:
    """Resolve interface candidates and record their origin, unit, and readability."""

    def __init__(self, platform: PlatformConfig, backend: ProbeBackend):
        self.platform = platform
        self.backend = backend

    def probe(self, *, full: bool = True, domains: Iterable[str] | None = None) -> dict[str, Any]:
        selected_domains = set(domains or ("cpu", "gpu"))
        invalid_domains = selected_domains.difference({"cpu", "gpu"})
        if invalid_domains:
            raise ProbeError(f"unsupported probe domains: {', '.join(sorted(invalid_domains))}")
        platform_identity = self._probe_platform_identity()
        capabilities: dict[str, Any] = {}
        for domain, section in (("cpu", self.platform.cpu), ("gpu", self.platform.gpu)):
            if domain not in selected_domains:
                continue
            interfaces = section.get("interfaces", {})
            if not isinstance(interfaces, dict):
                raise ProbeError(f"platform {domain}.interfaces must be a mapping")
            for name, definition in interfaces.items():
                if not isinstance(definition, dict):
                    raise ProbeError(f"platform {domain}.interfaces.{name} must be a mapping")
                capabilities[f"{domain}.{name}"] = self._probe_interface(
                    f"{domain}.{name}", definition, include_values=full
                )

        cpu_topology = (
            self._probe_cpu_topology(include_values=full)
            if "cpu" in selected_domains
            else {"core_count": 0, "cores": [], "source": "not-requested"}
        )
        thermal = self._probe_thermal(include_values=full)
        if "cpu" in selected_domains and thermal.get("cpu", {}).get("paths"):
            capabilities["cpu.temperature"] = thermal["cpu"]
        if "gpu" in selected_domains and thermal.get("gpu", {}).get("paths"):
            capabilities["gpu.temperature"] = thermal["gpu"]

        identity_required_missing = list(platform_identity["required_missing"])
        required_missing = sorted(
            identity_required_missing
            + [name for name, record in capabilities.items() if record.get("required") and not record.get("available")]
        )
        return {
            "schema_version": 1,
            "producer": {"name": "vmin_judge", "component": "PlatformProbe"},
            "platform": self.platform.name,
            "platform_fingerprint": self.platform.fingerprint,
            "probe_domains": sorted(selected_domains),
            "device": dict(self.backend.identity()),
            "platform_identity": platform_identity,
            "identity_required_missing": identity_required_missing,
            "cpu_topology": cpu_topology,
            "thermal_zones": thermal,
            "capabilities": capabilities,
            "required_missing": required_missing,
            "supported": not required_missing,
        }

    def require(self, probe_result: Mapping[str, Any], names: list[str]) -> None:
        capabilities = probe_result.get("capabilities", {})
        missing = [name for name in names if not capabilities.get(name, {}).get("available")]
        if missing:
            raise ProbeError(f"required capabilities unavailable: {', '.join(sorted(missing))}")

    def apply_required_scope(
        self,
        probe_result: dict[str, Any],
        names: list[str],
        *,
        scope: str,
    ) -> dict[str, Any]:
        """Gate execution on an explicit profile/command requirement set.

        The complete platform requirement result is retained for audit, while
        ``supported`` and ``required_missing`` become scoped to the requested
        operation.
        """
        capabilities = probe_result.get("capabilities", {})
        required = sorted(set(names))
        missing = sorted(
            set(probe_result.get("identity_required_missing", []))
            | {name for name in required if not capabilities.get(name, {}).get("available")}
        )
        probe_result["platform_required_missing"] = list(probe_result.get("required_missing", []))
        probe_result["required_scope"] = {"name": scope, "capabilities": required}
        probe_result["required_missing"] = missing
        probe_result["supported"] = not missing
        return probe_result

    def apply_requested_value_preflight(
        self,
        probe_result: dict[str, Any],
        capability_name: str,
        requested: str,
    ) -> dict[str, Any]:
        record = probe_result.get("capabilities", {}).get(capability_name, {})
        configured = bool(record.get("available_values_configured", False))
        required = bool(record.get("require_requested_value", False))
        supported_by_path = record.get("supported_values_by_path", {})
        governor_paths = list(record.get("paths", []))
        unsupported_paths: list[str] = []
        unverified_paths: list[str] = []
        for path in governor_paths:
            supported = supported_by_path.get(path)
            if not supported:
                unverified_paths.append(path)
            elif requested not in supported:
                unsupported_paths.append(path)
        verified = configured and bool(governor_paths) and not unsupported_paths and not unverified_paths
        record["requested_value_preflight"] = {
            "requested": requested,
            "configured": configured,
            "required": required,
            "verified": verified,
            "unsupported_paths": unsupported_paths,
            "unverified_paths": unverified_paths,
        }
        requirement_name = f"{capability_name}.requested_value"
        if required and not verified:
            probe_result["required_missing"] = sorted(
                set(probe_result.get("required_missing", [])) | {requirement_name}
            )
            probe_result["supported"] = False
        return probe_result

    def _probe_platform_identity(self) -> dict[str, Any]:
        identity = self.platform.identity
        default_required = bool(identity.get("required", False))
        configured_fields = identity.get("fields", {})
        fields: dict[str, Any] = {}
        required_missing: list[str] = []
        for field_name, definition in configured_fields.items():
            path = str(PurePosixPath(str(definition["path"])))
            parser = str(definition.get("parser", "text"))
            accepted = [str(value) for value in definition.get("accepted", [])]
            required = bool(definition.get("required", default_required))
            actual: str | None = None
            error: str | None = None
            if not self.backend.exists(path):
                error = "source path not found"
            else:
                try:
                    raw = self.backend.read_text(path)
                    if parser == "kernel_cmdline":
                        key = str(definition["key"])
                        tokens = {
                            token.partition("=")[0]: token.partition("=")[2]
                            for token in raw.split()
                            if "=" in token
                        }
                        actual = tokens.get(key)
                        if actual is None:
                            error = f"key not found: {key}"
                    else:
                        actual = raw.strip() or None
                        if actual is None:
                            error = "identity value is empty"
                except OSError as exc:
                    error = str(exc)
            case_sensitive = bool(definition.get("case_sensitive", False))
            comparable_actual = actual if case_sensitive or actual is None else actual.casefold()
            comparable_accepted = accepted if case_sensitive else [value.casefold() for value in accepted]
            matched = comparable_actual in comparable_accepted
            capability_name = f"platform.identity.{field_name}"
            if required and not matched:
                required_missing.append(capability_name)
            fields[field_name] = {
                "path": path,
                "parser": parser,
                "key": definition.get("key"),
                "accepted": accepted,
                "actual": actual,
                "available": actual is not None,
                "matched": matched,
                "required": required,
                "error": error,
            }
        return {
            "configured": bool(configured_fields),
            "required": default_required,
            "matched": not required_missing,
            "fields": fields,
            "required_missing": sorted(required_missing),
        }

    def _probe_interface(self, name: str, definition: Mapping[str, Any], *, include_values: bool) -> dict[str, Any]:
        candidates = definition.get("candidates", [])
        if isinstance(candidates, str):
            candidates = [candidates]
        if not isinstance(candidates, list) or not all(isinstance(item, str) for item in candidates):
            raise ProbeError(f"{name}.candidates must be a list of strings")
        paths: list[str] = []
        for candidate in candidates:
            matched = self.backend.glob(candidate) if self._has_glob(candidate) else ([candidate] if self.backend.exists(candidate) else [])
            for path in matched:
                normalized = str(PurePosixPath(path))
                if normalized not in paths:
                    paths.append(normalized)
        values: dict[str, Any] = {}
        unreadable: list[str] = []
        if include_values:
            for path in paths:
                try:
                    values[path] = self._normalize_value(self.backend.read_text(path), definition.get("unit"))
                except (OSError, ValueError) as exc:
                    unreadable.append(path)
                    values[path] = {"error": str(exc)}
        available_value_candidates = definition.get("available_values_candidates", [])
        if isinstance(available_value_candidates, str):
            available_value_candidates = [available_value_candidates]
        if not isinstance(available_value_candidates, list) or not all(
            isinstance(item, str) for item in available_value_candidates
        ):
            raise ProbeError(f"{name}.available_values_candidates must be a list of strings")
        available_value_paths: list[str] = []
        for candidate in available_value_candidates:
            matched = self.backend.glob(candidate) if self._has_glob(candidate) else (
                [candidate] if self.backend.exists(candidate) else []
            )
            for path in matched:
                normalized = str(PurePosixPath(path))
                if normalized not in available_value_paths:
                    available_value_paths.append(normalized)
        values_by_source: dict[str, list[str]] = {}
        available_value_errors: dict[str, str] = {}
        if include_values:
            for path in available_value_paths:
                try:
                    values_by_source[path] = list(dict.fromkeys(self.backend.read_text(path).split()))
                except OSError as exc:
                    available_value_errors[path] = str(exc)
        supported_values_by_path: dict[str, list[str]] = {}
        for path in paths:
            same_parent = [
                available_path
                for available_path in available_value_paths
                if PurePosixPath(available_path).parent == PurePosixPath(path).parent
            ]
            if len(same_parent) == 1 and same_parent[0] in values_by_source:
                supported_values_by_path[path] = values_by_source[same_parent[0]]
            elif len(available_value_paths) == 1 and available_value_paths[0] in values_by_source:
                supported_values_by_path[path] = values_by_source[available_value_paths[0]]
        supported_values = sorted(
            {value for supported in supported_values_by_path.values() for value in supported}
        )
        return {
            "name": name,
            "available": bool(paths),
            "required": bool(definition.get("required", False)),
            "unit": definition.get("unit"),
            "derivation": definition.get("derivation"),
            "candidate_paths": list(candidates),
            "paths": paths,
            "readable": bool(paths) and not unreadable,
            "unreadable_paths": unreadable,
            "values": values,
            "available_values_configured": bool(available_value_candidates),
            "available_value_candidate_paths": list(available_value_candidates),
            "available_value_paths": available_value_paths,
            "available_values_by_source": values_by_source,
            "available_value_errors": available_value_errors,
            "supported_values": supported_values,
            "supported_values_by_path": supported_values_by_path,
            "require_requested_value": bool(definition.get("require_requested_value", False)),
            "provenance": "platform-profile+runtime-probe",
        }

    def _probe_cpu_topology(self, *, include_values: bool) -> dict[str, Any]:
        pattern = str(self.platform.cpu.get("topology_glob", "/sys/devices/system/cpu/cpu[0-9]*"))
        cores: list[dict[str, Any]] = []
        for path in self.backend.glob(pattern):
            match = re.search(r"/cpu(\d+)$", path.rstrip("/"))
            if not match:
                continue
            cpu_id = int(match.group(1))
            online_path = f"{path.rstrip('/')}/online"
            online: bool | None = True if cpu_id == 0 and not self.backend.exists(online_path) else None
            if include_values and self.backend.exists(online_path):
                try:
                    online = self.backend.read_text(online_path).strip() != "0"
                except OSError:
                    online = None
            cores.append({"cpu": cpu_id, "path": str(PurePosixPath(path)), "online": online})
        source = pattern
        if not cores and self.backend.exists("/proc/stat"):
            try:
                proc_stat = self.backend.read_text("/proc/stat")
            except OSError:
                proc_stat = ""
            for line in proc_stat.splitlines():
                match = re.match(r"^cpu(\d+)\s", line)
                if not match:
                    continue
                cpu_id = int(match.group(1))
                cores.append(
                    {
                        "cpu": cpu_id,
                        "path": f"/sys/devices/system/cpu/cpu{cpu_id}",
                        "online": True if cpu_id == 0 else None,
                    }
                )
            if cores:
                source = "/proc/stat"
        cores.sort(key=lambda item: item["cpu"])
        policies = self._probe_cpu_policies(include_values=include_values)
        policy_by_cpu: dict[str, list[int]] = {}
        for policy in policies:
            membership = policy["related_cpus"] or policy["affected_cpus"]
            for cpu_id in membership:
                policy_by_cpu.setdefault(str(cpu_id), []).append(policy["policy"])
        for core in cores:
            core["policies"] = list(policy_by_cpu.get(str(core["cpu"]), []))
        return {
            "core_count": len(cores),
            "cores": cores,
            "source_glob": pattern,
            "source": source,
            "policy_glob": str(
                self.platform.cpu.get(
                    "policy_glob", "/sys/devices/system/cpu/cpufreq/policy[0-9]*"
                )
            ),
            "policies": policies,
            "policy_by_cpu": policy_by_cpu,
        }

    def _probe_cpu_policies(self, *, include_values: bool) -> list[dict[str, Any]]:
        pattern = str(
            self.platform.cpu.get(
                "policy_glob", "/sys/devices/system/cpu/cpufreq/policy[0-9]*"
            )
        )
        policies: list[dict[str, Any]] = []
        for path in self.backend.glob(pattern):
            match = re.search(r"/policy(\d+)$", path.rstrip("/"))
            if not match:
                continue
            record: dict[str, Any] = {
                "policy": int(match.group(1)),
                "path": str(PurePosixPath(path)),
                "affected_cpus": [],
                "related_cpus": [],
                "errors": {},
            }
            if include_values:
                for field_name in ("affected_cpus", "related_cpus"):
                    field_path = f"{path.rstrip('/')}/{field_name}"
                    if not self.backend.exists(field_path):
                        record["errors"][field_name] = "not found"
                        continue
                    try:
                        record[field_name] = self._parse_cpu_list(self.backend.read_text(field_path))
                    except (OSError, ValueError) as exc:
                        record["errors"][field_name] = str(exc)
            record["mapped_cpus"] = record["related_cpus"] or record["affected_cpus"]
            record["readable"] = bool(record["mapped_cpus"])
            policies.append(record)
        policies.sort(key=lambda item: item["policy"])
        return policies

    @staticmethod
    def _parse_cpu_list(raw: str) -> list[int]:
        cpus: set[int] = set()
        normalized = raw.strip().replace(",", " ")
        if not normalized:
            return []
        for token in normalized.split():
            if "-" in token:
                start_text, separator, end_text = token.partition("-")
                if not separator or not start_text.isdigit() or not end_text.isdigit():
                    raise ValueError(f"invalid CPU range: {token}")
                start = int(start_text)
                end = int(end_text)
                if start > end:
                    raise ValueError(f"descending CPU range: {token}")
                cpus.update(range(start, end + 1))
            elif token.isdigit():
                cpus.add(int(token))
            else:
                raise ValueError(f"invalid CPU id: {token}")
        return sorted(cpus)

    def _probe_thermal(self, *, include_values: bool) -> dict[str, Any]:
        thermal = self.platform.thermal
        zone_glob = str(thermal.get("zone_glob", "/sys/class/thermal/thermal_zone*"))
        cpu_patterns = [str(item).lower() for item in thermal.get("cpu_type_patterns", ["cpu", "soc", "cluster"])]
        gpu_patterns = [str(item).lower() for item in thermal.get("gpu_type_patterns", ["gpu"])]
        groups: dict[str, list[dict[str, Any]]] = {"cpu": [], "gpu": [], "other": []}
        for zone in self.backend.glob(zone_glob):
            type_path = f"{zone.rstrip('/')}/type"
            temp_path = f"{zone.rstrip('/')}/temp"
            try:
                zone_type = self.backend.read_text(type_path).strip()
            except OSError:
                zone_type = "unknown"
            lowered = zone_type.lower()
            group = "gpu" if any(pattern in lowered for pattern in gpu_patterns) else (
                "cpu" if any(pattern in lowered for pattern in cpu_patterns) else "other"
            )
            record: dict[str, Any] = {
                "zone": str(PurePosixPath(zone)),
                "type": zone_type,
                "path": str(PurePosixPath(temp_path)),
                "temperature_unit_configured": self._configured_temperature_unit(zone_type),
            }
            if include_values and self.backend.exists(temp_path):
                try:
                    raw_text = self.backend.read_text(temp_path).strip()
                    normalized = self._normalize_temperature(raw_text, zone_type)
                    record.update(normalized)
                except (OSError, ValueError) as exc:
                    record["error"] = str(exc)
            groups[group].append(record)
        result: dict[str, Any] = {"zones": groups, "source_glob": zone_glob}
        for group in ("cpu", "gpu"):
            paths = [record["path"] for record in groups[group]]
            invalid_paths = [
                record["path"]
                for record in groups[group]
                if include_values and ("error" in record or record.get("valid") is False)
            ]
            result[group] = {
                "name": f"{group}.temperature",
                "available": bool(paths) and (not include_values or len(invalid_paths) < len(paths)),
                "required": True,
                "unit": "celsius",
                "paths": paths,
                "readable": bool(paths) and not invalid_paths,
                "invalid_paths": invalid_paths,
                "raw_values": {record["path"]: record.get("raw_value") for record in groups[group]},
                "parser_by_path": {
                    record["path"]: self._temperature_parser(record["temperature_unit_configured"])
                    for record in groups[group]
                },
                "values": {
                    record["path"]: record.get("temperature_c") if record.get("valid", True) else None
                    for record in groups[group]
                },
                "provenance": "thermal-zone-type-probe",
            }
        return result

    def _normalize_temperature(self, raw: str, zone_type: str) -> dict[str, Any]:
        thermal = self.platform.thermal
        configured_unit = self._configured_temperature_unit(zone_type)
        numeric = float(raw)
        if configured_unit == "auto":
            if abs(numeric) > 200.0:
                temperature_c = numeric / 1000.0
                applied_unit = "millidegree_celsius"
            else:
                temperature_c = numeric
                applied_unit = "degree_celsius"
        elif configured_unit in {"millidegree_celsius", "millicelsius"}:
            temperature_c = numeric / 1000.0
            applied_unit = "millidegree_celsius"
        elif configured_unit in {"degree_celsius", "celsius"}:
            temperature_c = numeric
            applied_unit = "degree_celsius"
        else:
            raise ProbeError(f"unsupported thermal temperature unit: {configured_unit}")

        plausible = thermal.get("plausible_range_c", {"min": -40.0, "max": 200.0})
        if not isinstance(plausible, dict):
            raise ProbeError("platform thermal.plausible_range_c must be a mapping")
        minimum = float(plausible.get("min", -40.0))
        maximum = float(plausible.get("max", 200.0))
        valid = minimum <= temperature_c <= maximum
        return {
            "raw_value": raw,
            "temperature_c": temperature_c,
            "temperature_unit_configured": configured_unit,
            "temperature_unit_applied": applied_unit,
            "valid": valid,
            "invalid_reason": None if valid else f"outside plausible range [{minimum}, {maximum}] celsius",
        }

    def _configured_temperature_unit(self, zone_type: str) -> str:
        thermal = self.platform.thermal
        configured_unit = str(thermal.get("temperature_unit", "millidegree_celsius")).lower()
        overrides = thermal.get("temperature_unit_by_type", {})
        if overrides and not isinstance(overrides, dict):
            raise ProbeError("platform thermal.temperature_unit_by_type must be a mapping")
        for pattern, unit in dict(overrides or {}).items():
            if str(pattern).lower() in zone_type.lower():
                configured_unit = str(unit).lower()
                break
        return configured_unit

    @staticmethod
    def _temperature_parser(configured_unit: str) -> str:
        if configured_unit == "auto":
            return "temperature_auto"
        if configured_unit in {"degree_celsius", "celsius"}:
            return "degree_celsius"
        return "millidegree_celsius"

    @staticmethod
    def _has_glob(path: str) -> bool:
        return any(character in path for character in "*?[")

    @staticmethod
    def _normalize_value(raw: str, unit: Any) -> Any:
        value = raw.strip()
        if unit in {"Hz", "kHz", "count", "us", "percent", "millidegree_celsius"}:
            try:
                numeric = float(value)
                return int(numeric) if numeric.is_integer() else numeric
            except ValueError:
                return value
        return value

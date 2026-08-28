"""Transport-independent platform and telemetry interface discovery."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Mapping

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

    def probe(self, *, full: bool = True) -> dict[str, Any]:
        capabilities: dict[str, Any] = {}
        for domain, section in (("cpu", self.platform.cpu), ("gpu", self.platform.gpu)):
            interfaces = section.get("interfaces", {})
            if not isinstance(interfaces, dict):
                raise ProbeError(f"platform {domain}.interfaces must be a mapping")
            for name, definition in interfaces.items():
                if not isinstance(definition, dict):
                    raise ProbeError(f"platform {domain}.interfaces.{name} must be a mapping")
                capabilities[f"{domain}.{name}"] = self._probe_interface(
                    f"{domain}.{name}", definition, include_values=full
                )

        cpu_topology = self._probe_cpu_topology(include_values=full)
        thermal = self._probe_thermal(include_values=full)
        if thermal.get("cpu", {}).get("paths"):
            capabilities["cpu.temperature"] = thermal["cpu"]
        if thermal.get("gpu", {}).get("paths"):
            capabilities["gpu.temperature"] = thermal["gpu"]

        required_missing = sorted(
            name for name, record in capabilities.items() if record.get("required") and not record.get("available")
        )
        return {
            "schema_version": 1,
            "producer": {"name": "vmin_judge", "component": "PlatformProbe"},
            "platform": self.platform.name,
            "platform_fingerprint": self.platform.fingerprint,
            "device": dict(self.backend.identity()),
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
        return {"core_count": len(cores), "cores": cores, "source_glob": pattern, "source": source}

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
            }
            if include_values and self.backend.exists(temp_path):
                try:
                    record["temperature_c"] = float(self.backend.read_text(temp_path).strip()) / 1000.0
                except (OSError, ValueError) as exc:
                    record["error"] = str(exc)
            groups[group].append(record)
        result: dict[str, Any] = {"zones": groups, "source_glob": zone_glob}
        for group in ("cpu", "gpu"):
            paths = [record["path"] for record in groups[group]]
            result[group] = {
                "name": f"{group}.temperature",
                "available": bool(paths),
                "required": True,
                "unit": "celsius",
                "paths": paths,
                "readable": all("error" not in record for record in groups[group]),
                "values": {record["path"]: record.get("temperature_c") for record in groups[group]},
                "provenance": "thermal-zone-type-probe",
            }
        return result

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

"""Static target extension points; native CPU/GPU behavior stays in its existing path."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .artifact_store import atomic_write_json, sha256_file
from .config_loader import ConfigError, document_sha256
from .deployment import AssetSpec


@dataclass(frozen=True)
class TargetAdapter:
    name: str
    unit_metric: str
    throughput: str
    latency: str
    throughput_unit: str
    per_unit_log: str

    def metrics(self, summary: Mapping[str, Any]) -> dict[str, Any]:
        view = {"target": self.name, "work_units": summary.get(self.unit_metric),
                "throughput": summary.get(self.throughput), "throughput_unit": self.throughput_unit,
                "host_latency_p99_ms": summary.get(self.latency)}
        if self.name == "npu":
            view["device_latency_p99_ms"] = summary.get("device_inference_time_ms_p99") if summary.get("device_time_valid") is True else None
            view["execution_placement"] = "unknown"  # Vendor evidence is evaluated separately.
        return view


NPU_MODELS = frozenset({"kws01", "ic01", "ad01", "sww01"})
NPU_BACKENDS = frozenset({"null", "reference_cpu", "tflite_delegate"})


def _number(data, name, default, minimum, *, integer=False, maximum=None):
    value = data.setdefault(name, default)
    if (type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value)
            or value < minimum or (maximum is not None and value > maximum)):
        raise ConfigError(f"NPU {name}: invalid finite {'integer' if integer else 'number'}")


class NpuAdapter(TargetAdapter):
    def validate_config(self, data: dict[str, Any]) -> dict[str, Any]:
        allowed = {"profile", "api", "mode", "backend", "input_manifest", "runtime_library", "delegate_library",
                   "delegate_options", "duration", "inferences", "warmup_inferences", "timeout", "inference_timeout_ms",
                   "verify_mode", "verify_interval", "success_log_interval", "heartbeat_interval", "output_format",
                   "summary_only", "per_inference_log", "golden_checksum", "fail_fast", "generate_golden", "seed",
                   "simulated_latency_ms"}
        unknown = set(data) - allowed
        if unknown:
            raise ConfigError(f"unknown NPU configuration fields/aliases: {sorted(unknown)}")
        for key, expected in (("api", "npu"), ("mode", "inference"), ("output_format", "jsonl")):
            if data.get(key) != expected:
                raise ConfigError(f"NPU {key} must be {expected}")
        if data.get("profile") not in NPU_MODELS | {"framework_smoke"} or data.get("backend") not in NPU_BACKENDS:
            raise ConfigError("unsupported NPU model profile/backend")
        if data["backend"] == "reference_cpu" and data["profile"] != "framework_smoke":
            raise ConfigError("reference_cpu is a framework_smoke fixture")
        if data.get("verify_mode") not in {"none", "checksum", "crc"}:
            raise ConfigError("unsupported NPU verify_mode")
        for key in ("input_manifest", "runtime_library", "delegate_library", "delegate_options", "golden_checksum"):
            if key in data and (not isinstance(data[key], str) or any(c in data[key] for c in '\r\n\0')):
                raise ConfigError(f"NPU {key} must be a delimiter-free string")
        if not data.get("input_manifest"):
            raise ConfigError("NPU input_manifest is required")
        for key, default, minimum, maximum in (("inferences", 0, 0, 2**64-1), ("warmup_inferences", 1, 0, 2**64-1),
                  ("verify_interval", 1, 1 if data["verify_mode"] != "none" else 0, 2**32-1),
                  ("success_log_interval", 60, 0, 2**32-1), ("inference_timeout_ms", 5000, 1, 2**32-1),
                  ("seed", 0x123456789abcdef0, 0, 2**64-1), ("simulated_latency_ms", 0, 0, 2**32-1)):
            _number(data, key, default, minimum, integer=True, maximum=maximum)
        for key, default, minimum in (("duration", 60, 0), ("timeout", 95, 0.001), ("heartbeat_interval", 1, 0.001)):
            _number(data, key, default, minimum)
        if data["duration"] == 0 and data["inferences"] == 0:
            raise ConfigError("NPU duration or inferences must be positive")
        for key, default in (("summary_only", False), ("per_inference_log", False), ("fail_fast", True), ("generate_golden", False)):
            if type(data.setdefault(key, default)) is not bool:
                raise ConfigError(f"NPU {key} must be boolean")
        if data["summary_only"]:
            raise ConfigError("live NPU requires summary_only=false")
        checksum = data.get("golden_checksum", "")
        width = 8 if data["verify_mode"] == "crc" else 16
        if checksum and not re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]{1,"+str(width)+"}", checksum):
            raise ConfigError("invalid NPU golden checksum")
        if data["backend"] == "tflite_delegate" and not all(data.get(k) for k in ("runtime_library", "delegate_library")):
            raise ConfigError("NPU delegate backend requires runtime_library and delegate_library")
        return data

    def plan_assets(self, paths, profile, config_path, config):
        """Confine and hash every manifest asset, preserve relative layout, materialize device config."""
        manifest_path = paths.resolve_input(config["input_manifest"], owner=config_path)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ConfigError(f"invalid NPU manifest: {exc}") from exc
        if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1:
            raise ConfigError("NPU manifest schema_version must be 1")
        registered = json.loads(paths.resolve_resource("config/npu_models.json").read_text())[config["profile"]]
        if not isinstance(manifest.get("profile"), dict):
            raise ConfigError("NPU manifest.profile must be a mapping")
        if manifest["profile"].get("id") != config["profile"] or manifest["profile"].get("version") != "1":
            raise ConfigError("NPU manifest profile does not match configuration")
        for key in ("inputs", "outputs"):
            if not isinstance(manifest.get(key), list) or not manifest[key] or not all(isinstance(item, dict) for item in manifest[key]):
                raise ConfigError(f"NPU manifest.{key} must be a nonempty list of tensor mappings")
            descriptors = [{k: v for k, v in item.items() if k not in {"file", "sha256"}} for item in manifest.get(key, [])]
            if document_sha256({"descriptors": descriptors}) != document_sha256({"descriptors": registered[key]}):
                raise ConfigError(f"NPU {key} tensor descriptors do not match registered model")
        base = manifest_path.parent.resolve()
        asset_root = self._confined(base, manifest.get("asset_root", "."), directory=True)
        remote_base = paths.remote(f"npu/{profile.name}")
        assets = [AssetSpec(manifest_path, remote_base / "manifest.json", kind="npu-manifest")]
        if not isinstance(manifest.get("model"), dict) or manifest["model"].get("format") != ("none" if config["profile"] == "framework_smoke" else "tflite"):
            raise ConfigError("NPU manifest model format does not match registered profile")
        declarations = [manifest["model"], *manifest["inputs"]]
        if config["profile"] == "framework_smoke" and manifest.get("model", {}).get("format") == "none":
            declarations = list(manifest.get("inputs", []))
        for item in declarations:
            if not item.get("file"):
                raise ConfigError("NPU manifest model/input file is required")
            local = self._confined(asset_root, item["file"])
            expected = item.get("sha256")
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or sha256_file(local) != expected:
                raise ConfigError(f"NPU manifest hash mismatch: {local}")
            assets.append(AssetSpec(local, remote_base / local.relative_to(base).as_posix(), kind="npu-model" if item is manifest.get("model") else "npu-input"))
        effective = copy.deepcopy(config)
        effective["input_manifest"] = str(remote_base / "manifest.json")
        installed = self.installed_libraries(profile, config)
        for key in ("runtime_library", "delegate_library"):
            if not config.get(key):
                continue
            if key in installed:
                effective[key] = installed[key]["path"]
                continue
            local = paths.resolve_input(config[key], owner=config_path)
            self.check_binary(local, profile)
            remote = remote_base / "lib" / local.name
            assets.append(AssetSpec(local, remote, kind="npu-" + key))
            effective[key] = str(remote)
        digest = document_sha256(effective)
        generated = paths.resolve_output(f"effective-configs/{profile.name}/{digest}.json", create_parent=True)
        atomic_write_json(generated, effective)
        assets.append(AssetSpec(generated, paths.remote(f"configs/{profile.name}.json"), kind="workload-config"))
        destinations = [str(a.remote) for a in assets]
        if len(set(destinations)) != len(destinations):
            raise ConfigError("duplicate NPU asset destination")
        return assets, generated

    @staticmethod
    def _confined(root, value, directory=False):
        if not isinstance(value, str) or not value or '\\' in value or any(c in value for c in '\r\n\0'):
            raise ConfigError("invalid NPU manifest asset path")
        posix = PurePosixPath(value)
        if posix.is_absolute() or '..' in posix.parts or ':' in value:
            raise ConfigError("NPU manifest asset must be a confined relative path")
        local = (root / value).resolve(strict=True)
        if not local.is_relative_to(root) or (not directory and not local.is_file()):
            raise ConfigError("NPU asset escapes its declared root or is not a file")
        return local

    @staticmethod
    def check_binary(path, profile):
        if profile.workload.get("target_abi") == "host-test":
            if profile.raw.get("purpose") != "smoke":
                raise ConfigError("host-test ABI requires purpose=smoke")
            return
        with path.open('rb') as stream:
            header = stream.read(20)
        if len(header) < 20 or header[:6] != b'\x7fELF\x02\x01' or int.from_bytes(header[18:20], 'little') != 183:
            raise ConfigError(f"NPU device asset must be an AArch64 ELF (host DLL/EXE is not a device library): {path}")

    @staticmethod
    def installed_libraries(profile, config):
        installed = profile.workload.get("installed_libraries", {})
        if not isinstance(installed, dict) or set(installed) - {"runtime_library", "delegate_library"}:
            raise ConfigError("invalid installed_libraries mapping")
        for key, entry in installed.items():
            if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                    or not PurePosixPath(entry["path"]).is_absolute() or '..' in PurePosixPath(entry["path"]).parts
                    or any(c in entry["path"] for c in '\r\n\0') or entry["path"] != config.get(key)
                    or not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
                raise ConfigError(f"installed {key} needs an absolute configured path and pinned SHA-256")
        return installed

    def qualification_evidence(self, paths, profile, config, assets):
        if config["backend"] != "tflite_delegate" or profile.workload.get("target_abi") == "host-test":
            raise ConfigError("NPU qualification requires a real delegate backend and device ABI")
        evidence_value = profile.workload.get("placement_evidence")
        if not isinstance(evidence_value, str):
            raise ConfigError("NPU qualification requires vendor placement_evidence; fallback_used=false is insufficient")
        evidence_path = paths.resolve_input(evidence_value, owner=profile.source_path)
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ConfigError(f"invalid vendor placement evidence: {exc}") from exc
        expected = {str(asset.remote): sha256_file(asset.local) for asset in assets if asset.kind != "agent"}
        expected.update({entry["path"]: entry["sha256"] for entry in self.installed_libraries(profile, config).values()})
        if (not isinstance(evidence, dict) or evidence.get("schema_version") != 1 or evidence.get("execution") != "full_npu"
                or not all(isinstance(evidence.get(k), str) and evidence[k] for k in ("vendor", "tool", "captured_at"))
                or evidence.get("asset_sha256") != expected):
            raise ConfigError("vendor placement evidence must prove full_npu for the exact deployed asset set")
        if not isinstance(evidence.get("diagnostic_file"), str) or not evidence["diagnostic_file"]:
            raise ConfigError("vendor placement evidence needs a diagnostic_file")
        diagnostic = paths.resolve_input(evidence["diagnostic_file"], owner=evidence_path)
        if evidence.get("diagnostic_sha256") != sha256_file(diagnostic):
            raise ConfigError("vendor diagnostic hash mismatch")
        return {"status": "full_npu", "evidence_sha256": sha256_file(evidence_path),
                "diagnostic_sha256": sha256_file(diagnostic), "vendor": evidence["vendor"], "tool": evidence["tool"]}


ADAPTERS = {
    "cpu": TargetAdapter("cpu", "batch_count", "operations_per_sec_avg", "batch_time_ms_p99", "operations/s", "per_batch_log"),
    "gpu": TargetAdapter("gpu", "frame_count", "fps_avg", "frame_time_p99_ms", "frames/s", "per_frame_log"),
    "npu": NpuAdapter("npu", "inference_count", "inferences_per_sec_avg", "host_inference_time_ms_p99", "inferences/s", "per_inference_log"),
}


def get_target(name: str) -> TargetAdapter:
    try:
        return ADAPTERS[name]
    except KeyError as exc:
        raise ConfigError(f"unsupported target: {name}") from exc

from __future__ import annotations

import json
import tempfile
import unittest
import warnings
from pathlib import Path

from src.config_loader import (
    ConfigError,
    PlatformConfig,
    ProfileConfig,
    document_sha256,
    load_document,
    validate_workload_config,
)


def valid_profile() -> dict:
    return {
        "schema_version": 1,
        "name": "cpu_test",
        "target": "cpu",
        "platform": "kirin9020",
        "workload": {"binary": "bin/cpu", "argv": ["--profile", "mixed"]},
        "environment": {},
        "baseline": None,
        "telemetry": {"required": ["cpu.frequency"]},
        "kernel_monitor": "critical",
    }


class ConfigLoaderTests(unittest.TestCase):
    def test_verification_settings_are_canonical_and_do_not_rewrite_source(self) -> None:
        for target in ("cpu", "gpu"):
            source = Path(__file__).parents[1] / "config" / "workloads" / f"{target}_qualification_kirin9030.json"
            document = json.loads(source.read_text(encoding="utf-8"))
            document.pop("verify_interval", None)
            document.pop("checksum_interval", None)
            document.pop("success_log_interval", None)
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "workload.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                normalized = validate_workload_config(path, target)
                self.assertEqual(normalized["verify_interval"], 1)
                self.assertEqual(normalized["success_log_interval"], 60)
                self.assertNotIn("verify_interval", json.loads(path.read_text(encoding="utf-8")))
                document["checksum_interval"] = 3
                path.write_text(json.dumps(document), encoding="utf-8")
                with warnings.catch_warnings(record=True) as notices:
                    warnings.simplefilter("always")
                    normalized = validate_workload_config(path, target)
                self.assertEqual(normalized["verify_interval"], 3)
                self.assertNotIn("checksum_interval", normalized)
                self.assertTrue(any("deprecated" in str(item.message) for item in notices))

    def test_rejects_ambiguous_invalid_or_suppressed_verification_contract(self) -> None:
        for target in ("cpu", "gpu"):
            source = Path(__file__).parents[1] / "config" / "workloads" / f"{target}_qualification_kirin9030.json"
            base = json.loads(source.read_text(encoding="utf-8"))
            base.pop("checksum_interval", None)
            base["verify_interval"] = 1
            cases = [
                ({"checksum_interval": 1}, "not both"),
                ({"verify-interval": 2}, "CLI spelling"),
                ({"verify_interval": 0}, "verify_interval"),
                ({"verify_interval": True}, "verify_interval"),
                ({"verify_interval": 1.0}, "verify_interval"),
                ({"verify_interval": "1"}, "verify_interval"),
                ({"verify_interval": 2**32}, "verify_interval"),
                ({"success_log_interval": -1}, "success_log_interval"),
                ({"success_log_interval": False}, "success_log_interval"),
                ({"success_log_interval": 1.5}, "success_log_interval"),
                ({"summary_only": True}, "live heartbeat"),
                ({"summary_only": "false"}, "boolean"),
                ({"per_batch_log" if target == "cpu" else "per_frame_log": 1}, "boolean"),
            ]
            for update, error in cases:
                with self.subTest(target=target, update=update), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "workload.json"
                    path.write_text(json.dumps({**base, **update}), encoding="utf-8")
                    with self.assertRaisesRegex(ConfigError, error):
                        validate_workload_config(path, target)

    def test_disabled_verification_and_success_logging_accept_zero(self) -> None:
        for target in ("cpu", "gpu"):
            source = Path(__file__).parents[1] / "config" / "workloads" / f"{target}_smoke.json"
            document = json.loads(source.read_text(encoding="utf-8"))
            document.pop("checksum_interval", None)
            document.update(verify_interval=0, success_log_interval=0)
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "workload.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                normalized = validate_workload_config(path, target)
                self.assertEqual(normalized["verify_interval"], 0)
                self.assertEqual(normalized["success_log_interval"], 0)

    def test_rejects_unimplemented_vulkan_modes_and_scheduling(self) -> None:
        source = Path(__file__).parents[1] / "config" / "workloads" / "gpu_smoke.json"
        base = json.loads(source.read_text(encoding="utf-8"))
        for update, error in (
            ({"mode": "compute"}, "offscreen"),
            ({"shader": "none"}, "shader"),
            ({"rt_format": "RGBA16F"}, "RGBA8"),
            ({"samples": 4}, "samples=1"),
            ({"samples": True}, "samples=1"),
            ({"duty_cycle": 0.5}, "does not implement"),
            ({"duty_cycle": float("nan")}, "finite number"),
        ):
            with self.subTest(update=update), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "workload.json"
                path.write_text(json.dumps({**base, **update}), encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, error):
                    validate_workload_config(path, "gpu")

    def test_vulkan_validation_respects_unoverridden_builtin_profile_defaults(self) -> None:
        source = Path(__file__).parents[1] / "config" / "workloads" / "gpu_smoke.json"
        base = json.loads(source.read_text(encoding="utf-8"))
        base.pop("mode", None)
        base.pop("duty_cycle", None)
        for profile, error in (("alu", "offscreen"), ("sfu", "offscreen"), ("game_light", "does not implement"), ("burst", "does not implement"), ("idle", "does not implement")):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "workload.json"
                document = {**base, "profile": profile}
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, error):
                    validate_workload_config(path, "gpu")
                document.update(mode="offscreen", duty_cycle=1)
                path.write_text(json.dumps(document), encoding="utf-8")
                validate_workload_config(path, "gpu")

    def test_positive_smoke_configs_are_error_only(self) -> None:
        config_root = Path(__file__).parents[1] / "config" / "workloads"
        expected_modes = {
            "cpu_smoke.json": "none",
            "gpu_smoke.json": "none",
            "cpu_mixed_big4.json": "checksum",
            "gpu_vulkan_mixed.json": "golden-image",
            "cpu_qualification_kirin9030.json": "checksum",
            "gpu_qualification_kirin9030.json": "golden-image",
        }
        for name, expected in expected_modes.items():
            with self.subTest(name=name):
                document = json.loads((config_root / name).read_text(encoding="utf-8"))
                self.assertEqual(document["verify_mode"], expected)

    def test_all_bundled_workloads_have_target_compatible_schema(self) -> None:
        config_root = Path(__file__).parents[1] / "config" / "workloads"
        for path in sorted(config_root.glob("*.json")):
            target = "gpu" if path.name.startswith("gpu_") else "cpu"
            with self.subTest(path=path.name):
                document = validate_workload_config(path, target)
                self.assertEqual(document["output_format"], "jsonl")

    def test_workload_validation_rejects_target_and_golden_path_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gpu.json"
            document = {
                "api": "cpu", "verify_mode": "golden-image", "output_format": "jsonl",
                "duration": 1, "timeout": 2, "iterations": 1, "heartbeat_interval": 1,
                "warmup": 0, "width": 1, "height": 1, "gpu_timeout_ms": 1,
                "golden_file": "relative.rgba",
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "api must be 'vulkan'"):
                validate_workload_config(path, "gpu")
            document["api"] = "vulkan"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "absolute path"):
                validate_workload_config(path, "gpu")

    def test_json_load_and_profile_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text(json.dumps(valid_profile()), encoding="utf-8")
            profile = ProfileConfig.from_file(path)
            self.assertEqual(profile.target, "cpu")
            self.assertEqual(profile.fingerprint, document_sha256(valid_profile()))

    def test_rejects_unsupported_schema(self) -> None:
        data = valid_profile()
        data["schema_version"] = 2
        with self.assertRaisesRegex(ConfigError, "unsupported major version"):
            ProfileConfig.from_mapping(data, source_path=Path("profile.json"))

    def test_rejects_shell_string_argv(self) -> None:
        data = valid_profile()
        data["workload"]["argv"] = "--profile mixed"
        with self.assertRaisesRegex(ConfigError, "list of strings"):
            ProfileConfig.from_mapping(data, source_path=Path("profile.json"))

    def test_scheduler_requirements_are_preferred_profile_metadata(self) -> None:
        data = valid_profile()
        data.pop("environment")
        data["scheduler_requirements"] = {"governor": "performance", "affinity": "4-7"}
        profile = ProfileConfig.from_mapping(data, source_path=Path("profile.json"))
        self.assertEqual(
            profile.environment,
            {"governor": "performance", "affinity": "4-7"},
        )

    def test_rejects_scheduler_requirements_mixed_with_legacy_environment(self) -> None:
        data = valid_profile()
        data["scheduler_requirements"] = {}
        with self.assertRaisesRegex(ConfigError, "use scheduler_requirements or legacy environment"):
            ProfileConfig.from_mapping(data, source_path=Path("profile.json"))

    def test_rejects_empty_profile_platform(self) -> None:
        data = valid_profile()
        data["platform"] = " "
        with self.assertRaisesRegex(ConfigError, "platform: must not be empty"):
            ProfileConfig.from_mapping(data, source_path=Path("profile.json"))

    def test_rejects_unknown_document_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.conf"
            path.write_text("schema_version=1", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "unsupported configuration extension"):
                load_document(path)

    def test_platform_rejects_non_generic_serial_shapes(self) -> None:
        base = {
            "schema_version": 1,
            "name": "serial-test",
            "transport": {},
            "serial": {},
            "cpu": {},
            "gpu": {},
            "thermal": {},
        }
        for serial, expected in (
            ({"baudrate": 0}, "positive integer"),
            ({"uart_candidates": "/dev/ttyVendor0"}, "list of non-empty strings"),
            ({"protocol": "jsonl"}, "uart-v2"),
            ({"max_frame_bytes": 10}, "64 to 4096"),
            ({"tail_guard_bytes": -1}, "0 to 4096"),
            ({"tail_guard_bytes": 4097}, "0 to 4096"),
            ({"safe_utilization": 1.5}, "at most 1"),
            ({"relay": "binary"}, "expected mapping"),
        ):
            data = dict(base)
            data["serial"] = serial
            with self.subTest(serial=serial), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "platform.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, expected):
                    PlatformConfig.from_file(path)

    def test_platform_rejects_invalid_thermal_unit_and_range(self) -> None:
        base = {
            "schema_version": 1,
            "name": "thermal-test",
            "transport": {},
            "serial": {},
            "cpu": {},
            "gpu": {},
        }
        for thermal, expected in (
            ({"temperature_unit": "guess"}, "temperature_unit"),
            ({"plausible_range_c": {"min": 80, "max": 20}}, "min must be less than max"),
        ):
            data = dict(base)
            data["thermal"] = thermal
            with self.subTest(thermal=thermal), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "platform.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, expected):
                    PlatformConfig.from_file(path)

    def test_platform_rejects_incomplete_required_identity(self) -> None:
        data = {
            "schema_version": 1,
            "name": "identity-test",
            "identity": {"required": True, "fields": {}},
            "transport": {},
            "serial": {},
            "cpu": {},
            "gpu": {},
            "thermal": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "platform.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "at least one field"):
                PlatformConfig.from_file(path)

    def test_platform_rejects_empty_kernel_cmdline_identity_key(self) -> None:
        data = {
            "schema_version": 1,
            "name": "identity-test",
            "identity": {
                "required": True,
                "fields": {
                    "hardware": {
                        "path": "/proc/cmdline",
                        "parser": "kernel_cmdline",
                        "key": " ",
                        "accepted": ["Kirin9030"],
                    }
                },
            },
            "transport": {},
            "serial": {},
            "cpu": {},
            "gpu": {},
            "thermal": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "platform.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "non-empty string"):
                PlatformConfig.from_file(path)


if __name__ == "__main__":
    unittest.main()

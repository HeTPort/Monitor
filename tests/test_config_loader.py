from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.config_loader import ConfigError, PlatformConfig, ProfileConfig, document_sha256, load_document


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

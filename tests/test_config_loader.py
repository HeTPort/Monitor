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


if __name__ == "__main__":
    unittest.main()

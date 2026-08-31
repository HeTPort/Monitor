from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from src.cli_commands import (
    _apply_platform_serial,
    _apply_saved_pairing,
    _concise_reason,
    cmd_smoke,
)
from src.events import build_event, encode_event
from src.path_resolver import PathResolver


ROOT = Path(__file__).parents[1]
MAIN = ROOT / "main.py"


class CLITests(unittest.TestCase):
    def run_cli(self, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(MAIN), *arguments],
            cwd=cwd or ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
        )

    def test_version_and_public_help(self) -> None:
        version = self.run_cli("--version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn("config=1", version.stdout)
        help_result = self.run_cli("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for command in ("probe", "deploy", "verify-deployment", "telemetry", "smoke", "golden", "calibrate", "baseline", "run", "collect", "report"):
            self.assertIn(command, help_result.stdout)
        self.assertNotIn("execute", help_result.stdout)

        smoke_help = self.run_cli("smoke", "--help")
        self.assertEqual(smoke_help.returncode, 0, smoke_help.stderr)
        self.assertIn("DEPRECATED", smoke_help.stdout)

        run_help = self.run_cli("run", "--help")
        self.assertIn("Optional approved baseline", run_help.stdout)
        self.assertIn("--test-id", run_help.stdout)
        self.assertNotIn("--no-deploy", run_help.stdout)
        self.assertNotIn("--run-id", run_help.stdout)

    def test_pair_help_exposes_optional_platform_serial_config(self) -> None:
        result = self.run_cli("pair", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--platform", result.stdout)
        self.assertIn("UART", result.stdout)
        self.assertIn("candidates", result.stdout)
        for removed in ("--channel", "--device-port", "--pc-port", "--monitor"):
            self.assertNotIn(removed, result.stdout)

    def test_baseline_list_is_machine_readable_without_hardware(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_cli("--state-dir", tmp, "--json", "baseline", "list")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["count"], 0)

    def test_smoke_reports_minimum_closed_loop_and_retained_spool(self) -> None:
        args = Namespace(repeat=1, profile="cpu_smoke_kirin9030", baseline="unexpected")
        with patch("src.cli_commands._execute_run_command", return_value=0) as execute:
            exit_code = cmd_smoke(args)
        self.assertEqual(exit_code, 0)
        self.assertIsNone(args.baseline)
        execute.assert_called_once_with(args, smoke=True)

    def test_validate_reports_resolved_override_path_and_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp)
            platform_path = override / "platform.json"
            platform_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "override-platform",
                        "transport": {},
                        "serial": {},
                        "cpu": {},
                        "gpu": {},
                        "thermal": {},
                    }
                ),
                encoding="utf-8",
            )
            workload = override / "workload.bin"
            workload.write_bytes(b"workload")
            workload_config = override / "workload.json"
            workload_config.write_text("{}", encoding="utf-8")
            profile_path = override / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "override-profile",
                        "target": "cpu",
                        "platform": str(platform_path),
                        "workload": {"binary": "workload.bin", "config": "workload.json"},
                        "environment": {},
                        "telemetry": {"required": [], "optional": []},
                        "kernel_monitor": "off",
                    }
                ),
                encoding="utf-8",
            )
            result = self.run_cli(
                "--config-dir",
                str(override),
                "--json",
                "validate",
                "--profile",
                str(profile_path),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            platforms = [item for item in payload["resolved_configs"] if item["kind"] == "platform"]
            self.assertEqual(Path(platforms[0]["path"]), platform_path.resolve())
            self.assertEqual(len(platforms[0]["sha256"]), 64)

    def test_saved_pairing_fills_missing_baud_without_overwriting_explicit_pc_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            (state / "pairing.conf").write_text(
                json.dumps({"pc_port": "COM5", "device_port": "/dev/ttyHW0", "baudrate": 9600}),
                encoding="utf-8",
            )
            paths = PathResolver(
                bundle_root=root,
                exe_root=root,
                state_root=state,
                output_root=root / "output",
                cwd=root,
            )
            args = Namespace(pc_serial="COM9", device_uart=None, baudrate=None)
            _apply_saved_pairing(args, paths)
            self.assertEqual(args.pc_serial, "COM9")
            self.assertEqual(args.device_uart, "/dev/ttyHW0")
            self.assertEqual(args.baudrate, 9600)

    def test_cmd_reason_keeps_actionable_environment_fields(self) -> None:
        reason = _concise_reason(
            {
                "scope": "agent",
                "code": "ENVIRONMENT_READBACK_FAILED",
                "seq": 2,
                "evidence": {
                    "phase": "readback",
                    "path": "/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq",
                    "requested": "1550000",
                    "actual": "2508000",
                    "unrelated_large_payload": {"ignored": True},
                },
            }
        )
        self.assertEqual(reason["requested"], "1550000")
        self.assertEqual(reason["actual"], "2508000")
        self.assertNotIn("unrelated_large_payload", reason)

    def test_selected_platform_supplies_serial_settings_only_when_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            platform_path = root / "vendor-platform.json"
            platform_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "vendor-platform",
                        "transport": {},
                        "serial": {"baudrate": 38400, "uart_candidates": ["/dev/ttyVendor2"]},
                        "cpu": {},
                        "gpu": {},
                        "thermal": {},
                    }
                ),
                encoding="utf-8",
            )
            paths = PathResolver(
                bundle_root=root,
                exe_root=root,
                state_root=root / "state",
                output_root=root / "output",
                cwd=root,
            )
            args = Namespace(device_uart=None, baudrate=None)
            _apply_platform_serial(args, paths, Namespace(platform=str(platform_path)))
            self.assertEqual(args.device_uart, "/dev/ttyVendor2")
            self.assertEqual(args.baudrate, 38400)

    def test_legacy_execute_command_is_removed(self) -> None:
        result = self.run_cli("execute", "--no-launch")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("invalid choice", result.stderr)

    def test_removed_ignored_options_are_rejected_by_parser(self) -> None:
        cases = (
            ("probe", "--platform", "kirin9030", "--refresh"),
            ("validate", "--offline"),
            ("validate", "--all"),
            ("validate", "--config", "old.conf"),
            ("validate", "--profiles", "old.yaml"),
            ("simulate", "--log-file", "old.log"),
            ("list-profiles", "--show-pending"),
            ("run", "--profile", "cpu_stress_kirin9030", "--run-id", "OLD"),
            ("run", "--profile", "cpu_stress_kirin9030", "--no-deploy"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_conflicting_selectors_fail_during_argument_parsing(self) -> None:
        cases = (
            ("run", "--profile", "cpu_stress_kirin9030", "--repeat", "2", "--attempt-id", "A-1"),
            ("simulate", "--events", "events.jsonl", "--raw-serial", "serial.raw"),
            ("deploy", "--profile", "cpu_stress_kirin9030", "--target", "cpu"),
            ("verify-deployment", "--profile", "cpu_stress_kirin9030", "--target", "cpu"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_deploy_and_collect_help_expose_only_safe_contracts(self) -> None:
        deploy = self.run_cli("deploy", "--help")
        self.assertEqual(deploy.returncode, 0, deploy.stderr)
        self.assertIn("--target {cpu,gpu,all} | --profile PROFILE", deploy.stdout)
        self.assertNotIn("--no-verify-hashes", deploy.stdout)

        collect = self.run_cli("collect", "--help")
        self.assertEqual(collect.returncode, 0, collect.stderr)
        self.assertIn("--test-id TEST_ID", collect.stdout)
        self.assertNotIn("--run-id", collect.stdout)
        self.assertNotIn("--keep-remote", collect.stdout)
        self.assertNotIn("--remote-run-dir", collect.stdout)

        unsafe_remove = self.run_cli(
            "--json", "collect", "--test-id", "T-1", "--remove-remote-after-verify"
        )
        self.assertEqual(unsafe_remove.returncode, 4, unsafe_remove.stderr)
        self.assertIn("requires --verify-hashes", unsafe_remove.stdout)

        invalid_clean = self.run_cli("--json", "deploy", "--target", "cpu", "--clean-stale")
        self.assertEqual(invalid_clean.returncode, 4, invalid_clean.stderr)
        self.assertIn("valid only with --target all", invalid_clean.stdout)

        invalid_baseline = self.run_cli(
            "--json", "deploy", "--target", "all", "--baseline", "some-baseline"
        )
        self.assertEqual(invalid_baseline.returncode, 4, invalid_baseline.stderr)
        self.assertIn("requires --profile or one specific --target", invalid_baseline.stdout)

    def test_golden_help_is_target_specific(self) -> None:
        cpu = self.run_cli("golden", "cpu", "--help")
        gpu = self.run_cli("golden", "gpu", "--help")
        self.assertIn("--accept-checksum", cpu.stdout)
        self.assertNotIn("--readback-name", cpu.stdout)
        self.assertIn("--readback-name", gpu.stdout)
        self.assertNotIn("--accept-checksum", gpu.stdout)

    def test_simulate_and_report_from_a_different_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / "events.jsonl"
            records = [
                build_event(run_id="cli-sim", seq=1, timestamp_ms=0, source="agent", event_type="agent_start", payload={}),
                build_event(
                    run_id="cli-sim", seq=2, timestamp_ms=1, source="cpu-workload", event_type="summary",
                    payload={"result": "PASS", "exit_code": 0},
                ),
                build_event(
                    run_id="cli-sim", seq=3, timestamp_ms=2, source="agent", event_type="agent_final",
                    payload={"workload_exit_code": 0, "restoration_ok": True, "spool_complete": True},
                ),
            ]
            events_path.write_bytes(b"".join(encode_event(record) for record in records))
            output = root / "results"
            simulate = self.run_cli(
                "--output-dir", str(output), "--json", "simulate", "--events", str(events_path), cwd=root
            )
            self.assertEqual(simulate.returncode, 0, simulate.stderr)
            simulated = json.loads(simulate.stdout)
            self.assertEqual(simulated["verdict"], "PASS")
            run_dir = Path(simulated["result"]).parent
            report = self.run_cli("--json", "report", "--run-dir", str(run_dir), "--format", "markdown,json", cwd=root)
            self.assertEqual(report.returncode, 0, report.stderr)
            self.assertTrue((run_dir / "report.md").exists())
            self.assertTrue((run_dir / "report.json").exists())

            (run_dir / "report.md").unlink()
            (run_dir / "report.json").unlink()
            invalid_report = self.run_cli(
                "--json", "report", "--run-dir", str(run_dir), "--format", "json,pdf", cwd=root
            )
            self.assertEqual(invalid_report.returncode, 4, invalid_report.stderr)
            self.assertFalse((run_dir / "report.json").exists())


if __name__ == "__main__":
    unittest.main()

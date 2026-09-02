from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.cli_commands import (
    _apply_platform_serial,
    _apply_saved_pairing,
    _concise_reason,
    _qualification_deadlines,
    cmd_golden,
    cmd_monitor_events,
    cmd_smoke,
    command_boundary,
)
from src.events import build_event, encode_event
from src.path_resolver import PathResolver
from src.run_orchestrator import RunInfrastructureError
from src.uart_protocol import encode_uart_frame


ROOT = Path(__file__).parents[1]
MAIN = ROOT / "main.py"


def valid_workload_document(target: str, verify_mode: str) -> dict:
    document = {
        "api": "cpu" if target == "cpu" else "vulkan",
        "verify_mode": verify_mode,
        "output_format": "jsonl",
        "duration": 1,
        "warmup": 0,
        "timeout": 2,
        "iterations": 1,
        "heartbeat_interval": 1,
    }
    if target == "cpu":
        document.update({"threads": 1, "working_set_kb": 1})
    else:
        document.update(
            {
                "width": 1,
                "height": 1,
                "gpu_timeout_ms": 1,
                "golden_file": "/data/local/tmp/avs/golden/test.rgba",
            }
        )
    return document


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
        for command in ("probe", "relay", "deploy", "verify-deployment", "telemetry", "smoke", "golden", "calibrate", "baseline", "run", "collect", "report"):
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

        relay_help = self.run_cli("relay", "probe", "--help")
        self.assertEqual(relay_help.returncode, 0, relay_help.stderr)
        self.assertIn("--check-uart", relay_help.stdout)

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

    def test_runtime_run_error_is_reported_as_infrastructure(self) -> None:
        @command_boundary
        def handler(_args):
            raise RunInfrastructureError("device-agent transport command did not finish")

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = handler(Namespace(json_output=True))
        self.assertEqual(exit_code, 3)
        self.assertEqual(json.loads(output.getvalue())["exit_code"], 3)

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
            workload_config.write_text(json.dumps(valid_workload_document("cpu", "none")), encoding="utf-8")
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

    def test_live_golden_uses_qualification_id_as_test_id_and_derived_deadlines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workload_binary = root / "cpu-avs-workload"
            workload_binary.write_bytes(b"cpu-workload")
            workload_config = root / "cpu-workload.json"
            document = valid_workload_document("cpu", "checksum")
            document["timeout"] = 75
            workload_config.write_text(json.dumps(document), encoding="utf-8")
            profile_path = root / "cpu-profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "cpu_live_qualification",
                        "target": "cpu",
                        "platform": "kirin9020",
                        "workload": {
                            "binary": str(workload_binary),
                            "remote_binary": "bin/cpu-avs-workload",
                            "config": str(workload_config),
                        },
                        "scheduler_requirements": {},
                        "baseline": None,
                        "telemetry": {"required": [], "optional": []},
                        "kernel_monitor": "off",
                    }
                ),
                encoding="utf-8",
            )
            source_spool = root / "captured" / "spool"
            source_spool.mkdir(parents=True)
            (source_spool / "events.jsonl").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "test_id": "QUAL-LIVE",
                        "run_id": "QUAL-LIVE-001-test",
                        "seq": 1,
                        "timestamp_ms": 1,
                        "source": "cpu-workload",
                        "type": "golden",
                        "payload": {"checksum": "0123456789abcdef"},
                    }
                ) + "\n",
                encoding="utf-8",
            )
            args = Namespace(
                known_good=True,
                config_dir=None,
                state_dir=str(root / "state"),
                output_dir=str(root / "output"),
                device_root="/data/local/tmp/avs",
                profile=str(profile_path),
                golden_target="cpu",
                runs=1,
                board_id="BOARD-A",
                run_dir=[],
                qualification_id="QUAL-LIVE",
                accept_checksum=None,
                json_output=True,
            )
            paths = PathResolver.create(
                output_dir=str(root / "output"),
                state_dir=str(root / "state"),
                entrypoint=MAIN,
            )
            profile = __import__("src.config_loader", fromlist=["ProfileConfig"]).ProfileConfig.from_file(profile_path)
            self.assertEqual(_qualification_deadlines(args, paths, profile, "golden"), (300.0, 80.0, 90.0, 20.0))
            output = io.StringIO()
            with patch("src.cli_commands._execute_live_qualification", return_value=[source_spool]) as execute:
                with redirect_stdout(output):
                    exit_code = cmd_golden(args)
            self.assertEqual(exit_code, 0, output.getvalue())
            self.assertEqual(execute.call_args.kwargs["test_id"], "QUAL-LIVE")
            self.assertEqual(json.loads(output.getvalue())["qualification_id"], "QUAL-LIVE")

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
            live_dir = output / "cli-sim"
            live_dir.mkdir(parents=True)
            live_marker = live_dir / "live-marker.txt"
            live_marker.write_text("preserve", encoding="utf-8")
            simulate = self.run_cli(
                "--output-dir", str(output), "--json", "simulate", "--events", str(events_path), cwd=root
            )
            self.assertEqual(simulate.returncode, 0, simulate.stderr)
            simulated = json.loads(simulate.stdout)
            self.assertEqual(simulated["verdict"], "PASS")
            self.assertIn("simulations", Path(simulated["result"]).parts)
            self.assertEqual(simulated["source_sha256"], __import__("hashlib").sha256(events_path.read_bytes()).hexdigest())
            repeated = self.run_cli(
                "--output-dir", str(output), "--json", "simulate", "--events", str(events_path), cwd=root
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertNotEqual(json.loads(repeated.stdout)["replay_id"], simulated["replay_id"])
            self.assertEqual(live_marker.read_text(encoding="utf-8"), "preserve")
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

    def test_simulate_uart_v2_raw_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = root / "serial.raw"
            records = [
                build_event(
                    run_id="raw-attempt", seq=1, timestamp_ms=0,
                    source="agent", event_type="agent_start", payload={},
                ),
                build_event(
                    run_id="raw-attempt", seq=2, timestamp_ms=1,
                    source="cpu-workload", event_type="summary",
                    payload={"result": "PASS", "exit_code": 0},
                ),
                build_event(
                    run_id="raw-attempt", seq=3, timestamp_ms=2,
                    source="agent", event_type="agent_final",
                    payload={"workload_exit_code": 0, "spool_complete": True},
                ),
            ]
            for record in records:
                record["test_id"] = "raw-test"
            raw_path.write_bytes(b"old-tail\x00" + b"".join(encode_uart_frame(record) for record in records))
            result = self.run_cli(
                "--output-dir", str(root / "results"), "--json", "simulate", "--raw-serial", str(raw_path)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["run_id"], "raw-attempt")
            self.assertEqual(payload["test_id"], "raw-test")
            self.assertEqual(payload["verdict"], "PASS")

    def test_monitor_decodes_uart_v2_but_does_not_issue_dut_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = [
                build_event(
                    run_id="monitor-attempt", seq=1, timestamp_ms=0,
                    source="agent", event_type="agent_start", payload={},
                ),
                build_event(
                    run_id="monitor-attempt", seq=2, timestamp_ms=1,
                    source="agent", event_type="agent_final",
                    payload={"workload_exit_code": 0, "spool_complete": True},
                ),
            ]
            for record in records:
                record["test_id"] = "monitor-test"
            chunks = [b"stale\x00", *[encode_uart_frame(record) for record in records]]

            class FakeSerialStream:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, _size):
                    return chunks.pop(0) if chunks else b""

            class FakeSerialModule:
                @staticmethod
                def Serial(**_kwargs):
                    return FakeSerialStream()

            args = Namespace(
                schema_version=1,
                state_dir=str(root / "state"),
                output_dir=str(root / "output"),
                config_dir=None,
                device_root="/data/local/tmp/avs",
                pc_serial="COM-FAKE",
                baudrate=115200,
                expected_run_id=None,
                save_raw=True,
                timeout=0.1,
                json_output=True,
            )
            output = io.StringIO()
            with patch.dict(sys.modules, {"serial": FakeSerialModule}), redirect_stdout(output):
                exit_code = cmd_monitor_events(args)
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["verdict"], "NOT_EVALUATED")
            self.assertTrue(payload["agent_final_seen"])
            result_dir = Path(payload["result"]).parent
            self.assertTrue((result_dir / "serial.raw").exists())

    def test_cpu_golden_calibration_and_draft_baseline_from_collected_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            state = root / "state"
            policy_path = root / "calibration-policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "minimum_boards": 2,
                        "minimum_accepted_samples": 2,
                        "rejection": {"reject_telemetry_gaps": True, "reject_throttled_samples": True},
                        "limits": {
                            "throughput": {"margin_percent": 5.0},
                            "latency": {"margin_percent": 10.0},
                        },
                    }
                ),
                encoding="utf-8",
            )
            workload_binary = root / "cpu-avs-workload"
            workload_binary.write_bytes(b"cpu-workload")
            workload_config = root / "cpu-workload.json"
            workload_config.write_text(
                json.dumps(valid_workload_document("cpu", "checksum")), encoding="utf-8"
            )
            profile_path = root / "cpu-profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "cpu_cli_qualification",
                        "target": "cpu",
                        "platform": "kirin9020",
                        "workload": {
                            "binary": str(workload_binary),
                            "remote_binary": "bin/cpu-avs-workload",
                            "config": str(workload_config),
                        },
                        "scheduler_requirements": {},
                        "baseline": None,
                        "telemetry": {"required": [], "optional": []},
                        "kernel_monitor": "off",
                    }
                ),
                encoding="utf-8",
            )
            golden_spools = []
            for index in range(2):
                spool = root / f"golden-{index}" / "spool"
                spool.mkdir(parents=True)
                event = {
                    "schema_version": 1,
                    "run_id": f"golden-{index}",
                    "seq": 1,
                    "timestamp_ms": index,
                    "source": "cpu-workload",
                    "type": "golden",
                    "payload": {"checksum": "0123456789abcdef"},
                }
                (spool / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
                golden_spools.append(spool)

            partial = self.run_cli(
                "--output-dir", str(output), "--json", "golden", "cpu",
                "--profile", str(profile_path), "--board-id", "BOARD-A", "--known-good",
                "--runs", "2", "--run-dir", str(golden_spools[0]),
            )
            self.assertEqual(partial.returncode, 4, partial.stderr)
            self.assertIn("exactly 2", partial.stdout)

            golden = self.run_cli(
                "--output-dir", str(output), "--json", "golden", "cpu",
                "--profile", str(profile_path), "--board-id", "BOARD-A", "--known-good",
                "--runs", "2", "--run-dir", str(golden_spools[0]), "--run-dir", str(golden_spools[1]),
                "--qualification-id", "CLI-CPU-GOLDEN",
            )
            self.assertEqual(golden.returncode, 0, golden.stdout or golden.stderr)
            golden_path = Path(json.loads(golden.stdout)["golden_manifest"])
            self.assertTrue(golden_path.exists())

            sample_dirs = []
            for index in range(2):
                test_root = root / f"sample-{index}"
                attempt = test_root / f"attempt-{index}"
                spool = test_root / "device-evidence" / attempt.name / "spool"
                attempt.mkdir(parents=True)
                spool.mkdir(parents=True)
                (attempt / "result.json").write_text(
                    json.dumps({"run_id": attempt.name, "verdict": "PASS"}), encoding="utf-8"
                )
                summary = {
                    "type": "summary", "result": "PASS", "exit_code": 0,
                    "operations_per_sec_avg": 1000.0 + index,
                    "batch_time_ms_p99": 10.0 + index,
                }
                (spool / "workload.log").write_text(json.dumps(summary) + "\n", encoding="utf-8")
                telemetry = {"payload": {"metric": "cpu.temperature", "value": 45.0}}
                (spool / "telemetry.jsonl").write_text(json.dumps(telemetry) + "\n", encoding="utf-8")
                sample_dirs.append(attempt)

            calibrate = self.run_cli(
                "--output-dir", str(output), "--state-dir", str(state), "--json", "calibrate", "cpu",
                "--profile", str(profile_path), "--board-id", "UNUSED", "--golden", str(golden_path),
                "--runs", "2", "--min-accepted", "2", "--temperature-range", "35:60",
                "--policy", str(policy_path),
                "--run-dir", f"BOARD-A={sample_dirs[0]}", "--run-dir", f"BOARD-B={sample_dirs[1]}",
                "--baseline-id", "cli-cpu-v1",
            )
            self.assertEqual(calibrate.returncode, 0, calibrate.stdout or calibrate.stderr)
            proposal = json.loads(calibrate.stdout)
            self.assertEqual(proposal["status"], "draft")
            approve = self.run_cli(
                "--state-dir", str(state), "--json", "baseline", "approve", "cli-cpu-v1",
                "--approver", "unit-test",
            )
            self.assertEqual(approve.returncode, 0, approve.stderr)
            self.assertEqual(json.loads(approve.stdout)["status"], "approved")

    def test_gpu_golden_uses_identical_collected_readbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workload_binary = root / "gpu-avs-workload"
            workload_binary.write_bytes(b"gpu-workload")
            workload_config = root / "gpu-workload.json"
            workload_config.write_text(
                json.dumps(valid_workload_document("gpu", "golden-image")), encoding="utf-8"
            )
            first_shader = root / "fullscreen.vert.spv"
            second_shader = root / "workload.frag.spv"
            first_shader.write_bytes(b"vert")
            second_shader.write_bytes(b"frag")
            profile_path = root / "gpu-profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "gpu_cli_qualification",
                        "target": "gpu",
                        "platform": "kirin9020",
                        "workload": {
                            "binary": str(workload_binary),
                            "remote_binary": "bin/gpu-avs-workload",
                            "config": str(workload_config),
                            "assets": [
                                {
                                    "local": str(first_shader),
                                    "remote": "shaders/vulkan/fullscreen.vert.spv",
                                    "kind": "shader",
                                },
                                {
                                    "local": str(second_shader),
                                    "remote": "shaders/vulkan/workload.frag.spv",
                                    "kind": "shader",
                                },
                            ],
                        },
                        "scheduler_requirements": {},
                        "baseline": None,
                        "telemetry": {"required": [], "optional": []},
                        "kernel_monitor": "off",
                    }
                ),
                encoding="utf-8",
            )
            spools = []
            for index in range(2):
                spool = root / f"gpu-{index}" / "spool"
                spool.mkdir(parents=True)
                event = {
                    "schema_version": 1, "run_id": f"gpu-{index}", "seq": 1,
                    "timestamp_ms": index, "source": "gpu-workload", "type": "golden",
                    "payload": {"checksum": "feedface"},
                }
                (spool / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
                (spool / "gpu-golden.rgba").write_bytes(b"same-readback")
                spools.append(spool)
            result = self.run_cli(
                "--output-dir", str(root / "output"), "--json", "golden", "gpu",
                "--profile", str(profile_path), "--board-id", "BOARD-A", "--known-good",
                "--runs", "2", "--run-dir", str(spools[0]), "--run-dir", str(spools[1]),
                "--qualification-id", "CLI-GPU-GOLDEN",
            )
            self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
            manifest = Path(json.loads(result.stdout)["golden_manifest"])
            self.assertTrue(manifest.exists())
            self.assertTrue((manifest.parent / "gpu-golden.rgba").exists())


if __name__ == "__main__":
    unittest.main()

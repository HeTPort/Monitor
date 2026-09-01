from __future__ import annotations

import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path, PurePosixPath

from src.baselines import Baseline
from src.cli_commands import _shell_agent_argv
from src.config_loader import ProfileConfig
from src.events import build_event, encode_event
from src.path_resolver import PathResolver
from src.run_orchestrator import RunInfrastructureError, RunManifestBuilder, RunOrchestrator
from src.transport import CommandResult
from src.uart_protocol import encode_uart_frame


def profile(source: Path) -> ProfileConfig:
    return ProfileConfig.from_mapping(
        {
            "schema_version": 1,
            "name": "cpu_mixed_big4",
            "target": "cpu",
            "platform": "kirin9020",
            "workload": {
                "binary": "../../tools/cpu-avs-workload",
                "remote_binary": "bin/cpu-avs-workload",
                "config": "../workloads/cpu.json",
                "argv": ["--config", "configs/cpu.json"],
            },
            "environment": {"affinity": "4-7", "governor": "performance", "online_cores": [4, 5]},
            "baseline": None,
            "telemetry": {
                "interval_ms": 1000,
                "required": ["cpu.frequency", "cpu.temperature"],
                "optional": ["cpu.online"],
            },
            "kernel_monitor": "critical",
        },
        source_path=source,
    )


def baseline(current_profile: ProfileConfig) -> Baseline:
    return Baseline.from_mapping(
        {
            "schema_version": 1,
            "id": "cpu-v1",
            "profile": current_profile.name,
            "target": "cpu",
            "platform": "kirin9020",
            "status": "approved",
            "fingerprints": {"profile": current_profile.fingerprint, "correctness": "golden-hash"},
            "golden": {"checksum": "0123456789abcdef"},
            "thresholds": {"performance": {"operations_per_sec_avg": {"min": 100.0}}},
            "calibration": {"accepted_count": 20},
            "approval": {"approver": "engineer", "approved_at": "2026-01-01T00:00:00Z"},
        }
    )


class RunManifestTests(unittest.TestCase):
    def test_builder_keeps_runtime_minimal_and_baseline_optional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = PathResolver(
                bundle_root=root,
                exe_root=root,
                state_root=root / "state",
                output_root=root / "output",
                cwd=root,
            )
            current_profile = profile(root / "profile.json")
            manifest = RunManifestBuilder(paths).build(
                profile=current_profile,
                baseline=baseline(current_profile),
                test_id="TEST-1",
                attempt_id="ATTEMPT-1",
                device_uart="/dev/ttyAMA0",
            )
            self.assertEqual(manifest["test_id"], "TEST-1")
            self.assertEqual(manifest["run_id"], "ATTEMPT-1")
            self.assertNotIn("taskset", manifest["workload"]["argv"])
            self.assertIn("--golden-checksum", manifest["workload"]["argv"])
            self.assertFalse(manifest["scheduler"]["managed_by_monitor"])
            self.assertEqual(manifest["scheduler"]["requirements"]["governor"], "performance")
            self.assertFalse(manifest["telemetry"]["enabled"])
            self.assertEqual(manifest["serial_transport"]["tail_guard_bytes"], 64)
            self.assertNotIn("kernel", manifest)
            self.assertFalse(manifest["event_crc"])
            agent_argv = _shell_agent_argv(PurePosixPath("/data/local/tmp/avs/bin/avs-device-agent"), manifest, 9600)
            self.assertEqual(agent_argv[:2], ["sh", "/data/local/tmp/avs/bin/avs-device-agent"])
            self.assertIn("--test-id", agent_argv)
            self.assertIn("--attempt-id", agent_argv)
            self.assertNotIn("--environment", agent_argv)
            self.assertNotIn("--telemetry-plan", agent_argv)
            self.assertEqual(agent_argv[agent_argv.index("--baudrate") + 1], "9600")
            self.assertEqual(agent_argv[agent_argv.index("--tail-guard") + 1], "64")
            self.assertIn("--", agent_argv)

            error_only = RunManifestBuilder(paths).build(
                profile=current_profile,
                baseline=None,
                test_id="TEST-2",
                attempt_id="ATTEMPT-2",
                device_uart="/dev/ttyAMA0",
                telemetry_enabled=True,
            )
            self.assertIsNone(error_only["baseline"])
            self.assertEqual(error_only["validation_mode"], "error-only")
            self.assertEqual(error_only["policy"]["required_telemetry"], [])
            telemetry_argv = _shell_agent_argv(PurePosixPath("/agent"), error_only, 9600)
            self.assertIn("--telemetry-plan", telemetry_argv)

            qualification = RunManifestBuilder(paths).build_qualification(
                profile=current_profile,
                golden={},
                capabilities=None,
                mode="golden",
                test_id="GOLDEN",
                attempt_id="golden-1",
                device_uart="/dev/ttyQualification7",
            )
            self.assertIsNone(qualification["baseline"])
            self.assertEqual(qualification["qualification"]["mode"], "golden")
            self.assertIn("--generate-golden", qualification["workload"]["argv"])

            smoke = RunManifestBuilder(paths).build_qualification(
                profile=current_profile,
                golden={},
                capabilities=None,
                mode="smoke",
                test_id="SMOKE",
                attempt_id="smoke-1",
                device_uart="/dev/ttyQualification7",
            )
            self.assertIsNone(smoke["baseline"])
            self.assertEqual(smoke["qualification"]["mode"], "smoke")
            self.assertFalse(smoke["qualification"]["production_baseline_allowed"])
            self.assertEqual(smoke["qualification"]["generated_reference_disposition"], "not-generated")
            self.assertNotIn("--generate-golden", smoke["workload"]["argv"])

    def test_scheduler_requirements_are_recorded_but_never_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = PathResolver(
                bundle_root=root,
                exe_root=root,
                state_root=root / "state",
                output_root=root / "output",
                cwd=root,
            )
            current_profile = ProfileConfig.from_mapping(
                {
                    "schema_version": 1,
                    "name": "cpu-policy-test",
                    "target": "cpu",
                    "platform": "kirin9020",
                    "workload": {"binary": "workload", "config": "workload.json"},
                    "environment": {"frequency_khz": "platform_max"},
                    "telemetry": {"required": [], "optional": []},
                    "kernel_monitor": "off",
                },
                source_path=root / "profile.json",
            )
            manifest = RunManifestBuilder(paths).build(
                profile=current_profile,
                baseline=None,
                test_id="NO-WRITES",
                attempt_id="NO-WRITES-1",
                device_uart="/dev/ttyHW0",
            )
            self.assertEqual(manifest["scheduler"]["requirements"]["frequency_khz"], "platform_max")
            self.assertNotIn("environment", manifest)
            self.assertNotIn("restoration_plan", manifest)


class RunOrchestratorTests(unittest.TestCase):
    def test_complete_stream_produces_pass_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {
                "schema_version": 1,
                "run_id": "run-pass",
                "profile": {"id": "cpu"},
                "baseline": {"id": "cpu-v1"},
                "overall_timeout_s": 10,
                "heartbeat_timeout_s": 5,
                "policy": {
                    "thresholds": {"performance": {"operations_per_sec_avg": {"min": 100.0}}},
                    "required_telemetry": ["cpu.frequency"],
                },
            }
            records = [
                build_event(run_id="run-pass", seq=1, timestamp_ms=0, source="agent", event_type="agent_start", payload={}),
                build_event(
                    run_id="run-pass",
                    seq=2,
                    timestamp_ms=1,
                    source="cpu-telemetry",
                    event_type="telemetry",
                    payload={"metrics": {"cpu.frequency": 2500000}},
                ),
                build_event(
                    run_id="run-pass",
                    seq=3,
                    timestamp_ms=2,
                    source="cpu-workload",
                    event_type="summary",
                    payload={"result": "PASS", "exit_code": 0, "operations_per_sec_avg": 150.0},
                ),
                build_event(
                    run_id="run-pass",
                    seq=4,
                    timestamp_ms=3,
                    source="agent",
                    event_type="agent_final",
                    payload={"workload_exit_code": 0, "restoration_ok": True, "spool_complete": True},
                ),
            ]
            wire = b"".join(encode_event(record) for record in records)
            chunks = [wire[:11], wire[11:79], wire[79:]]
            execution = RunOrchestrator(Path(tmp)).evaluate_stream(manifest, chunks)
            self.assertEqual(execution.result.verdict, "PASS")
            self.assertEqual(execution.event_count, 4)
            self.assertTrue(execution.result_path.exists())
            self.assertTrue((execution.result_path.parent / "workload-summary.json").exists())

    def test_corrupt_stream_is_infrastructure_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {
                "schema_version": 1,
                "run_id": "run-corrupt",
                "overall_timeout_s": 10,
                "heartbeat_timeout_s": 5,
                "policy": {"thresholds": {}, "required_telemetry": []},
            }
            record = build_event(
                run_id="run-corrupt", seq=1, timestamp_ms=0, source="agent", event_type="agent_start", payload={}
            )
            record["crc32"] = "00000000"
            execution = RunOrchestrator(Path(tmp)).evaluate_stream(manifest, [encode_event(record)])
            self.assertEqual(execution.result.verdict, "INFRA_ERROR")
            self.assertTrue(any(reason["scope"] == "protocol" for reason in execution.result.infrastructure_reasons))

    def test_protocol_failure_still_captures_later_serial_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {
                "schema_version": 1,
                "run_id": "run-drain",
                "overall_timeout_s": 10,
                "heartbeat_timeout_s": 5,
                "policy": {"thresholds": {}, "required_telemetry": []},
            }
            first = b"not-json\n"
            later = encode_event(
                build_event(
                    run_id="run-drain",
                    seq=1,
                    timestamp_ms=1,
                    source="agent",
                    event_type="agent_final",
                    payload={"workload_exit_code": 3, "restoration_ok": True, "spool_complete": True},
                )
            )
            execution = RunOrchestrator(Path(tmp)).evaluate_stream(manifest, [first, later])
            self.assertEqual(execution.result.verdict, "INFRA_ERROR")
            self.assertEqual((execution.result_path.parent / "serial.raw").read_bytes(), first + later)

    def test_telemetry_does_not_mask_workload_heartbeat_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {
                "schema_version": 1,
                "run_id": "run-heartbeat-timeout",
                "overall_timeout_s": 100,
                "heartbeat_timeout_s": 5,
                "policy": {"thresholds": {}, "required_telemetry": []},
            }
            records = [
                build_event(run_id=manifest["run_id"], seq=1, timestamp_ms=0, source="agent", event_type="agent_start", payload={}),
                build_event(run_id=manifest["run_id"], seq=2, timestamp_ms=1, source="cpu-telemetry", event_type="telemetry", payload={"metric": "cpu.temperature", "value": 30}),
                build_event(run_id=manifest["run_id"], seq=3, timestamp_ms=2, source="cpu-telemetry", event_type="telemetry", payload={"metric": "cpu.temperature", "value": 31}),
            ]
            ticks = iter([0.0, 0.0, 3.0, 6.0])
            execution = RunOrchestrator(Path(tmp)).evaluate_stream(
                manifest,
                [encode_event(record) for record in records],
                clock=lambda: next(ticks),
            )
            self.assertEqual(execution.result.verdict, "INFRA_ERROR")
            self.assertTrue(any(reason["code"] == "HEARTBEAT_OR_SUMMARY_TIMEOUT" for reason in execution.result.dut_reasons))

    def test_hardware_run_opens_and_clears_serial_before_agent_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {
                "schema_version": 1,
                "test_id": "test-serial-order",
                "run_id": "run-serial-order",
                "overall_timeout_s": 10,
                "heartbeat_timeout_s": 5,
                "policy": {"thresholds": {}, "required_telemetry": []},
                "serial_transport": {"protocol": "uart-v2", "max_frame_bytes": 512},
            }
            records = [
                build_event(run_id=manifest["run_id"], seq=1, timestamp_ms=0, source="agent", event_type="agent_start", payload={}),
                build_event(run_id=manifest["run_id"], seq=2, timestamp_ms=1, source="cpu-workload", event_type="summary", payload={"result": "PASS", "exit_code": 0}),
                build_event(run_id=manifest["run_id"], seq=3, timestamp_ms=2, source="agent", event_type="agent_final", payload={"workload_exit_code": 0, "restoration_ok": True, "spool_complete": True}),
            ]
            for record in records:
                record["test_id"] = manifest["test_id"]
            stale = encode_uart_frame(
                build_event(run_id="old-run", seq=1, timestamp_ms=0, source="agent", event_type="agent_start", payload={})
                | {"test_id": "old-test"}
            )
            wire = bytearray(b"delayed-old-tail\x00" + stale + b"".join(encode_uart_frame(record) for record in records))

            class FakeSerial:
                opened = False
                resets = 0

                def __init__(self, **_kwargs):
                    FakeSerial.opened = True

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    FakeSerial.opened = False

                def reset_input_buffer(self):
                    FakeSerial.resets += 1

                def read(self, size):
                    chunk = bytes(wire[:size])
                    del wire[:size]
                    return chunk

            class FakeTransport:
                def invoke(self, argv, timeout_s):
                    self.argv = argv
                    self.timeout_s = timeout_s
                    if not FakeSerial.opened or FakeSerial.resets < 1:
                        raise AssertionError("agent launched before serial was opened and cleared")
                    return CommandResult(tuple(argv), 0, "", "", 0.0)

            with patch.dict("sys.modules", {"serial": SimpleNamespace(Serial=FakeSerial)}):
                execution = RunOrchestrator(Path(tmp)).run_serial(
                    manifest,
                    transport=FakeTransport(),
                    agent_argv=["agent"],
                    pc_serial="COM-test",
                    baudrate=9600,
                )
            self.assertEqual(execution.result.verdict, "PASS")
            # Clear only before launch. Clearing after FINAL can discard bytes
            # from a following session on adapters that deliver asynchronously.
            self.assertEqual(FakeSerial.resets, 1)

    def test_hardware_timeout_cancels_agent_without_waiting_for_worker_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {
                "schema_version": 1,
                "test_id": "test-timeout",
                "run_id": "run-timeout",
                "overall_timeout_s": 0.02,
                "heartbeat_timeout_s": 0.01,
                "policy": {"thresholds": {}, "required_telemetry": []},
                "serial_transport": {"protocol": "uart-v2", "max_frame_bytes": 512, "tail_guard_bytes": 64},
            }

            class EmptySerial:
                def __init__(self, **_kwargs):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    pass

                def reset_input_buffer(self):
                    pass

                def read(self, _size):
                    time.sleep(0.005)
                    return b""

            class BlockingTransport:
                def __init__(self):
                    self.release = threading.Event()
                    self.cancel_count = 0

                def invoke(self, argv, _timeout_s):
                    self.release.wait(5)
                    return CommandResult(tuple(argv), -1, "", "cancelled", 0.0, timed_out=True)

                def cancel_active(self):
                    self.cancel_count += 1
                    self.release.set()
                    return 1

            transport = BlockingTransport()
            started = time.monotonic()
            with patch.dict("sys.modules", {"serial": SimpleNamespace(Serial=EmptySerial)}):
                with self.assertRaisesRegex(RunInfrastructureError, "did not finish"):
                    RunOrchestrator(Path(tmp)).run_serial(
                        manifest,
                        transport=transport,
                        agent_argv=["agent"],
                        pc_serial="COM-test",
                        baudrate=9600,
                    )
            self.assertGreaterEqual(transport.cancel_count, 1)
            self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main()

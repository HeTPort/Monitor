from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path, PurePosixPath

from src.baselines import Baseline
from src.cli_commands import _shell_agent_argv
from src.config_loader import ProfileConfig
from src.events import build_event, encode_event
from src.path_resolver import PathResolver
from src.run_orchestrator import RunManifestBuilder, RunOrchestrator
from src.transport import CommandResult


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
    def test_builder_resolves_environment_telemetry_and_kernel_rules(self) -> None:
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
            capabilities = {
                "capabilities": {
                    "cpu.governor": {"paths": ["/sys/cpu4/governor", "/sys/cpu5/governor"]},
                    "cpu.online": {"paths": ["/sys/cpu4/online", "/sys/cpu5/online", "/sys/cpu6/online"]},
                    "cpu.frequency": {"paths": ["/sys/cpu4/freq", "/sys/cpu5/freq"], "unit": "kHz"},
                    "cpu.temperature": {
                        "paths": [
                            "/sys/class/thermal/thermal_zone0/temp",
                            "/sys/class/thermal/thermal_zone2/temp",
                        ],
                        "unit": "celsius",
                        "parser_by_path": {
                            "/sys/class/thermal/thermal_zone0/temp": "millidegree_celsius",
                            "/sys/class/thermal/thermal_zone2/temp": "degree_celsius",
                        },
                    },
                }
            }
            rules = root / "rules.conf"
            rules.write_text("[warn]\nicontains|thermal limit\n[fail]\niregex|gpu.*hang\n", encoding="utf-8")
            manifest = RunManifestBuilder(paths).build(
                profile=current_profile,
                baseline=baseline(current_profile),
                capabilities=capabilities,
                run_id="run-1",
                kernel_rules_path=rules,
                device_uart="/dev/ttyAMA0",
            )
            self.assertEqual(manifest["workload"]["argv"][:3], ["taskset", "-c", "4-7"])
            self.assertIn("--golden-checksum", manifest["workload"]["argv"])
            self.assertEqual(len(manifest["environment"]["actions"]), 4)
            self.assertEqual(len(manifest["telemetry"]["samplers"]), 3)
            self.assertEqual(len(manifest["kernel"]["rules"]), 2)
            self.assertFalse(manifest["kernel"]["raw_local"])
            self.assertFalse(manifest["event_crc"])
            agent_argv = _shell_agent_argv(PurePosixPath("/data/local/tmp/avs/bin/avs-device-agent"), manifest, 9600)
            self.assertEqual(agent_argv[:2], ["sh", "/data/local/tmp/avs/bin/avs-device-agent"])
            self.assertIn("--kernel-rule", agent_argv)
            self.assertEqual(agent_argv[agent_argv.index("--telemetry-interval") + 1], "5")
            self.assertEqual(agent_argv[agent_argv.index("--baudrate") + 1], "9600")
            self.assertIn("--", agent_argv)
            telemetry_specs = [
                agent_argv[index + 1]
                for index, value in enumerate(agent_argv[:-1])
                if value == "--telemetry"
            ]
            self.assertTrue(any("|millidegree_celsius|" in spec for spec in telemetry_specs))
            self.assertTrue(any("|degree_celsius|" in spec for spec in telemetry_specs))

            qualification = RunManifestBuilder(paths).build_qualification(
                profile=current_profile,
                golden={},
                capabilities=capabilities,
                mode="golden",
                run_id="golden-1",
                kernel_mode="critical",
                kernel_rules_path=rules,
                device_uart="/dev/ttyQualification7",
            )
            self.assertIsNone(qualification["baseline"])
            self.assertEqual(qualification["qualification"]["mode"], "golden")
            self.assertIn("--generate-golden", qualification["workload"]["argv"])

            smoke = RunManifestBuilder(paths).build_qualification(
                profile=current_profile,
                golden={},
                capabilities=capabilities,
                mode="smoke",
                run_id="smoke-1",
                kernel_mode="critical",
                kernel_rules_path=rules,
                device_uart="/dev/ttyQualification7",
            )
            self.assertIsNone(smoke["baseline"])
            self.assertEqual(smoke["qualification"]["mode"], "smoke")
            self.assertFalse(smoke["qualification"]["production_baseline_allowed"])
            self.assertEqual(smoke["qualification"]["generated_reference_disposition"], "discard")
            self.assertIn("--generate-golden", smoke["workload"]["argv"])

    def test_policy_paths_keep_per_policy_platform_maxima(self) -> None:
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
            values = {
                "/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq": 1550000,
                "/sys/devices/system/cpu/cpufreq/policy1/scaling_max_freq": 2050000,
                "/sys/devices/system/cpu/cpufreq/policy2/scaling_max_freq": 2094000,
                "/sys/devices/system/cpu/cpufreq/policy3/scaling_max_freq": 2508000,
            }
            capabilities = {
                "capabilities": {
                    "cpu.maximum_frequency": {"paths": list(values), "values": values},
                    "cpu.minimum_frequency": {
                        "paths": [path.replace("scaling_max_freq", "scaling_min_freq") for path in values]
                    },
                }
            }
            actions = RunManifestBuilder(paths)._environment_actions(current_profile, capabilities)
            requested = {action["path"]: int(action["value"]) for action in actions}
            for maximum_path, value in values.items():
                self.assertEqual(requested[maximum_path], value)
                self.assertEqual(requested[maximum_path.replace("scaling_max_freq", "scaling_min_freq")], value)


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
                "run_id": "run-serial-order",
                "overall_timeout_s": 10,
                "heartbeat_timeout_s": 5,
                "policy": {"thresholds": {}, "required_telemetry": []},
            }
            records = [
                build_event(run_id=manifest["run_id"], seq=1, timestamp_ms=0, source="agent", event_type="agent_start", payload={}),
                build_event(run_id=manifest["run_id"], seq=2, timestamp_ms=1, source="cpu-workload", event_type="summary", payload={"result": "PASS", "exit_code": 0}),
                build_event(run_id=manifest["run_id"], seq=3, timestamp_ms=2, source="agent", event_type="agent_final", payload={"workload_exit_code": 0, "restoration_ok": True, "spool_complete": True}),
            ]
            wire = bytearray(b"".join(encode_event(record) for record in records))

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
            self.assertGreaterEqual(FakeSerial.resets, 2)


if __name__ == "__main__":
    unittest.main()

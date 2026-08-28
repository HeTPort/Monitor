from __future__ import annotations

import tempfile
import unittest
from pathlib import Path, PurePosixPath

from src.baselines import Baseline
from src.cli_commands import _shell_agent_argv
from src.config_loader import ProfileConfig
from src.events import build_event, encode_event
from src.path_resolver import PathResolver
from src.run_orchestrator import RunManifestBuilder, RunOrchestrator


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
                    "cpu.temperature": {"paths": ["/sys/thermal/temp"], "unit": "millidegree_celsius"},
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

            qualification = RunManifestBuilder(paths).build_qualification(
                profile=current_profile,
                golden={},
                capabilities=capabilities,
                mode="golden",
                run_id="golden-1",
                kernel_mode="critical",
                kernel_rules_path=rules,
            )
            self.assertIsNone(qualification["baseline"])
            self.assertEqual(qualification["qualification"]["mode"], "golden")
            self.assertIn("--generate-golden", qualification["workload"]["argv"])


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


if __name__ == "__main__":
    unittest.main()

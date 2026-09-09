import hashlib
import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

from src.cli_commands import _runtime_preflight, _sample_from_run
from src.deployment import AssetSpec, DeploymentError
from src.evidence_collection import collect_subset, validated_hash_entries
from src.events import EventEnvelope, build_event
from src.policy_engine import PolicyEngine, PolicyLimits
from src.qualification import CalibrationPolicy, CalibrationSample, CalibrationService, QualificationError
from src.transport import CommandResult, FakeTransport, TransportError
from src.verification_contract import REQUIRED_FEATURES, validate_capabilities, verification_issues, verification_requirement


def event(seq, kind, payload):
    return EventEnvelope.from_mapping(build_event(run_id="test", seq=seq, timestamp_ms=seq,
        source="cpu-workload", event_type=kind, payload=payload))


class VerificationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.requirement = verification_requirement("cpu", {"verify_mode": "checksum"}, strict=True)
        self.summary = dict(result="PASS", exit_code=0, contract_version=2, verify_mode="checksum",
            verify_interval=1, success_log_interval=60, verify_pass=True, verify_count=10,
            verify_fail_count=0, batch_count=10)

    def test_split_metadata_is_merged_and_checked(self):
        engine = PolicyEngine(PolicyLimits(verification=self.requirement))
        engine.process(event(1, "verification_summary", {k: v for k, v in self.summary.items() if k not in {"result", "exit_code", "verify_pass"}}))
        engine.process(event(2, "summary", {"result": "PASS", "exit_code": 0, "verify_pass": True}))
        result = engine.finalize(require_agent_final=False)
        self.assertEqual(result.verdict, "PASS")
        self.assertEqual(result.workload_summary["verify_count"], 10)

    def test_missing_zero_malformed_and_inconsistent_checks_cannot_pass(self):
        for override in ({"verify_count": 0}, {"verify_count": 9}, {"verify_count": True},
                         {"verify_interval": 2}, {"verify_fail_count": "bad"}, {"contract_version": 1}):
            with self.subTest(override=override):
                engine = PolicyEngine(PolicyLimits(verification=self.requirement))
                engine.process(event(1, "summary", self.summary | override))
                self.assertEqual(engine.finalize(require_agent_final=False).verdict, "INFRA_ERROR")
        self.assertTrue(verification_issues({"result": "PASS"}, self.requirement))

    def test_failure_survives_missing_summary_and_conflicts_fail_closed(self):
        for payload in ({"result": "FAIL"}, {"pass": False}):
            engine = PolicyEngine()
            engine.process(event(1, "verify", payload))
            self.assertEqual(engine.finalize(timed_out=True, require_agent_final=False).dut_reasons[0]["code"], "VERIFY_FAIL")
        engine = PolicyEngine(PolicyLimits(verification=self.requirement))
        engine.process(event(1, "verification_summary", {"verify_count": 9}))
        engine.process(event(2, "summary", self.summary))
        self.assertEqual(engine.finalize(require_agent_final=False).verdict, "INFRA_ERROR")

    def test_gpu_coverage_and_exempt_modes(self):
        requirement = verification_requirement("gpu", {"verify_mode": "golden-image", "verify_interval": 2, "success_log_interval": 0})
        summary = self.summary | {"verify_mode": "golden-image", "verify_interval": 2, "success_log_interval": 0, "frame_count": 21}
        self.assertEqual(verification_issues(summary, requirement), [])
        self.assertEqual(verification_issues({}, {"required": False}), [])
        for config in ({"verify_mode": "none"}, {"verify_mode": "checksum", "verify_interval": 2}):
            with self.assertRaises(ValueError):
                verification_requirement("cpu", config, strict=True)

    def test_capability_schema_rejects_old_and_missing_features(self):
        good = {"workload": "cpu", "contract_version": 2, "features": sorted(REQUIRED_FEATURES), "verify_modes": ["checksum"]}
        validate_capabilities(good, "cpu", "checksum")
        for changes in ({"contract_version": 1}, {"features": []}, {"workload": "gpu"}, {"verify_modes": []}):
            with self.assertRaises(ValueError):
                validate_capabilities(good | changes, "cpu", "checksum")


class EvidenceCollectionTests(unittest.TestCase):
    def test_subset_only_pulls_selected_hash_verified_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote = PurePosixPath("/avs/tests/test/attempt")
            contents = {"final.json": b"{}", "workload-summary.json": b"{}", "workload.log": b"huge"}
            hashes = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
            files = {str(remote / "spool" / name): data for name, data in contents.items()}
            files[str(remote / "spool/artifact-hashes.json")] = json.dumps({"sha256": hashes}).encode()
            transport = FakeTransport(files)
            record = collect_subset(transport, remote, Path(tmp), "minimal")
            self.assertEqual(record["omitted_files"], ["workload.log"])
            self.assertFalse((Path(tmp) / "spool/workload.log").exists())
            self.assertEqual(len(transport.files), 4)
            with self.assertRaisesRegex(TransportError, "required"):
                collect_subset(transport, remote, Path(tmp), "calibration")
            transport.files[str(remote / "spool/final.json")] = b"tampered"
            with self.assertRaisesRegex(TransportError, "mismatch"):
                collect_subset(transport, remote, Path(tmp), "minimal")

    def test_hash_manifest_cannot_escape_collection_root(self):
        for name in ("../secret", "/secret", "C:/secret", "a\\secret", "a/../secret"):
            with self.assertRaises(TransportError):
                validated_hash_entries({"sha256": {name: "a" * 64}})


class PreflightTests(unittest.TestCase):
    def test_mismatched_binary_stops_before_capability_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "workload"
            binary.write_bytes(b"new")
            asset = AssetSpec(binary, PurePosixPath("/avs/bin/cpu"), kind="workload")
            transport = FakeTransport({"/avs/bin/cpu": b"old"})
            profile = SimpleNamespace(target="cpu", platform="soc")
            paths = SimpleNamespace(device_root=PurePosixPath("/avs"), resolve_resource=lambda _: binary, remote=lambda p: PurePosixPath("/avs") / p)
            with patch("src.cli_commands._asset_plan", return_value=([asset], None, binary)), \
                 patch("src.cli_commands.validate_workload_config", return_value={"verify_mode": "checksum"}), \
                 patch("src.cli_commands.load_platform", return_value=SimpleNamespace(name="soc", serial={})):
                with self.assertRaises(DeploymentError):
                    _runtime_preflight(paths, profile, transport)
            self.assertTrue(all(command[0] == "sha256sum" for command in transport.commands))


class CalibrationContractTests(unittest.TestCase):
    def test_cv_policy_is_enforced_and_duplicate_runs_rejected(self):
        samples = [CalibrationSample(run_id=f"r{i}", board_id=f"b{i}", summary={"result": "PASS", "exit_code": 0,
            "operations_per_sec_avg": value, "batch_time_ms_p99": 10}) for i, value in enumerate((100, 200))]
        args = dict(profile="cpu", target="cpu", platform="soc", fingerprints={}, golden={}, baseline_id="test")
        policy = CalibrationPolicy.from_mapping({"minimum_accepted_samples": 2, "limits": {"variation": {"maximum_percent": 10}}})
        with self.assertRaisesRegex(QualificationError, "variation"):
            CalibrationService().calibrate(**args, samples=samples, policy=policy)
        with self.assertRaisesRegex(QualificationError, "duplicate"):
            CalibrationService().calibrate(**args, samples=[samples[0], samples[0]], policy=policy)

    def test_native_summary_subset_supports_calibration_and_pc_failure_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "spool").mkdir()
            (root / "result.json").write_text(json.dumps({"run_id": "r", "verdict": "INFRA_ERROR", "exit_code": 3}))
            (root / "spool/workload-summary.json").write_text(json.dumps({"type": "summary", "result": "PASS", "exit_code": 0, "batch_count": 3}))
            (root / "spool/telemetry.jsonl").write_text('\n'.join(json.dumps({"timestamp_ms": i * 5000, "payload": {"complete": i != 1,
                "metrics": {"cpu.temperature": 45}}}) for i in range(3)))
            sample = _sample_from_run(root, "b", required_metrics=["cpu.temperature"])
            self.assertEqual(sample.summary["batch_count"], 3)
            self.assertIn("pc_result_not_pass", sample.rejection_reasons)
            self.assertFalse(sample.telemetry_complete)

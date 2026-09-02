from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from src.cli_commands import _sample_from_run
from src.qualification import (
    CalibrationPolicy,
    CalibrationSample,
    CalibrationService,
    GoldenService,
    QualificationError,
)
from src.qualification_artifacts import resolve_qualification_run


class GoldenServiceTests(unittest.TestCase):
    def test_cpu_and_gpu_repeat_consistency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = GoldenService(Path(tmp))
            cpu = service.create_cpu(
                qualification_id="q-cpu",
                profile="cpu_mixed_big4",
                fingerprint_fields={"threads": 4, "iterations": 100000},
                golden_records=[{"checksum": "0123456789abcdef"}] * 3,
                board_ids=["b1", "b1", "b2"],
            )
            self.assertEqual(cpu["repeat_count"], 3)
            first = Path(tmp) / "readback-1.rgba"
            second = Path(tmp) / "readback-2.rgba"
            first.write_bytes(b"raw-gpu-buffer")
            second.write_bytes(b"raw-gpu-buffer")
            gpu = service.create_gpu(
                qualification_id="q-gpu",
                profile="gpu_vulkan_mixed",
                fingerprint_fields={"api": "vulkan", "width": 1280, "height": 720},
                golden_records=[{"checksum": "aa"}, {"checksum": "aa"}],
                readback_files=[first, second],
                board_ids=["b1", "b2"],
            )
            self.assertEqual(gpu["readback_size"], len(b"raw-gpu-buffer"))

    def test_inconsistent_goldens_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = GoldenService(Path(tmp))
            with self.assertRaisesRegex(QualificationError, "inconsistent"):
                service.create_cpu(
                    qualification_id="q",
                    profile="cpu",
                    fingerprint_fields={},
                    golden_records=[{"checksum": "a"}, {"checksum": "b"}],
                    board_ids=["b1", "b2"],
                )


class CalibrationServiceTests(unittest.TestCase):
    def test_rejects_bad_environment_and_proposes_margined_limits(self) -> None:
        samples = []
        for index in range(20):
            samples.append(
                CalibrationSample(
                    run_id=f"good-{index}",
                    board_id="b1" if index < 10 else "b2",
                    summary={
                        "result": "PASS",
                        "exit_code": 0,
                        "operations_per_sec_avg": 1000.0 + index,
                        "batch_time_ms_p99": 10.0 + index / 10.0,
                    },
                    temperature_c=45.0,
                )
            )
        samples.append(
            CalibrationSample(
                run_id="hot",
                board_id="b2",
                summary={
                    "result": "PASS",
                    "exit_code": 0,
                    "operations_per_sec_avg": 5000.0,
                    "batch_time_ms_p99": 1.0,
                },
                temperature_c=80.0,
            )
        )
        proposal = CalibrationService().calibrate(
            profile="cpu_mixed_big4",
            target="cpu",
            platform="kirin9020",
            fingerprints={"profile": "abc", "correctness": "def"},
            golden={"checksum": "0123456789abcdef"},
            samples=samples,
            policy=CalibrationPolicy(),
            temperature_range=(35.0, 60.0),
            baseline_id="kirin9020-cpu-v1",
        )
        calibration = proposal["calibration"]
        self.assertEqual(calibration["accepted_count"], 20)
        self.assertEqual(calibration["rejected_count"], 1)
        self.assertAlmostEqual(
            proposal["thresholds"]["performance"]["operations_per_sec_avg"]["min"],
            950.0,
        )
        self.assertAlmostEqual(
            proposal["thresholds"]["performance"]["batch_time_ms_p99"]["max"],
            13.09,
        )

    def test_insufficient_cohort_fails(self) -> None:
        sample = CalibrationSample(
            run_id="one",
            board_id="only-board",
            summary={"result": "PASS", "exit_code": 0, "fps_avg": 60.0, "frame_time_p99_ms": 20.0},
        )
        with self.assertRaisesRegex(QualificationError, "minimum"):
            CalibrationService().calibrate(
                profile="gpu",
                target="gpu",
                platform="soc",
                fingerprints={"profile": "a"},
                golden={"readback_sha256": "b"},
                samples=[sample],
                policy=CalibrationPolicy(minimum_boards=2, minimum_accepted_samples=1),
                baseline_id="gpu-v1",
            )

    def test_standard_collected_layout_resolves_full_summary_and_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            test_root = Path(tmp) / "QUAL-CPU"
            attempt = test_root / "QUAL-CPU-001"
            spool = test_root / "device-evidence" / "QUAL-CPU-001" / "spool"
            attempt.mkdir(parents=True)
            spool.mkdir(parents=True)
            (attempt / "result.json").write_text(
                json.dumps({"run_id": "QUAL-CPU-001", "verdict": "PASS"}), encoding="utf-8"
            )
            (attempt / "workload-summary.json").write_text(
                json.dumps({"result": "PASS", "exit_code": 0}), encoding="utf-8"
            )
            full_summary = {
                "type": "summary",
                "result": "PASS",
                "exit_code": 0,
                "operations_per_sec_avg": 1234.0,
                "batch_time_ms_p99": 9.5,
            }
            (spool / "workload.log").write_text(json.dumps(full_summary) + "\n", encoding="utf-8")
            (spool / "events.jsonl").write_text(
                json.dumps({"type": "golden", "payload": {"checksum": "abc"}}) + "\n", encoding="utf-8"
            )
            (spool / "telemetry.jsonl").write_text(
                json.dumps({"payload": {"metric": "cpu.temperature", "value": 45.0}}) + "\n",
                encoding="utf-8",
            )
            resolved = resolve_qualification_run(attempt)
            self.assertEqual(resolved.spool_dir, spool)
            self.assertEqual(resolved.summary["operations_per_sec_avg"], 1234.0)
            self.assertEqual(resolved.events_path, spool / "events.jsonl")
            sample = _sample_from_run(attempt, "BOARD-A")
            self.assertEqual(sample.summary["batch_time_ms_p99"], 9.5)
            self.assertEqual(sample.temperature_c, 45.0)
            self.assertTrue(sample.telemetry_complete)

    def test_spool_input_pairs_back_to_pc_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            test_root = Path(tmp) / "QUAL-GPU"
            attempt = test_root / "QUAL-GPU-001"
            spool = test_root / "device-evidence" / "QUAL-GPU-001" / "spool"
            attempt.mkdir(parents=True)
            spool.mkdir(parents=True)
            (attempt / "result.json").write_text(json.dumps({"run_id": "QUAL-GPU-001"}), encoding="utf-8")
            (spool / "workload.log").write_text(
                json.dumps({"type": "summary", "result": "PASS", "exit_code": 0, "fps_avg": 8.0, "frame_time_p99_ms": 130.0}) + "\n",
                encoding="utf-8",
            )
            (spool / "events.jsonl").write_text("", encoding="utf-8")
            resolved = resolve_qualification_run(spool)
            self.assertEqual(resolved.pc_run_dir, attempt)
            self.assertEqual(resolved.result_path, attempt / "result.json")


if __name__ == "__main__":
    unittest.main()

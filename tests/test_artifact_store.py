from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.artifact_store import ArtifactError, ArtifactStore, sha256_file
from src.events import EventEnvelope, build_event


class ArtifactStoreTests(unittest.TestCase):
    def test_routes_events_and_finalizes_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore.create(Path(tmp), "run-1")
            store.write_json("run-manifest.json", {"schema_version": 1, "run_id": "run-1"})
            telemetry = EventEnvelope.from_mapping(
                build_event(
                    run_id="run-1",
                    seq=1,
                    timestamp_ms=1,
                    source="cpu-telemetry",
                    event_type="telemetry",
                    payload={"metric": "cpu.frequency", "value": 2500000},
                )
            )
            store.append_event(telemetry)
            result_path = store.finalize({"verdict": "PASS", "exit_code": 0})
            result = json.loads(result_path.read_text(encoding="utf-8"))
            hashes = json.loads((store.run_dir / "artifact-hashes.json").read_text(encoding="utf-8"))["sha256"]
            self.assertTrue(result["artifacts"]["complete"])
            self.assertIn("events.jsonl", hashes)
            self.assertIn("telemetry.jsonl", hashes)
            self.assertEqual(hashes["run-manifest.json"], sha256_file(store.run_dir / "run-manifest.json"))

    def test_refuses_nonempty_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "same"
            existing.mkdir()
            (existing / "data").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ArtifactError, "already contains"):
                ArtifactStore.create(Path(tmp), "same")

    def test_groups_attempt_artifacts_below_test_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore.create(Path(tmp), "attempt-002", test_id="TEST-0831")
            expected_dir = Path(tmp) / "TEST-0831" / "attempt-002"
            self.assertTrue(store.run_dir.exists())
            self.assertTrue(expected_dir.exists())
            self.assertTrue(store.run_dir.samefile(expected_dir))

    def test_incomplete_result_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore.create(Path(tmp), "run-2")
            path = store.close_incomplete("disk full")
            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["verdict"], "INFRA_ERROR")
            self.assertFalse(result["artifacts"]["complete"])


if __name__ == "__main__":
    unittest.main()

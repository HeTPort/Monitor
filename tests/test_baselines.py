from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.baselines import BaselineError, BaselineRegistry


def proposal(identifier: str = "soc-cpu-v1") -> dict:
    return {
        "schema_version": 1,
        "id": identifier,
        "profile": "cpu_mixed_big4",
        "target": "cpu",
        "platform": "kirin9020",
        "status": "draft",
        "fingerprints": {"profile": "abc", "correctness": "def"},
        "golden": {"checksum": "0123456789abcdef"},
        "thresholds": {"performance": {"operations_per_sec_avg": {"min": 100.0}}},
        "calibration": {"accepted_count": 20},
        "approval": None,
    }


class BaselineRegistryTests(unittest.TestCase):
    def test_draft_approval_resolution_and_immutable_deprecation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = BaselineRegistry(Path(tmp))
            draft = registry.create_draft(proposal())
            self.assertEqual(draft.status, "draft")
            approved = registry.approve(draft.id, "engineer")
            self.assertEqual(approved.status, "approved")
            self.assertTrue(registry.verify_immutable(draft.id))
            baseline_path = registry.root / draft.id / "baseline.json"
            approved_bytes = baseline_path.read_bytes()
            resolved = registry.resolve("cpu_mixed_big4", {"profile": "abc", "correctness": "def"})
            self.assertEqual(resolved.id, draft.id)
            deprecated = registry.deprecate(draft.id, "driver update")
            self.assertEqual(deprecated.status, "deprecated")
            self.assertEqual(baseline_path.read_bytes(), approved_bytes)
            self.assertTrue(registry.verify_immutable(draft.id))

    def test_invalid_transitions_and_ambiguous_auto_resolution_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = BaselineRegistry(Path(tmp))
            registry.create_draft(proposal("one"))
            with self.assertRaisesRegex(BaselineError, "only an approved"):
                registry.deprecate("one", "bad")
            registry.approve("one", "engineer")
            registry.create_draft(proposal("two"))
            registry.approve("two", "engineer")
            with self.assertRaisesRegex(BaselineError, "multiple"):
                registry.resolve("cpu_mixed_big4", {"profile": "abc"})

    def test_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = BaselineRegistry(Path(tmp))
            registry.create_draft(proposal())
            registry.approve("soc-cpu-v1", "engineer")
            path = registry.root / "soc-cpu-v1" / "baseline.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["thresholds"]["performance"]["operations_per_sec_avg"]["min"] = 1
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertFalse(registry.verify_immutable("soc-cpu-v1"))

    def test_export_import_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = BaselineRegistry(root / "source")
            source.create_draft(proposal())
            source.approve("soc-cpu-v1", "engineer")
            bundle = source.export_bundle("soc-cpu-v1", root / "cpu-v1.zip")
            destination = BaselineRegistry(root / "destination")
            imported = destination.import_bundle(bundle)
            self.assertEqual(imported.id, "soc-cpu-v1")
            self.assertTrue(destination.verify_immutable(imported.id))


if __name__ == "__main__":
    unittest.main()

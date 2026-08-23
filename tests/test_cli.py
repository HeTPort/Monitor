from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.events import build_event, encode_event


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
        for command in ("probe", "deploy", "golden", "calibrate", "baseline", "run", "collect", "report"):
            self.assertIn(command, help_result.stdout)

    def test_baseline_list_is_machine_readable_without_hardware(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_cli("--state-dir", tmp, "--json", "baseline", "list")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["count"], 0)

    def test_legacy_execute_rejects_unsafe_ignored_modes(self) -> None:
        result = self.run_cli("execute", "--no-launch")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("diagnostic monitor", result.stdout)

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


if __name__ == "__main__":
    unittest.main()

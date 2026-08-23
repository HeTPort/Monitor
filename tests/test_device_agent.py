from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from device.avs_device_agent import EnvironmentController, EventWriter, KernelMonitor, run_agent
from src.events import EventDecoder


class DeviceAgentTests(unittest.TestCase):
    def test_single_writer_serializes_concurrent_producers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uart = root / "uart.log"
            writer = EventWriter("run-1", uart, root / "spool")
            writer.start()

            def produce(worker: int) -> None:
                for item in range(20):
                    writer.emit("agent", "capability", {"worker": worker, "item": item})

            threads = [threading.Thread(target=produce, args=(worker,)) for worker in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            writer.close()
            decoder = EventDecoder("run-1")
            events = decoder.feed(uart.read_bytes())
            decoder.finish()
            self.assertEqual(len(events), 80)
            self.assertEqual([event.seq for event in events], list(range(1, 81)))

    def test_end_to_end_workload_pipe_and_restoration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uart = root / "uart.log"
            state_path = root / "governor"
            state_path.write_text("ondemand", encoding="utf-8")
            workload_asset = root / "workload.marker"
            workload_asset.write_bytes(b"known-workload")
            summary = {"type": "summary", "result": "PASS", "exit_code": 0, "fps_avg": 60.0}
            script = f"import json; print(json.dumps({summary!r}))"
            manifest = {
                "schema_version": 1,
                "run_id": "agent-run",
                "target": "gpu",
                "uart": str(uart),
                "spool_dir": str(root / "spool"),
                "timeout_s": 10,
                "workload": {"argv": [sys.executable, "-c", script]},
                "assets": [
                    {
                        "path": str(workload_asset),
                        "sha256": hashlib.sha256(workload_asset.read_bytes()).hexdigest(),
                    }
                ],
                "environment": {"actions": [{"path": str(state_path), "value": "performance", "required": True}]},
                "telemetry": {"samplers": []},
                "kernel": {"mode": "off", "rules": []},
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            exit_code = run_agent(
                argparse.Namespace(
                    manifest=str(manifest_path),
                    uart=str(uart),
                    baudrate=115200,
                    spool_dir=None,
                    dry_run=False,
                )
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(state_path.read_text(encoding="utf-8"), "ondemand")
            decoder = EventDecoder("agent-run")
            events = decoder.feed(uart.read_bytes())
            decoder.finish()
            self.assertIn("summary", [event.type for event in events])
            self.assertEqual(events[-1].type, "agent_final")
            self.assertTrue(events[-1].payload["restoration_ok"])
            hashes = json.loads((root / "spool" / "artifact-hashes.json").read_text(encoding="utf-8"))
            self.assertIn("events.jsonl", hashes["sha256"])

    def test_dry_run_validates_without_uart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {
                "schema_version": 1,
                "run_id": "dry-run",
                "target": "cpu",
                "workload": {"argv": ["/bin/cpu-workload"]},
                "assets": [],
            }
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            code = run_agent(
                argparse.Namespace(
                    manifest=str(path), uart=None, baudrate=115200, spool_dir=str(root / "spool"), dry_run=True
                )
            )
            self.assertEqual(code, 0)
            self.assertFalse((root / "uart").exists())

    def test_kernel_filter_deduplicates_and_rate_limits(self) -> None:
        monitor = KernelMonitor(
            {"rules": [], "dedupe_window_ms": 1000, "max_events_per_second": 2},
            writer=None,  # type: ignore[arg-type]
            stop=threading.Event(),
            spool_dir=Path("."),
        )
        self.assertTrue(monitor._allow_event("same", 10.0))
        self.assertFalse(monitor._allow_event("same", 10.5))
        self.assertTrue(monitor._allow_event("different", 10.5))
        self.assertFalse(monitor._allow_event("third", 10.6))
        self.assertTrue(monitor._allow_event("same", 11.1))


if __name__ == "__main__":
    unittest.main()

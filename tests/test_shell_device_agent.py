from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import json
from pathlib import Path

from src.events import EventDecoder


ROOT = Path(__file__).parents[1]
AGENT = ROOT / "device" / "avs_device_agent.sh"


def shell_path(path: Path) -> str:
    resolved = path.resolve()
    if resolved.drive:
        return f"/{resolved.drive[0].lower()}{resolved.as_posix()[2:]}"
    return resolved.as_posix()


@unittest.skipUnless(shutil.which("sh"), "POSIX sh is required")
class ShellDeviceAgentTests(unittest.TestCase):
    def test_version_and_end_to_end_workload_stream(self) -> None:
        version = subprocess.run(
            ["sh", shell_path(AGENT), "--version"],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertEqual(version.stdout.strip(), "avs-device-agent 0.1.1 protocol 1")

        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            uart = root / "uart.jsonl"
            spool = root / "spool"
            state = root / "governor"
            state.write_text("ondemand\n", encoding="utf-8")
            temp_milli = root / "temp-milli"
            temp_degree = root / "temp-degree"
            temp_milli.write_text("31074\n", encoding="utf-8")
            temp_degree.write_text("30\n", encoding="utf-8")
            workload = root / "workload.sh"
            workload.write_text(
                "#!/bin/sh\n"
                "echo 'driver setup diagnostic'\n"
                "echo '{\"type\":\"heartbeat\",\"progress\":1}'\n"
                "echo '{\"type\":\"summary\",\"result\":\"PASS\",\"exit_code\":0}'\n",
                encoding="utf-8",
                newline="\n",
            )
            result = subprocess.run(
                [
                    "sh",
                    shell_path(AGENT),
                    "--run-id",
                    "shell-run",
                    "--target",
                    "cpu",
                    "--uart",
                    shell_path(uart),
                    "--spool-dir",
                    shell_path(spool),
                    "--cwd",
                    shell_path(root),
                    "--baudrate",
                    "9600",
                    "--timeout",
                    "5",
                    "--kernel-mode",
                    "off",
                    "--environment",
                    f"{shell_path(state)}|performance|1",
                    "--telemetry",
                    f"cpu.temperature.0|temperature_auto|{shell_path(temp_milli)}",
                    "--telemetry",
                    f"cpu.temperature.1|temperature_auto|{shell_path(temp_degree)}",
                    "--",
                    "sh",
                    shell_path(workload),
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(state.read_text(encoding="utf-8").strip(), "ondemand")
            decoder = EventDecoder("shell-run")
            events = decoder.feed(uart.read_bytes())
            decoder.finish()
            self.assertEqual(events[0].type, "agent_start")
            environment = [event for event in events if event.type == "environment"]
            self.assertTrue(any(event.payload.get("phase") == "readback" for event in environment))
            self.assertTrue(any(event.payload.get("phase") == "restore" for event in environment))
            self.assertTrue(all(event.payload.get("path") == shell_path(state) for event in environment))
            self.assertIn("summary", [event.type for event in events])
            invalid_output = [event for event in events if event.type == "error"]
            self.assertEqual(invalid_output[0].payload["error_code"], "WORKLOAD_OUTPUT_INVALID")
            self.assertEqual(invalid_output[0].payload["line"], "driver setup diagnostic")
            self.assertEqual(events[-1].type, "agent_final")
            self.assertTrue(events[-1].payload["restoration_ok"])
            temperatures = {
                event.payload["metric"]: event.payload["value"]
                for event in events
                if event.type == "telemetry" and "temperature" in event.payload.get("metric", "")
            }
            self.assertEqual(temperatures["cpu.temperature.0"], 31.074)
            self.assertEqual(temperatures["cpu.temperature.1"], 30.0)
            self.assertTrue((spool / "events.jsonl").exists())
            hashes = json.loads((spool / "artifact-hashes.json").read_text(encoding="utf-8"))
            self.assertEqual(len(hashes["sha256"]["events.jsonl"]), 64)


if __name__ == "__main__":
    unittest.main()

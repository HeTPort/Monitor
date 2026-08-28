from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
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
        self.assertEqual(version.stdout.strip(), "avs-device-agent 0.1.0 protocol 1")

        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            uart = root / "uart.jsonl"
            spool = root / "spool"
            state = root / "governor"
            state.write_text("ondemand\n", encoding="utf-8")
            workload = root / "workload.sh"
            workload.write_text(
                "#!/bin/sh\n"
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
            self.assertIn("summary", [event.type for event in events])
            self.assertEqual(events[-1].type, "agent_final")
            self.assertTrue(events[-1].payload["restoration_ok"])
            self.assertTrue((spool / "events.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

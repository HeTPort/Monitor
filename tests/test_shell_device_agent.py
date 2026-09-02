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
TELEMETRY = ROOT / "device" / "avs_telemetry_agent.sh"


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
        self.assertEqual(version.stdout.strip(), "avs-device-agent 0.3.2 protocol 2")

        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            uart = root / "uart.jsonl"
            spool = root / "spool"
            spool.mkdir()
            (spool / "workload.log").write_text("prior-attempt-marker\n", encoding="utf-8")
            workload = root / "workload.sh"
            workload.write_text(
                "#!/bin/sh\n"
                "echo 'driver setup diagnostic'\n"
                "echo '{\"type\":\"heartbeat\",\"progress\":1}'\n"
                "echo '{\"type\":\"summary\",\"result\":\"PASS\",\"exit_code\":0}'\n",
                encoding="utf-8",
                newline="\n",
            )
            relay = root / "relay.sh"
            relay.write_text(
                "#!/bin/sh\n"
                "uart=\n"
                "while [ $# -gt 0 ]; do\n"
                "  case \"$1\" in --uart) uart=$2; shift 2 ;; *) shift ;; esac\n"
                "done\n"
                "cat > \"$uart\"\n",
                encoding="utf-8",
                newline="\n",
            )
            relay.chmod(0o755)
            result = subprocess.run(
                [
                    "sh",
                    shell_path(AGENT),
                    "--test-id",
                    "shell-test",
                    "--attempt-id",
                    "shell-run",
                    "--target",
                    "cpu",
                    "--uart",
                    shell_path(uart),
                    "--relay",
                    shell_path(relay),
                    "--spool-dir",
                    shell_path(spool),
                    "--cwd",
                    shell_path(root),
                    "--baudrate",
                    "9600",
                    "--timeout",
                    "5",
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
            decoder = EventDecoder("shell-run")
            events = decoder.feed(uart.read_bytes())
            decoder.finish()
            self.assertEqual(events[0].type, "agent_start")
            self.assertTrue(all(event.raw.get("test_id") == "shell-test" for event in events))
            self.assertFalse(any(event.type == "environment" for event in events))
            self.assertFalse(any(event.type == "telemetry" for event in events))
            self.assertIn("summary", [event.type for event in events])
            invalid_output = [event for event in events if event.type == "error"]
            self.assertEqual(invalid_output[0].payload["error_code"], "WORKLOAD_OUTPUT_INVALID")
            self.assertEqual(events[-1].type, "agent_final")
            self.assertNotIn("restoration_ok", events[-1].payload)
            self.assertTrue(all(line.startswith(b"{") and line.endswith(b"}") for line in uart.read_bytes().splitlines()))
            self.assertTrue((spool / "events.jsonl").exists())
            workload_log = (spool / "workload.log").read_text(encoding="utf-8")
            self.assertTrue(workload_log.startswith("prior-attempt-marker\n"))
            self.assertIn("driver setup diagnostic", workload_log)
            final = json.loads((spool / "final.json").read_text(encoding="utf-8"))
            self.assertEqual(final["test_id"], "shell-test")
            self.assertEqual(final["attempt_id"], "shell-run")
            hashes = json.loads((spool / "artifact-hashes.json").read_text(encoding="utf-8"))
            self.assertEqual(len(hashes["sha256"]["events.jsonl"]), 64)

    def test_standalone_telemetry_appends_device_local_jsonl(self) -> None:
        version = subprocess.run(
            ["sh", shell_path(TELEMETRY), "--version"],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(version.returncode, 0, version.stderr)
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            value = root / "temperature"
            value.write_text("31074\n", encoding="utf-8")
            plan = root / "telemetry.conf"
            plan.write_text(
                f"cpu.temperature|temperature_auto|{shell_path(value)}\n",
                encoding="utf-8",
            )
            output = root / "device" / "telemetry.jsonl"
            result = subprocess.run(
                [
                    "sh",
                    shell_path(TELEMETRY),
                    "--test-id",
                    "telemetry-test",
                    "--attempt-id",
                    "telemetry-1",
                    "--target",
                    "cpu",
                    "--output",
                    shell_path(output),
                    "--plan",
                    shell_path(plan),
                    "--interval",
                    "1",
                    "--duration",
                    "0",
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            decoder = EventDecoder("telemetry-1")
            events = decoder.feed(output.read_bytes())
            decoder.finish()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].raw["test_id"], "telemetry-test")
            self.assertEqual(events[0].payload["metric"], "cpu.temperature")
            self.assertEqual(events[0].payload["value"], 31.074)

    def test_agent_bounds_non_cooperative_telemetry_and_still_emits_final(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            uart = root / "uart.jsonl"
            spool = root / "spool"
            spool.mkdir()
            workload = root / "workload.sh"
            workload.write_text(
                "#!/bin/sh\n"
                "echo '{\"type\":\"summary\",\"result\":\"PASS\",\"exit_code\":0}'\n",
                encoding="utf-8",
                newline="\n",
            )
            telemetry = root / "stubborn-telemetry.sh"
            telemetry.write_text(
                "#!/bin/sh\ntrap '' TERM\nwhile :; do sleep 1; done\n",
                encoding="utf-8",
                newline="\n",
            )
            plan = root / "telemetry.conf"
            plan.write_text("cpu.temperature|number|/missing\n", encoding="utf-8", newline="\n")
            relay = root / "relay.sh"
            relay.write_text(
                "#!/bin/sh\nuart=\nwhile [ $# -gt 0 ]; do\n"
                "  case \"$1\" in --uart) uart=$2; shift 2 ;; *) shift ;; esac\n"
                "done\ncat > \"$uart\"\n",
                encoding="utf-8",
                newline="\n",
            )
            relay.chmod(0o755)
            result = subprocess.run(
                [
                    "sh", shell_path(AGENT),
                    "--test-id", "telemetry-stop-test",
                    "--attempt-id", "telemetry-stop-attempt",
                    "--target", "cpu",
                    "--uart", shell_path(uart),
                    "--relay", shell_path(relay),
                    "--spool-dir", shell_path(spool),
                    "--cwd", shell_path(root),
                    "--timeout", "5",
                    "--telemetry-agent", shell_path(telemetry),
                    "--telemetry-plan", shell_path(plan),
                    "--telemetry-interval", "1",
                    "--telemetry-shutdown-timeout", "1",
                    "--", "sh", shell_path(workload),
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            decoder = EventDecoder("telemetry-stop-attempt")
            events = decoder.feed(uart.read_bytes())
            decoder.finish()
            self.assertEqual(events[-1].type, "agent_final")
            self.assertTrue(events[-1].payload["telemetry_timed_out"])
            self.assertTrue(any(
                event.type == "error" and event.payload.get("error_code") == "TELEMETRY_SHUTDOWN_TIMEOUT"
                for event in events
            ))
            final = json.loads((spool / "final.json").read_text(encoding="utf-8"))
            self.assertTrue(final["telemetry_timed_out"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import json
import hashlib
import os
import shlex
from pathlib import Path

from src.events import EventDecoder
from src.uart_protocol import UartV2Decoder, encode_uart_frame


ROOT = Path(__file__).parents[1]
AGENT = ROOT / "device" / "avs_device_agent.sh"
TELEMETRY = ROOT / "device" / "avs_telemetry_agent.sh"


def shell_path(path: Path) -> str:
    resolved = path.resolve()
    if resolved.drive:
        return f"/{resolved.drive[0].lower()}{resolved.as_posix()[2:]}"
    return resolved.as_posix()


@unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "native POSIX filesystem/FIFOs are required; run these tests in WSL/Linux")
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
        self.assertEqual(version.stdout.strip(), "avs-device-agent 0.4.0 protocol 2")

        with tempfile.TemporaryDirectory() as tmp:
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
                "cat > \"$uart\"\n"
                "printf 'relay closed after EOF\\n' >&2\n",
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
            native_summary = (spool / "workload-summary.json").read_bytes()
            self.assertEqual(json.loads(native_summary), {"type": "summary", "result": "PASS", "exit_code": 0})
            self.assertEqual(hashes["sha256"]["workload-summary.json"], hashlib.sha256(native_summary).hexdigest())
            relay_log = (spool / "relay.log").read_bytes()
            self.assertIn(b"relay closed after EOF", relay_log)
            self.assertEqual(hashes["sha256"]["relay.log"], hashlib.sha256(relay_log).hexdigest())

    def _run_verify_stream(
        self, script: str, *, target: str = "cpu", identifiers: tuple[str, str] = ("verify-test", "verify-run"),
        metrics: tuple[str, ...] = (), instrument_clock: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[dict], list[dict], int]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uart = root / "uart.jsonl"
            spool = root / "spool"
            workload = root / "workload.sh"
            workload.write_text("#!/bin/sh\n" + script, encoding="utf-8", newline="\n")
            relay = root / "relay.sh"
            relay.write_text(
                '#!/bin/sh\nuart=\nwhile [ $# -gt 0 ]; do\n'
                'case "$1" in --uart) uart=$2; shift 2 ;; *) shift ;; esac\n'
                'done\ncat > "$uart"\n', encoding="utf-8", newline="\n",
            )
            relay.chmod(0o755)
            environment = os.environ.copy()
            trace = root / "clock.calls"
            if instrument_clock:
                clock = shutil.which("date")
                self.assertIsNotNone(clock)
                shim = root / "date"
                shim.write_text(
                    '#!/bin/sh\nprintf "date\\n" >> "$CLOCK_TRACE"\n'
                    f'exec {shlex.quote(shell_path(Path(clock)))} "$@"\n',
                    encoding="utf-8", newline="\n",
                )
                shim.chmod(0o755)
                environment["PATH"] = str(root) + os.pathsep + environment["PATH"]
                environment["CLOCK_TRACE"] = shell_path(trace)
            command = [
                "sh", shell_path(AGENT), "--test-id", identifiers[0], "--attempt-id", identifiers[1],
                "--target", target, "--uart", shell_path(uart), "--relay", shell_path(relay),
                "--spool-dir", shell_path(spool), "--cwd", shell_path(root),
                "--baudrate", "9600", "--max-frame", "512", "--timeout", "10",
            ]
            for metric in metrics:
                command.extend(("--summary-metric", metric))
            command.extend(("--", "sh", shell_path(workload)))
            result = subprocess.run(command, text=True, encoding="utf-8", capture_output=True, timeout=20, env=environment)
            records = [json.loads(line) for line in uart.read_text(encoding="utf-8").splitlines()] if uart.exists() else []
            local = [json.loads(line) for line in (spool / "events.jsonl").read_text(encoding="utf-8").splitlines()] if spool.exists() else []
            clock_calls = len(trace.read_text(encoding="utf-8").splitlines()) if trace.exists() else 0
            if records:
                frames = [encode_uart_frame(record) for record in records]
                self.assertTrue(all(len(frame) - 2 <= 512 for frame in frames))
                decoder = UartV2Decoder(identifiers[1], identifiers[0], max_frame_bytes=512)
                decoded = decoder.feed(b"".join(frames))
                decoder.finish()
                self.assertEqual(len(decoded), len(records))
                self.assertEqual(decoded[-1].type, "agent_final")
            return result, records, local, clock_calls

    def test_first_native_verify_failure_survives_missing_summary_and_failure_flood(self) -> None:
        for target, failure in (
            ("cpu", '{"type":"verify","batch":1,"result":"FAIL","mismatch_count":7,"checksum":"0x1234","golden_checksum":"0x5678"}'),
            ("gpu", '{"type":"verify","frame":1,"pass":false,"pixel_diff_count":3}'),
        ):
            with self.subTest(target=target):
                result, records, local, _ = self._run_verify_stream(
                    f"i=0\nwhile [ $i -lt 1000 ]; do\nprintf '%s\\n' '{failure}'\ni=$((i+1))\ndone\nexit 1\n",
                    target=target,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                failures = [record for record in records if record["type"] == "verify"]
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["payload"]["result"], "FAIL")
                self.assertIs(failures[0]["payload"]["pass"], False)
                self.assertEqual(failures[0]["payload"]["batch" if target == "cpu" else "frame"], 1)
                self.assertEqual(len([record for record in local if record["type"] == "verify"]), 1000)
                self.assertFalse(any(record["type"] == "summary" for record in records))
                self.assertIs(records[-1]["payload"]["summary_seen"], False)

    def test_success_flood_preserves_control_events_without_per_line_clock_processes(self) -> None:
        summary = {
            "type": "summary", "result": "PASS", "exit_code": 0, "verify_pass": True,
            "contract_version": 2, "verify_mode": "checksum", "verify_count": 6000, "verify_fail_count": 0,
            "verify_interval": 1, "success_log_interval": 0, "batch_count": 6000,
        }
        script = (
            "printf '%s\\n' '{\"type\":\"heartbeat\"}'\n"
            "i=0\nwhile [ $i -lt 6000 ]; do\n"
            "printf '%s\\n' '{\"type\":\"verify\",\"result\":\"PASS\"}'\ni=$((i+1))\ndone\n"
            "sleep 1\nprintf '%s\\n' '{\"type\":\"heartbeat\"}'\n"
            f"printf '%s\\n' '{json.dumps(summary, separators=(',', ':'))}'\n"
        )
        result, records, local, clock_calls = self._run_verify_stream(script, instrument_clock=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len([record for record in local if record["type"] == "verify"]), 6000)
        self.assertFalse(any(record["type"] == "verify" for record in records))
        self.assertEqual(len([record for record in records if record["type"] == "heartbeat"]), 2)
        self.assertLess(clock_calls, 30, "successful verify must not spawn date per record")
        verification = {}
        for record in records:
            if record["type"] == "verification_summary":
                verification.update(record["payload"])
        self.assertEqual(verification["verify_count"], 6000)
        self.assertEqual(verification["success_log_interval"], 0)
        self.assertEqual(verification["contract_version"], 2)
        self.assertEqual([record["type"] for record in records][-2:], ["summary", "agent_final"])

    def test_large_verification_evidence_is_split_and_missing_fields_are_not_fabricated(self) -> None:
        maximum = 18446744073709551615
        summary = {
            "type": "summary", "result": "PASS", "exit_code": 0, "verify_pass": True,
            "contract_version": 2, "verify_count": maximum, "verify_fail_count": maximum,
            "verify_mode": "golden-image", "verify_interval": maximum, "success_log_interval": maximum,
            "frame_count": maximum, "fps": 1234567890.123456, "frame_time_p99_ms": 1234567890.123456,
        }
        result, records, _, _ = self._run_verify_stream(
            f"printf '%s\\n' '{json.dumps(summary, separators=(',', ':'))}'\n", target="gpu",
            identifiers=("t" * 60, "r" * 70), metrics=("fps", "frame_time_p99_ms"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        parts = [record for record in records if record["type"] == "verification_summary"]
        self.assertGreaterEqual(len(parts), 2)
        merged = {}
        for part in parts:
            self.assertFalse(set(merged) & set(part["payload"]))
            merged.update(part["payload"])
        self.assertEqual(merged, {key: summary[key] for key in (
            "contract_version", "verify_count", "verify_fail_count", "verify_mode", "verify_interval", "success_log_interval", "frame_count",
        )})
        terminal = next(record for record in records if record["type"] == "summary")
        self.assertEqual(terminal["payload"]["fps"], summary["fps"])
        self.assertFalse(any(record["type"] == "error" for record in records))
        result, records, _, _ = self._run_verify_stream(
            "printf '%s\\n' '{\"type\":\"summary\",\"result\":\"PASS\",\"exit_code\":0}'\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(record["type"] == "verification_summary" for record in records))

    def test_impossible_identifiers_are_rejected_before_workload_launch(self) -> None:
        result, records, local, _ = self._run_verify_stream("exit 99\n", identifiers=("t" * 200, "r" * 200))
        self.assertEqual(result.returncode, 2)
        self.assertIn("insufficient space", result.stderr)
        self.assertEqual(records, [])
        self.assertEqual(local, [])

    def test_standalone_telemetry_appends_device_local_jsonl(self) -> None:
        version = subprocess.run(
            ["sh", shell_path(TELEMETRY), "--version"],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(version.returncode, 0, version.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = root / "temperature"
            value.write_text("31074\n", encoding="utf-8")
            plan = root / "telemetry.conf"
            plan.write_text(
                f"required|cpu.temperature|temperature_auto|all|{shell_path(value)}\n",
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
            self.assertTrue(events[0].payload["complete"])
            self.assertEqual(events[0].payload["sample_id"], 1)
            self.assertEqual(events[0].payload["metrics"]["cpu.temperature"], [31.074])
            self.assertEqual(events[0].payload["missing_required"], [])

    def test_telemetry_snapshot_rejects_missing_required_and_parses_prefixed_gpu_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            utilization = root / "gpu_utilisation"
            utilization.write_text("Gpu utilisation : 37\n", encoding="utf-8")
            plan = root / "telemetry.conf"
            plan.write_text(
                f"required|gpu.utilization|prefixed_number|first|{shell_path(utilization)}\n"
                f"required|gpu.temperature|temperature_auto|first|{shell_path(root / 'missing')}\n",
                encoding="utf-8",
            )
            output = root / "spool" / "telemetry.jsonl"
            result = subprocess.run(
                [
                    "sh", shell_path(TELEMETRY), "--test-id", "gpu-telemetry",
                    "--attempt-id", "gpu-telemetry-1", "--target", "gpu",
                    "--output", shell_path(output), "--plan", shell_path(plan),
                    "--interval", "1", "--duration", "0",
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 5, result.stderr)
            event = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(event["payload"]["complete"])
            self.assertEqual(event["payload"]["metrics"]["gpu.utilization"], [37])
            self.assertEqual(event["payload"]["missing_required"], ["gpu.temperature"])

    def test_telemetry_plan_without_required_metrics_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = root / "optional"
            value.write_text("1\n", encoding="utf-8")
            plan = root / "telemetry.conf"
            plan.write_text(
                f"optional|cpu.idle_residency|number|all|{shell_path(value)}\n",
                encoding="utf-8",
            )
            output = root / "spool" / "telemetry.jsonl"
            result = subprocess.run(
                [
                    "sh", shell_path(TELEMETRY), "--test-id", "empty-plan",
                    "--attempt-id", "empty-plan-1", "--target", "cpu",
                    "--output", shell_path(output), "--plan", shell_path(plan),
                    "--interval", "1", "--duration", "0",
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 5)
            self.assertIn("no required metrics", result.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "")

    def test_agent_bounds_non_cooperative_telemetry_and_still_emits_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
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

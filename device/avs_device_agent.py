#!/usr/bin/env python3
"""Fixed manifest-driven device agent for initial HarmonyOS bring-up.

The agent has exactly one UART writer. Workload, telemetry, and filtered kernel
producers submit structured events to its queue and never open the UART.
"""

from __future__ import annotations

import argparse
from collections import deque
import glob
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Mapping


AGENT_VERSION = "0.1.0"
EVENT_SCHEMA_VERSION = 1
EVENT_TYPES = {
    "agent_start", "capability", "environment", "start", "heartbeat", "batch",
    "verify", "golden", "telemetry", "kernel", "error", "summary", "violation",
    "agent_final",
}


class AgentError(RuntimeError):
    pass


def canonical_crc(record: Mapping[str, Any]) -> str:
    content = {key: value for key, value in record.items() if key != "crc32"}
    payload = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_spool_hashes(spool_dir: Path) -> Path:
    hashes = {
        path.relative_to(spool_dir).as_posix(): sha256_file(path)
        for path in sorted(spool_dir.rglob("*"))
        if path.is_file() and path.name != "artifact-hashes.json"
    }
    destination = spool_dir / "artifact-hashes.json"
    temporary = spool_dir / ".artifact-hashes.json.tmp"
    temporary.write_text(
        json.dumps({"schema_version": 1, "sha256": hashes}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return destination


class EventWriter:
    """The only class allowed to open and write the device UART."""

    def __init__(self, run_id: str, uart: Path | None, spool_dir: Path, *, include_crc: bool = True):
        self.run_id = run_id
        self.uart = uart
        self.spool_dir = spool_dir
        self.include_crc = include_crc
        self.items: queue.Queue[tuple[str, str, dict[str, Any]] | None] = queue.Queue()
        self.started = time.monotonic()
        self.seq = 0
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="uart-writer", daemon=True)

    def start(self) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def emit(self, source: str, event_type: str, payload: Mapping[str, Any]) -> None:
        if event_type not in EVENT_TYPES:
            raise AgentError(f"unsupported event type: {event_type}")
        if self.error is not None:
            raise AgentError(f"event writer failed: {self.error}")
        self.items.put((source, event_type, dict(payload)))

    def close(self) -> None:
        self.items.put(None)
        self.thread.join(timeout=10.0)
        if self.thread.is_alive():
            raise AgentError("event writer did not stop")
        if self.error is not None:
            raise AgentError(f"event writer failed: {self.error}")

    def _run(self) -> None:
        uart_stream = None
        try:
            spool_path = self.spool_dir / "events.jsonl"
            with spool_path.open("ab", buffering=0) as spool:
                if self.uart is not None:
                    uart_stream = self.uart.open("ab", buffering=0)
                while True:
                    item = self.items.get()
                    if item is None:
                        break
                    source, event_type, payload = item
                    self.seq += 1
                    record: dict[str, Any] = {
                        "schema_version": EVENT_SCHEMA_VERSION,
                        "run_id": self.run_id,
                        "seq": self.seq,
                        "timestamp_ms": int((time.monotonic() - self.started) * 1000),
                        "source": source,
                        "type": event_type,
                        "payload": payload,
                    }
                    if self.include_crc:
                        record["crc32"] = canonical_crc(record)
                    wire = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
                    spool.write(wire)
                    if uart_stream is not None:
                        uart_stream.write(wire)
        except BaseException as exc:  # Preserve the writer failure for the owner.
            self.error = exc
        finally:
            if uart_stream is not None:
                uart_stream.close()


class EnvironmentController:
    def __init__(self, actions: list[dict[str, Any]], writer: EventWriter):
        self.actions = actions
        self.writer = writer
        self.original: list[tuple[Path, str]] = []

    def apply(self) -> None:
        for action in self.actions:
            path = Path(str(action.get("path", "")))
            value = str(action.get("value", ""))
            required = bool(action.get("required", True))
            if not path.is_absolute():
                raise AgentError(f"environment path must be absolute: {path}")
            try:
                before = path.read_text(encoding="utf-8").strip()
                self.original.append((path, before))
                path.write_text(value, encoding="utf-8")
                after = path.read_text(encoding="utf-8").strip()
                matched = after == value.strip()
                self.writer.emit(
                    "agent",
                    "environment",
                    {"path": str(path), "requested": value, "before": before, "readback": after, "matched": matched},
                )
                if required and not matched:
                    raise AgentError(f"environment readback mismatch: {path}")
            except OSError as exc:
                self.writer.emit(
                    "agent",
                    "error",
                    {"origin": "agent", "error_code": "ENVIRONMENT_APPLY_FAILED", "path": str(path), "message": str(exc)},
                )
                if required:
                    raise AgentError(f"environment apply failed for {path}: {exc}") from exc

    def restore(self) -> tuple[bool, list[dict[str, str]]]:
        errors: list[dict[str, str]] = []
        for path, value in reversed(self.original):
            try:
                path.write_text(value, encoding="utf-8")
                readback = path.read_text(encoding="utf-8").strip()
                if readback != value:
                    errors.append({"path": str(path), "message": f"readback={readback!r}, expected={value!r}"})
            except OSError as exc:
                errors.append({"path": str(path), "message": str(exc)})
        return not errors, errors


class TelemetrySampler(threading.Thread):
    def __init__(self, config: Mapping[str, Any], writer: EventWriter, stop: threading.Event):
        super().__init__(name="telemetry", daemon=True)
        self.interval = max(float(config.get("interval_ms", 1000)) / 1000.0, 0.05)
        self.samplers = config.get("samplers", [])
        self.source = str(config.get("source", "agent"))
        self.writer = writer
        self.stop_event = stop
        self.previous_proc_stat: dict[str, tuple[int, int]] = {}

    def run(self) -> None:
        while not self.stop_event.is_set():
            metrics: dict[str, Any] = {}
            provenance: dict[str, str] = {}
            errors: list[dict[str, str]] = []
            for sampler in self.samplers:
                try:
                    self._sample(sampler, metrics, provenance)
                except (OSError, ValueError, TypeError) as exc:
                    errors.append({"metric": str(sampler.get("metric", "unknown")), "message": str(exc)})
            self.writer.emit(self.source, "telemetry", {"metrics": metrics, "provenance": provenance, "errors": errors})
            self.stop_event.wait(self.interval)

    def _sample(self, sampler: Mapping[str, Any], metrics: dict[str, Any], provenance: dict[str, str]) -> None:
        metric = str(sampler["metric"])
        parser = str(sampler.get("parser", "float"))
        paths = sampler.get("paths") or [sampler.get("path")]
        matched: list[str] = []
        for candidate in paths:
            if not candidate:
                continue
            expanded = sorted(glob.glob(str(candidate))) if any(char in str(candidate) for char in "*?[") else [str(candidate)]
            matched.extend(expanded)
        if parser == "proc_stat_utilization":
            self._sample_proc_stat(Path(matched[0]), metric, metrics, provenance)
            return
        for index, path_text in enumerate(matched):
            path = Path(path_text)
            raw = path.read_text(encoding="utf-8").strip()
            value: Any
            if parser in {"int", "online"}:
                value = int(raw)
            elif parser == "millidegree_celsius":
                value = float(raw) / 1000.0
            elif parser == "float":
                value = float(raw)
            elif parser == "number":
                match = re.search(r"[-+]?\d+(?:\.\d+)?", raw)
                if not match:
                    raise ValueError(f"no numeric value in {path}: {raw!r}")
                numeric = float(match.group(0))
                value = int(numeric) if numeric.is_integer() else numeric
            else:
                value = raw
            key = metric if len(matched) == 1 else f"{metric}.{self._path_suffix(path_text, index)}"
            metrics[key] = value
            provenance[key] = path_text

    def _sample_proc_stat(
        self,
        path: Path,
        metric: str,
        metrics: dict[str, Any],
        provenance: dict[str, str],
    ) -> None:
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if not parts or not re.fullmatch(r"cpu\d+", parts[0]):
                continue
            values = [int(item) for item in parts[1:]]
            idle = sum(values[3:5]) if len(values) >= 5 else values[3]
            total = sum(values)
            previous = self.previous_proc_stat.get(parts[0])
            self.previous_proc_stat[parts[0]] = (total, idle)
            if previous and total > previous[0]:
                utilization = 100.0 * (1.0 - (idle - previous[1]) / (total - previous[0]))
                key = f"{metric}.{parts[0]}"
                metrics[key] = round(max(0.0, min(100.0, utilization)), 3)
                provenance[key] = str(path)

    @staticmethod
    def _path_suffix(path: str, fallback: int) -> str:
        for pattern in (r"cpu(\d+)", r"thermal_zone(\d+)", r"state(\d+)"):
            match = re.search(pattern, path)
            if match:
                return match.group(1)
        return str(fallback)


class KernelMonitor(threading.Thread):
    def __init__(self, config: Mapping[str, Any], writer: EventWriter, stop: threading.Event, spool_dir: Path):
        super().__init__(name="kernel", daemon=True)
        self.config = config
        self.writer = writer
        self.stop_event = stop
        self.spool_dir = spool_dir
        self.process: subprocess.Popen[str] | None = None
        self.dedupe_window_s = max(0.0, float(config.get("dedupe_window_ms", 1000)) / 1000.0)
        self.max_events_per_second = max(1, int(config.get("max_events_per_second", 10)))
        self.last_emitted: dict[str, float] = {}
        self.rate_window: deque[float] = deque()
        self.suppressed = 0
        self.rules = [
            (str(rule.get("id", "kernel-rule")), str(rule.get("severity", "critical")), re.compile(str(rule["pattern"]), re.I))
            for rule in config.get("rules", [])
        ]

    def run(self) -> None:
        if str(self.config.get("mode", "critical")) == "off":
            return
        raw_stream = None
        try:
            if self.config.get("raw_local", False):
                raw_stream = (self.spool_dir / "dmesg.raw").open("a", encoding="utf-8")
            self.process = subprocess.Popen(
                ["dmesg", "-w"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert self.process.stdout is not None
            for line in self.process.stdout:
                if self.stop_event.is_set():
                    break
                clean = line.rstrip("\r\n")
                if raw_stream is not None:
                    raw_stream.write(clean + "\n")
                    raw_stream.flush()
                for rule_id, severity, pattern in self.rules:
                    if pattern.search(clean):
                        now = time.monotonic()
                        if not self._allow_event(f"{rule_id}\0{clean}", now):
                            self.suppressed += 1
                            break
                        suppressed = self.suppressed
                        self.suppressed = 0
                        self.writer.emit(
                            "kernel",
                            "kernel",
                            {
                                "rule_id": rule_id,
                                "severity": severity,
                                "message": clean,
                                "suppressed_since_previous": suppressed,
                            },
                        )
                        break
        except (OSError, subprocess.SubprocessError) as exc:
            if self.config.get("required", False):
                self.writer.emit(
                    "agent", "error", {"origin": "agent", "error_code": "KERNEL_MONITOR_FAILED", "message": str(exc)}
                )
        finally:
            if self.suppressed:
                self.writer.emit(
                    "agent",
                    "capability",
                    {"name": "kernel.filter", "suppressed_events": self.suppressed},
                )
            if raw_stream is not None:
                raw_stream.close()

    def _allow_event(self, key: str, now: float) -> bool:
        previous = self.last_emitted.get(key)
        if previous is not None and now - previous < self.dedupe_window_s:
            return False
        while self.rate_window and now - self.rate_window[0] >= 1.0:
            self.rate_window.popleft()
        if len(self.rate_window) >= self.max_events_per_second:
            return False
        self.last_emitted[key] = now
        self.rate_window.append(now)
        return True

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentError(f"cannot load manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise AgentError("manifest must be a schema_version 1 object")
    for key in ("run_id", "target", "workload"):
        if key not in data:
            raise AgentError(f"manifest missing required field: {key}")
    workload = data["workload"]
    if not isinstance(workload, dict) or not isinstance(workload.get("argv"), list):
        raise AgentError("manifest workload.argv must be a list")
    if not workload["argv"] or not all(isinstance(item, str) and "\x00" not in item for item in workload["argv"]):
        raise AgentError("manifest workload.argv must contain safe strings")
    if data["target"] not in {"cpu", "gpu"}:
        raise AgentError("manifest target must be cpu or gpu")
    return data


def validate_assets(manifest: Mapping[str, Any]) -> None:
    for asset in manifest.get("assets", []):
        path = Path(str(asset["path"]))
        expected = str(asset["sha256"]).lower()
        if not path.exists() or sha256_file(path) != expected:
            raise AgentError(f"asset hash validation failed: {path}")


def workload_reader(stream: Any, writer: EventWriter, target: str, state: dict[str, Any]) -> None:
    for raw_line in stream:
        line = raw_line.rstrip("\r\n")
        if not line:
            continue
        try:
            record = json.loads(line)
            event_type = record.get("type") if isinstance(record, dict) else None
            if event_type not in EVENT_TYPES:
                raise ValueError(f"unsupported type {event_type!r}")
            if event_type == "summary":
                state["summary_seen"] = True
            writer.emit(f"{target}-workload", event_type, record)
        except (json.JSONDecodeError, ValueError) as exc:
            writer.emit(
                "agent",
                "error",
                {"origin": "agent", "error_code": "WORKLOAD_OUTPUT_INVALID", "message": str(exc), "line": line[:512]},
            )


def run_agent(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve(strict=True)
    manifest = load_manifest(manifest_path)
    validate_assets(manifest)
    run_id = str(manifest["run_id"])
    spool_dir = Path(args.spool_dir or manifest.get("spool_dir") or manifest_path.parent).resolve()
    if args.dry_run:
        spool_dir.mkdir(parents=True, exist_ok=True)
        print(json.dumps({"valid": True, "run_id": run_id, "manifest": str(manifest_path), "spool_dir": str(spool_dir)}))
        return 0

    uart_value = args.uart or manifest.get("uart")
    if not isinstance(uart_value, str) or not uart_value:
        raise AgentError("UART is required via --uart or manifest.uart")
    uart = Path(uart_value)
    stop = threading.Event()
    writer = EventWriter(run_id, uart, spool_dir, include_crc=bool(manifest.get("event_crc", True)))
    environment = EnvironmentController(list(manifest.get("environment", {}).get("actions", [])), writer)
    telemetry = TelemetrySampler(manifest.get("telemetry", {}), writer, stop)
    kernel = KernelMonitor(manifest.get("kernel", {}), writer, stop, spool_dir)
    workload: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None
    reader_state: dict[str, Any] = {"summary_seen": False}
    restoration_ok = True
    restoration_errors: list[dict[str, str]] = []
    workload_exit: int | None = None
    timed_out = False

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()
        if workload is not None and workload.poll() is None:
            workload.terminate()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    writer.start()
    writer.emit(
        "agent",
        "agent_start",
        {
            "agent_version": AGENT_VERSION,
            "protocol_version": EVENT_SCHEMA_VERSION,
            "manifest_sha256": sha256_file(manifest_path),
            "baudrate": args.baudrate,
        },
    )
    try:
        environment.apply()
        if telemetry.samplers:
            telemetry.start()
        if kernel.rules or manifest.get("kernel", {}).get("required", False):
            kernel.start()
        workload_config = manifest["workload"]
        argv = workload_config["argv"]
        cwd = workload_config.get("cwd")
        env = os.environ.copy()
        env.update({str(key): str(value) for key, value in workload_config.get("env", {}).items()})
        workload = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert workload.stdout is not None
        reader = threading.Thread(
            target=workload_reader,
            args=(workload.stdout, writer, manifest["target"], reader_state),
            name="workload-reader",
            daemon=True,
        )
        reader.start()
        deadline = time.monotonic() + float(manifest.get("timeout_s", 300))
        while workload.poll() is None and not stop.is_set():
            if time.monotonic() >= deadline:
                timed_out = True
                workload.terminate()
                break
            time.sleep(0.1)
        if workload.poll() is None:
            try:
                workload.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                workload.kill()
        workload_exit = workload.wait()
        if reader is not None:
            reader.join(timeout=5.0)
    except BaseException as exc:
        writer.emit("agent", "error", {"origin": "agent", "error_code": "AGENT_RUN_FAILED", "message": str(exc)})
    finally:
        stop.set()
        kernel.stop()
        if telemetry.is_alive():
            telemetry.join(timeout=5.0)
        if kernel.is_alive():
            kernel.join(timeout=5.0)
        restoration_ok, restoration_errors = environment.restore()
        writer.emit(
            "agent",
            "agent_final",
            {
                "workload_exit_code": workload_exit,
                "summary_seen": bool(reader_state["summary_seen"]),
                "timed_out": timed_out,
                "restoration_ok": restoration_ok,
                "restoration_errors": restoration_errors,
                "spool_complete": True,
                "aborted": stop.is_set() and workload_exit is None,
            },
        )
        writer.close()
        write_spool_hashes(spool_dir)
    if not restoration_ok:
        return 3
    if workload_exit is None:
        return 3
    return max(0, min(int(workload_exit), 125))


def setup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="avs-device-agent")
    parser.add_argument("--manifest", required=False)
    parser.add_argument("--uart")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--spool-dir")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--version", action="version", version=f"avs-device-agent {AGENT_VERSION} protocol {EVENT_SCHEMA_VERSION}")
    return parser


def main() -> int:
    parser = setup_parser()
    args = parser.parse_args()
    if not args.manifest:
        parser.error("--manifest is required")
    try:
        return run_agent(args)
    except (AgentError, OSError) as exc:
        print(f"avs-device-agent: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

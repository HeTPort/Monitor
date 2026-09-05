"""Minimal profile-driven workload/UART execution and evaluation."""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Mapping

from .artifact_store import ArtifactStore, atomic_write_json, sha256_file
from .baselines import Baseline
from .config_loader import PlatformConfig, ProfileConfig, document_sha256
from .events import EventDecoder, EventProtocolError
from .path_resolver import PathResolver
from .policy_engine import PolicyEngine, PolicyLimits, PolicyResult
from .transport import CommandResult, Transport
from .uart_protocol import UART_PROTOCOL, UartV2Decoder, frame_wire_seconds


class RunError(RuntimeError):
    pass


class RunInfrastructureError(RunError):
    """A runtime transport/orchestration failure, not invalid input."""


def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def _safe_identifier(value: str, name: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    if not value or any(character not in allowed for character in value):
        raise RunError(f"unsafe {name}: {value!r}")
    return value


class RunManifestBuilder:
    """Build a small runtime manifest without probing, deploying, or changing the device."""

    def __init__(self, paths: PathResolver):
        self.paths = paths

    def build(
        self,
        *,
        profile: ProfileConfig,
        baseline: Baseline | None = None,
        golden: Mapping[str, Any] | None = None,
        capabilities: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        test_id: str | None = None,
        attempt_id: str | None = None,
        kernel_mode: str | None = None,
        overall_timeout_s: float = 300.0,
        heartbeat_timeout_s: float = 45.0,
        kernel_rules_path: Path | None = None,
        device_uart: str | None = None,
        telemetry_enabled: bool = False,
        pc_artifacts: str = "result",
    ) -> dict[str, Any]:
        del capabilities, kernel_mode, kernel_rules_path
        if not device_uart:
            raise RunError("device UART must be resolved from an explicit value, saved pairing, or platform config")
        if pc_artifacts not in {"result", "full"}:
            raise RunError("pc_artifacts must be result or full")

        resolved_test_id = _safe_identifier(test_id or run_id or new_run_id(profile.target), "test_id")
        resolved_attempt_id = _safe_identifier(attempt_id or run_id or resolved_test_id, "attempt_id")
        if baseline is not None and golden is not None:
            raise RunError("baseline and golden reference are mutually exclusive")
        if baseline is not None:
            self._validate_baseline(profile, baseline)
        if golden is not None:
            if profile.target == "cpu" and not golden.get("checksum"):
                raise RunError("CPU golden reference is missing checksum")
            if profile.target == "gpu" and not golden.get("remote_path"):
                raise RunError("GPU golden reference is missing remote_path")

        remote_attempt = self.paths.remote(PurePosixPath("tests") / resolved_test_id / resolved_attempt_id)
        remote_binary = self.paths.remote(
            str(profile.workload.get("remote_binary", f"bin/{profile.target}-avs-workload"))
        )
        remote_config = self.paths.remote(f"configs/{profile.name}.json")
        workload_argv = [str(remote_binary), "--config", str(remote_config)]
        if baseline is not None:
            if profile.target == "cpu" and baseline.golden.get("checksum"):
                workload_argv.extend(("--golden-checksum", str(baseline.golden["checksum"])))
            if profile.target == "gpu" and baseline.golden.get("remote_path"):
                workload_argv.extend(("--golden-file", str(baseline.golden["remote_path"])))
        elif golden is not None:
            if profile.target == "cpu" and golden.get("checksum"):
                workload_argv.extend(("--golden-checksum", str(golden["checksum"])))
            if profile.target == "gpu" and golden.get("remote_path"):
                workload_argv.extend(("--golden-file", str(golden["remote_path"])))

        thresholds = json.loads(json.dumps(baseline.thresholds)) if baseline is not None else {}
        platform_value = profile.platform
        platform_candidate = (
            platform_value
            if Path(platform_value).suffix.lower() in {".json", ".yaml", ".yml"}
            else f"config/platforms/{platform_value}.yaml"
        )
        platform_path = self.paths.resolve_input(platform_candidate, required=False)
        platform = PlatformConfig.from_file(platform_path) if platform_path.exists() else None
        platform_serial = platform.serial if platform is not None else {}
        relay_config = dict(platform_serial.get("relay", {}))
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "producer": {"name": "vmin_judge", "component": "RunManifestBuilder", "version": "2.1.0"},
            "test_id": resolved_test_id,
            "attempt_id": resolved_attempt_id,
            "run_id": resolved_attempt_id,
            "profile": {"id": profile.name, "sha256": profile.fingerprint},
            "baseline": {"id": baseline.id, "sha256": baseline.sha256} if baseline is not None else None,
            "golden_reference": (
                {
                    "qualification_id": golden.get("qualification_id"),
                    "correctness_fingerprint": golden.get("correctness_fingerprint"),
                }
                if golden is not None
                else None
            ),
            "validation_mode": (
                "baseline" if baseline is not None else "golden-reference" if golden is not None else "error-only"
            ),
            "platform": profile.platform,
            "target": profile.target,
            "uart": str(PurePosixPath(device_uart)),
            "device_attempt_dir": str(remote_attempt),
            "spool_dir": str(remote_attempt / "spool"),
            "timeout_s": overall_timeout_s,
            "overall_timeout_s": overall_timeout_s,
            "heartbeat_timeout_s": heartbeat_timeout_s,
            "event_crc": False,
            "agent_backend": "posix-shell",
            "serial_transport": {
                "protocol": str(platform_serial.get("protocol", UART_PROTOCOL)),
                "max_frame_bytes": int(platform_serial.get("max_frame_bytes", 512)),
                "tail_guard_bytes": int(platform_serial.get("tail_guard_bytes", 64)),
                "safe_utilization": float(platform_serial.get("safe_utilization", 0.70)),
                "relay": str(self.paths.remote(relay_config.get("remote_asset", "bin/avs-uart-relay"))),
            },
            "pc_artifacts": pc_artifacts,
            "workload": {
                "argv": workload_argv,
                "cwd": str(self.paths.device_root),
                "config_path": str(remote_config),
            },
            "scheduler": {"managed_by_monitor": False, "requirements": dict(profile.environment)},
            "telemetry": {
                "enabled": bool(telemetry_enabled),
                "device_local": True,
                "plan": str(self.paths.remote(f"configs/telemetry/{profile.name}.conf")),
                "agent": str(self.paths.remote("bin/avs-telemetry-agent")),
                "interval_s": max(1, (int(profile.telemetry.get("interval_ms", 5000)) + 999) // 1000),
                "shutdown_timeout_s": 10,
            },
            "logs": {"device_local": True, "streamed_to_pc": False},
            "policy": {"thresholds": thresholds, "required_telemetry": []},
            "assets": [],
        }
        manifest["manifest_sha256"] = document_sha256(manifest)
        return manifest

    def build_qualification(
        self,
        *,
        profile: ProfileConfig,
        golden: Mapping[str, Any],
        capabilities: Mapping[str, Any] | None,
        mode: str,
        run_id: str | None = None,
        test_id: str | None = None,
        attempt_id: str | None = None,
        kernel_mode: str | None = None,
        overall_timeout_s: float = 300.0,
        heartbeat_timeout_s: float = 45.0,
        workload_guard_s: float | None = None,
        final_timeout_s: float | None = None,
        kernel_rules_path: Path | None = None,
        device_uart: str | None = None,
        pc_artifacts: str = "full",
        telemetry_enabled: bool = False,
    ) -> dict[str, Any]:
        if mode not in {"smoke", "golden", "calibration"}:
            raise RunError(f"unsupported qualification mode: {mode}")
        manifest = self.build(
            profile=profile,
            baseline=None,
            capabilities=capabilities,
            run_id=run_id,
            test_id=test_id,
            attempt_id=attempt_id,
            kernel_mode=kernel_mode,
            overall_timeout_s=overall_timeout_s,
            heartbeat_timeout_s=heartbeat_timeout_s,
            kernel_rules_path=kernel_rules_path,
            device_uart=device_uart,
            telemetry_enabled=telemetry_enabled,
            pc_artifacts=pc_artifacts,
        )
        manifest["qualification"] = {
            "mode": mode,
            "production_baseline_allowed": False,
            "generated_reference_disposition": "qualification-artifact" if mode == "golden" else "not-generated",
        }
        if workload_guard_s is not None:
            if workload_guard_s <= 0:
                raise RunError("qualification workload guard must be positive")
            manifest["timeout_s"] = float(workload_guard_s)
            manifest["qualification"]["workload_guard_s"] = float(workload_guard_s)
        if final_timeout_s is not None:
            if final_timeout_s <= 0:
                raise RunError("qualification FINAL timeout must be positive")
            manifest["final_timeout_s"] = float(final_timeout_s)
            manifest["qualification"]["final_timeout_s"] = float(final_timeout_s)
        argv = manifest["workload"]["argv"]
        if mode == "golden":
            argv.extend(("--generate-golden", "true"))
            if profile.target == "gpu":
                argv.extend(("--golden-file", f"{manifest['spool_dir']}/gpu-golden.rgba"))
        elif mode == "calibration":
            if profile.target == "cpu" and golden.get("checksum"):
                argv.extend(("--golden-checksum", str(golden["checksum"])))
            if profile.target == "gpu" and golden.get("remote_path"):
                argv.extend(("--golden-file", str(golden["remote_path"])))
        manifest["manifest_sha256"] = document_sha256(
            {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        )
        return manifest

    @staticmethod
    def _validate_baseline(profile: ProfileConfig, baseline: Baseline) -> None:
        if baseline.status != "approved":
            raise RunError(f"baseline is not approved: {baseline.id} ({baseline.status})")
        for name, actual, expected in (
            ("profile", profile.name, baseline.profile),
            ("target", profile.target, baseline.target),
            ("platform", profile.platform, baseline.platform),
        ):
            if actual != expected:
                raise RunError(f"baseline {name} mismatch: profile={actual!r} baseline={expected!r}")
        expected_profile_hash = baseline.fingerprints.get("profile")
        if expected_profile_hash and expected_profile_hash != profile.fingerprint:
            raise RunError(
                f"profile fingerprint does not match approved baseline: {profile.fingerprint} != {expected_profile_hash}"
            )


@dataclass
class RunExecution:
    result: PolicyResult
    result_path: Path
    event_count: int
    launch_result: CommandResult | None = None


class RunOrchestrator:
    """Evaluate one serial attempt and produce the selected PC-side evidence."""

    def __init__(self, output_root: Path):
        self.output_root = output_root.expanduser().resolve()

    def evaluate_stream(
        self,
        manifest: Mapping[str, Any],
        chunks: Iterable[bytes],
        *,
        capabilities: Mapping[str, Any] | None = None,
        deployment: Mapping[str, Any] | None = None,
        save_raw: bool | None = None,
        save_events: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> RunExecution:
        run_id = str(manifest["run_id"])
        test_id = str(manifest.get("test_id", run_id))
        mode = str(manifest.get("pc_artifacts", "full"))
        if save_raw is None:
            save_raw = mode == "full"
        if save_events is None:
            save_events = mode == "full"
        store = ArtifactStore.create(self.output_root, run_id, test_id=test_id if "test_id" in manifest else None)
        store.write_json("run-manifest.json", dict(manifest))
        if capabilities:
            store.write_json("capabilities.json", dict(capabilities))
        if deployment:
            store.write_json("deployment-manifest.json", dict(deployment))
        if "profile" in manifest:
            store.write_json("effective-profile.json", dict(manifest["profile"]))
        serial_transport = manifest.get("serial_transport", {})
        uart_v2 = isinstance(serial_transport, Mapping) and serial_transport.get("protocol") == UART_PROTOCOL
        decoder = (
            UartV2Decoder(
                run_id,
                test_id,
                max_frame_bytes=int(serial_transport.get("max_frame_bytes", 512)),
            )
            if uart_v2
            else EventDecoder(run_id)
        )
        policy = PolicyEngine(self._limits(manifest))
        event_count = 0
        started = clock()
        last_workload_activity = started
        overall_timeout = float(manifest.get("overall_timeout_s", manifest.get("timeout_s", 300)))
        heartbeat_timeout = float(manifest.get("heartbeat_timeout_s", 45))
        final_timeout = float(manifest.get("final_timeout_s", overall_timeout))
        timed_out = False
        timeout_phase: str | None = None
        final_seen = False
        agent_started = False
        workload_completed = False
        workload_completed_at: float | None = None
        protocol_failed = False
        try:
            for chunk in chunks:
                now = clock()
                heartbeat_expired = (
                    agent_started
                    and not workload_completed
                    and not protocol_failed
                    and now - last_workload_activity > heartbeat_timeout
                )
                final_expired = (
                    workload_completed_at is not None
                    and not final_seen
                    and not protocol_failed
                    and now - workload_completed_at > final_timeout
                )
                overall_expired = now - started > overall_timeout
                if overall_expired or heartbeat_expired or final_expired:
                    timed_out = True
                    if final_expired or (overall_expired and workload_completed):
                        timeout_phase = "post-summary-final"
                        policy.infrastructure_failure(
                            "agent",
                            "AGENT_FINAL_TIMEOUT",
                            f"agent_final was not received within {final_timeout:g}s after workload summary",
                        )
                    elif heartbeat_expired:
                        timeout_phase = "pre-summary-heartbeat"
                    else:
                        timeout_phase = "overall"
                    break
                if not chunk:
                    continue
                if save_raw:
                    store.append_raw_serial(chunk)
                if protocol_failed:
                    continue
                try:
                    events = decoder.feed(chunk)
                except EventProtocolError as exc:
                    policy.protocol_failure(exc)
                    protocol_failed = True
                    continue
                for event in events:
                    if "test_id" in manifest and event.raw.get("test_id") != test_id:
                        policy.protocol_failure(
                            EventProtocolError(
                                "wrong_test_id",
                                f"event test_id {event.raw.get('test_id')!r} does not match {test_id!r}",
                            )
                        )
                        protocol_failed = True
                        break
                    event_count += 1
                    if save_events:
                        store.append_event(event)
                    policy.process(event)
                    if event.type == "agent_start":
                        agent_started = True
                        last_workload_activity = now
                    if event.source.endswith("-workload") and event.type in {
                        "start", "heartbeat", "batch", "verify", "golden", "summary", "error", "violation"
                    }:
                        agent_started = True
                        last_workload_activity = now
                    if event.type == "summary":
                        workload_completed = True
                        workload_completed_at = now
                        store.write_json("workload-summary.json", event.payload)
                    if event.type == "agent_final":
                        final_seen = True
                        break
                if final_seen or protocol_failed:
                    break
            if not timed_out and not final_seen and not protocol_failed:
                try:
                    decoder.finish()
                except EventProtocolError as exc:
                    policy.protocol_failure(exc)
            result = policy.finalize(timed_out=timed_out, require_agent_final=True)
            baseline_info = manifest.get("baseline") or {}
            result_document = {
                "schema_version": 1,
                "producer": {"name": "vmin_judge", "component": "RunOrchestrator", "version": "2.1.0"},
                "test_id": test_id,
                "attempt_id": str(manifest.get("attempt_id", run_id)),
                "run_id": run_id,
                "profile_id": manifest.get("profile", {}).get("id"),
                "baseline_id": baseline_info.get("id"),
                "validation_mode": manifest.get("validation_mode", "error-only"),
                "event_count": event_count,
                "device_evidence": manifest.get("device_attempt_dir"),
                "pc_artifacts": mode,
                "liveness": {
                    "timed_out": timed_out,
                    "timeout_phase": timeout_phase,
                    "heartbeat_timeout_s": heartbeat_timeout,
                    "final_timeout_s": final_timeout,
                    "overall_timeout_s": overall_timeout,
                    "agent_final_seen": final_seen,
                },
                **result.to_dict(),
            }
            result_path = store.finalize(result_document)
            return RunExecution(result=result, result_path=result_path, event_count=event_count)
        except BaseException as exc:
            store.close_incomplete(str(exc))
            raise

    def run_serial(
        self,
        manifest: Mapping[str, Any],
        *,
        transport: Transport,
        agent_argv: list[str],
        pc_serial: str,
        baudrate: int,
        capabilities: Mapping[str, Any] | None = None,
        deployment: Mapping[str, Any] | None = None,
    ) -> RunExecution:
        try:
            import serial
        except ImportError as exc:
            raise RunError("pyserial is required for a hardware run; install requirements.txt") from exc
        stop = threading.Event()
        timeout = float(manifest.get("overall_timeout_s", 300)) + 30.0
        serial_transport = manifest.get("serial_transport", {})
        max_frame_bytes = int(serial_transport.get("max_frame_bytes", 512)) if isinstance(serial_transport, Mapping) else 512
        tail_guard_bytes = int(serial_transport.get("tail_guard_bytes", 64)) if isinstance(serial_transport, Mapping) else 64
        drain_grace_s = max(2.0, 3.0 * frame_wire_seconds(max_frame_bytes + tail_guard_bytes + 2, baudrate))
        with serial.Serial(port=pc_serial, baudrate=baudrate, timeout=0.1) as stream:
            if hasattr(stream, "reset_input_buffer"):
                stream.reset_input_buffer()
            future: Future[CommandResult] = Future()

            def invoke_agent() -> None:
                try:
                    future.set_result(transport.invoke(agent_argv, timeout))
                except BaseException as exc:
                    future.set_exception(exc)

            agent_thread = threading.Thread(target=invoke_agent, name="device-agent", daemon=True)
            agent_thread.start()

            def serial_chunks() -> Iterator[bytes]:
                agent_finished_at: float | None = None
                while not stop.is_set():
                    chunk = bytes(stream.read(4096))
                    if future.done() and agent_finished_at is None:
                        agent_finished_at = time.monotonic()
                    if not chunk and agent_finished_at is not None:
                        if time.monotonic() - agent_finished_at >= drain_grace_s:
                            break
                    yield chunk

            transport_reason: dict[str, Any] | None = None
            launch_result: CommandResult | None = None
            try:
                execution = self.evaluate_stream(
                    manifest,
                    serial_chunks(),
                    capabilities=capabilities,
                    deployment=deployment,
                )
                if not execution.result.agent_final_seen and not future.done():
                    transport.cancel_active()
                    transport_reason = {
                        "scope": "transport",
                        "code": "AGENT_TRANSPORT_CANCELLED_AFTER_VERDICT",
                        "message": "device-agent transport was cancelled after UART evaluation completed",
                    }
                try:
                    launch_result = future.result(timeout=10.0 if execution.result.agent_final_seen else 0.25)
                except TimeoutError:
                    transport.cancel_active()
                    transport_reason = transport_reason or {
                        "scope": "transport",
                        "code": "AGENT_TRANSPORT_DID_NOT_FINISH",
                        "message": "device-agent transport did not finish after UART evaluation completed",
                    }
                except BaseException as exc:
                    transport_reason = {
                        "scope": "transport",
                        "code": "AGENT_TRANSPORT_EXCEPTION",
                        "message": str(exc),
                    }
            finally:
                stop.set()
                if not future.done():
                    transport.cancel_active()
            execution.launch_result = launch_result
            if transport_reason is not None:
                self._record_infrastructure_reason(execution, transport_reason)
            if launch_result is not None and execution.result.agent_final_seen:
                expected_exit = execution.result.workload_exit_code
                launch_invalid = (
                    not launch_result.transport_ok
                    or expected_exit is None
                    or launch_result.return_code != expected_exit
                )
            else:
                launch_invalid = False
            if launch_invalid and launch_result is not None:
                reason = {
                    "scope": "agent-process",
                    "code": "AGENT_EXIT_MISMATCH",
                    "expected_workload_exit": expected_exit,
                    "agent_process_exit": launch_result.return_code,
                    "timed_out": launch_result.timed_out,
                    "stdout": launch_result.stdout,
                    "stderr": launch_result.stderr,
                }
                self._record_infrastructure_reason(execution, reason)
            return execution

    @staticmethod
    def _record_infrastructure_reason(execution: RunExecution, reason: Mapping[str, Any]) -> None:
        normalized = dict(reason)
        if normalized not in execution.result.infrastructure_reasons:
            execution.result.infrastructure_reasons.append(normalized)
        execution.result.verdict = "INFRA_ERROR"
        execution.result.exit_code = 3
        document = json.loads(execution.result_path.read_text(encoding="utf-8"))
        document["verdict"] = "INFRA_ERROR"
        document["exit_code"] = 3
        reasons = document.setdefault("infrastructure_reasons", [])
        if normalized not in reasons:
            reasons.append(normalized)
        hashes = {
            path.relative_to(execution.result_path.parent).as_posix(): sha256_file(path)
            for path in sorted(execution.result_path.parent.rglob("*"))
            if path.is_file() and path.name not in {"artifact-hashes.json", "result.json"}
        }
        atomic_write_json(
            execution.result_path.parent / "artifact-hashes.json",
            {"schema_version": 1, "sha256": hashes},
        )
        document["artifacts"] = {
            "complete": True,
            "hash_manifest": "artifact-hashes.json",
            "hashed_file_count": len(hashes),
        }
        atomic_write_json(execution.result_path, document)

    @staticmethod
    def _limits(manifest: Mapping[str, Any]) -> PolicyLimits:
        policy = manifest.get("policy", {})
        thresholds = policy.get("thresholds", {}) if isinstance(policy, dict) else {}
        return PolicyLimits.from_mapping(
            {
                "performance": thresholds.get("performance", {}),
                "telemetry": thresholds.get("telemetry", {}),
                "required_telemetry": policy.get("required_telemetry", []),
            }
        )

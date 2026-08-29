"""Resolved run manifests and integrated serial execution/evaluation transactions."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Mapping

from .artifact_store import ArtifactStore, atomic_write_json, sha256_file
from .baselines import Baseline
from .config_loader import ProfileConfig, document_sha256, load_document
from .events import EventDecoder, EventProtocolError
from .path_resolver import PathResolver
from .policy_engine import PolicyEngine, PolicyLimits, PolicyResult
from .transport import CommandResult, Transport


class RunError(RuntimeError):
    pass


def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def parse_kernel_rules(path: Path) -> list[dict[str, str]]:
    """Convert legacy sectioned rules into explicit agent regex rules."""
    section = ""
    rules: list[dict[str, str]] = []
    counters = {"warn": 0, "fail": 0}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section not in {"warn", "fail"}:
            continue
        try:
            match_type, expression = line.split("|", 1)
        except ValueError as exc:
            raise RunError(f"invalid kernel rule line: {raw}") from exc
        match_type = match_type.lower()
        if match_type in {"contains", "icontains", "exact", "iexact"}:
            pattern = re.escape(expression)
            if match_type in {"exact", "iexact"}:
                pattern = f"^(?:{pattern})$"
        elif match_type in {"regex", "iregex"}:
            pattern = expression
        else:
            raise RunError(f"unsupported kernel rule type: {match_type}")
        counters[section] += 1
        rules.append(
            {
                "id": f"kernel-{section}-{counters[section]:03d}",
                "severity": "critical" if section == "fail" else "warning",
                "pattern": pattern,
            }
        )
    return rules


class RunManifestBuilder:
    """Resolve profile, approved baseline, capabilities, paths, and policies into one manifest."""

    def __init__(self, paths: PathResolver):
        self.paths = paths

    def build(
        self,
        *,
        profile: ProfileConfig,
        baseline: Baseline,
        capabilities: Mapping[str, Any],
        run_id: str | None = None,
        kernel_mode: str | None = None,
        overall_timeout_s: float = 300.0,
        heartbeat_timeout_s: float = 45.0,
        kernel_rules_path: Path | None = None,
        device_uart: str | None = None,
    ) -> dict[str, Any]:
        if not device_uart:
            raise RunError("device UART must be resolved from an explicit value, saved pairing, or platform config")
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
        resolved_run_id = run_id or new_run_id(profile.target)
        remote_run = self.paths.remote(PurePosixPath("runs") / resolved_run_id)
        remote_binary = self.paths.remote(str(profile.workload.get("remote_binary", f"bin/{profile.target}-avs-workload")))
        remote_config = self.paths.remote(f"configs/{profile.name}.json")
        workload_argv = [str(remote_binary), "--config", str(remote_config)]
        if profile.target == "cpu" and baseline.golden.get("checksum"):
            workload_argv.extend(("--golden-checksum", str(baseline.golden["checksum"])))
        if profile.target == "gpu" and baseline.golden.get("remote_path"):
            workload_argv.extend(("--golden-file", str(baseline.golden["remote_path"])))
        affinity = profile.environment.get("affinity")
        if affinity:
            workload_argv = ["taskset", "-c", str(affinity), *workload_argv]

        environment_actions = self._environment_actions(profile, capabilities)
        telemetry = self._telemetry(profile, capabilities)
        mode = kernel_mode or profile.kernel_monitor
        if mode not in {"off", "critical", "full-local"}:
            raise RunError(f"invalid kernel mode: {mode}")
        rules = parse_kernel_rules(kernel_rules_path) if kernel_rules_path is not None and mode != "off" else []
        thresholds = json.loads(json.dumps(baseline.thresholds))
        telemetry_thresholds = thresholds.setdefault("telemetry", {})
        temperature = profile.environment.get("temperature_c")
        if isinstance(temperature, dict):
            telemetry_thresholds[f"{profile.target}.temperature"] = {
                key: float(value) for key, value in temperature.items() if key in {"min", "max"}
            }
        frequency_actions = [action for action in environment_actions if action.get("policy_metric", "").startswith(f"{profile.target}.frequency")]
        tolerance = float(profile.environment.get("frequency_tolerance_percent", 2.0)) / 100.0
        for action in frequency_actions:
            value = float(action["policy_value"])
            telemetry_thresholds[action["policy_metric"]] = {
                "min": value * (1.0 - tolerance),
                "max": value * (1.0 + tolerance),
            }
        for action in environment_actions:
            if action.get("kind") == "online":
                telemetry_thresholds[action["policy_metric"]] = {"min": 1, "max": 1}

        manifest: dict[str, Any] = {
            "schema_version": 1,
            "producer": {"name": "vmin_judge", "component": "RunManifestBuilder", "version": "2.0.0"},
            "run_id": resolved_run_id,
            "profile": {"id": profile.name, "sha256": profile.fingerprint},
            "baseline": {"id": baseline.id, "sha256": baseline.sha256},
            "platform": profile.platform,
            "target": profile.target,
            "capabilities_sha256": document_sha256(dict(capabilities)),
            "uart": str(PurePosixPath(device_uart)),
            "spool_dir": str(remote_run / "spool"),
            "timeout_s": overall_timeout_s,
            "overall_timeout_s": overall_timeout_s,
            "heartbeat_timeout_s": heartbeat_timeout_s,
            "event_crc": False,
            "agent_backend": "posix-shell",
            "workload": {
                "argv": workload_argv,
                "cwd": str(self.paths.device_root),
                "config_path": str(remote_config),
            },
            "environment": {
                "actions": environment_actions,
                "requested": dict(profile.environment),
                "restoration_required": True,
            },
            "telemetry": telemetry,
            "kernel": {
                "mode": mode,
                "required": mode != "off",
                "raw_local": mode == "full-local",
                "rules": rules,
                "dedupe_window_ms": int(profile.kernel_options.get("dedupe_window_ms", 1000)),
                "max_events_per_second": int(profile.kernel_options.get("max_events_per_second", 10)),
            },
            "policy": {
                "thresholds": thresholds,
                "required_telemetry": list(profile.telemetry.get("required", [])),
            },
            "assets": [],
            "restoration_plan": [action["path"] for action in environment_actions],
        }
        manifest["manifest_sha256"] = document_sha256(manifest)
        return manifest

    def build_qualification(
        self,
        *,
        profile: ProfileConfig,
        golden: Mapping[str, Any],
        capabilities: Mapping[str, Any],
        mode: str,
        run_id: str | None = None,
        kernel_mode: str | None = None,
        overall_timeout_s: float = 300.0,
        heartbeat_timeout_s: float = 45.0,
        kernel_rules_path: Path | None = None,
        device_uart: str | None = None,
    ) -> dict[str, Any]:
        if mode not in {"golden", "calibration"}:
            raise RunError(f"unsupported qualification mode: {mode}")
        synthetic = Baseline.from_mapping(
            {
                "schema_version": 1,
                "id": f"qualification-{profile.name}",
                "profile": profile.name,
                "target": profile.target,
                "platform": profile.platform,
                "status": "approved",
                "fingerprints": {"profile": profile.fingerprint, "correctness": str(golden.get("correctness_fingerprint", "pending"))},
                "golden": dict(golden),
                "thresholds": {},
                "calibration": {"qualification_only": True},
                "approval": {"approver": "qualification-workflow", "approved_at": "runtime-only"},
            }
        )
        manifest = self.build(
            profile=profile,
            baseline=synthetic,
            capabilities=capabilities,
            run_id=run_id,
            kernel_mode=kernel_mode,
            overall_timeout_s=overall_timeout_s,
            heartbeat_timeout_s=heartbeat_timeout_s,
            kernel_rules_path=kernel_rules_path,
            device_uart=device_uart,
        )
        manifest["qualification"] = {"mode": mode, "production_baseline_allowed": False}
        manifest["baseline"] = None
        argv = manifest["workload"]["argv"]
        if mode == "golden":
            argv.extend(("--generate-golden", "true"))
            if profile.target == "gpu":
                argv.extend(("--golden-file", f"{manifest['spool_dir']}/gpu-golden.rgba"))
        manifest["manifest_sha256"] = document_sha256({key: value for key, value in manifest.items() if key != "manifest_sha256"})
        return manifest

    @staticmethod
    def _environment_actions(profile: ProfileConfig, capabilities: Mapping[str, Any]) -> list[dict[str, Any]]:
        records = capabilities.get("capabilities", {})
        actions: list[dict[str, Any]] = []
        governor = profile.environment.get("governor")
        if governor:
            for path in records.get(f"{profile.target}.governor", {}).get("paths", []):
                actions.append({"path": path, "value": str(governor), "required": True, "kind": "governor"})
        frequency_key = "frequency_khz" if profile.target == "cpu" else "frequency_hz"
        frequency = profile.environment.get(frequency_key)
        minimum_record = records.get(f"{profile.target}.minimum_frequency", {})
        maximum_record = records.get(f"{profile.target}.maximum_frequency", {})
        frequency_values: dict[str, int] = {}
        if frequency == "platform_max":
            for path, value in maximum_record.get("values", {}).items():
                if isinstance(value, (int, float)):
                    frequency_values[RunManifestBuilder._instance_suffix(path)] = int(value)
        elif isinstance(frequency, int):
            suffixes = {
                RunManifestBuilder._instance_suffix(path)
                for path in [*minimum_record.get("paths", []), *maximum_record.get("paths", [])]
            }
            frequency_values = {suffix: frequency for suffix in suffixes}
        for record, kind in ((maximum_record, "maximum_frequency"), (minimum_record, "minimum_frequency")):
            for path in record.get("paths", []):
                suffix = RunManifestBuilder._instance_suffix(path)
                if suffix in frequency_values:
                    metric = f"{profile.target}.frequency{('.' + suffix) if suffix else ''}"
                    actions.append(
                        {
                            "path": path,
                            "value": str(frequency_values[suffix]),
                            "required": True,
                            "kind": kind,
                            "policy_metric": metric,
                            "policy_value": frequency_values[suffix],
                        }
                    )
        requested_online = profile.environment.get("online_cores")
        if isinstance(requested_online, list):
            requested = {int(value) for value in requested_online}
            for path in records.get("cpu.online", {}).get("paths", []):
                match = re.search(r"/cpu(\d+)/online$", path)
                if match and int(match.group(1)) in requested:
                    actions.append(
                        {
                            "path": path,
                            "value": "1",
                            "required": True,
                            "kind": "online",
                            "policy_metric": f"cpu.online.{match.group(1)}",
                            "policy_value": 1,
                        }
                    )
        return actions

    @staticmethod
    def _instance_suffix(path: str) -> str:
        for pattern in (r"/cpu(\d+)/", r"gpu(?:freq)?(\d+)"):
            match = re.search(pattern, path)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _telemetry(profile: ProfileConfig, capabilities: Mapping[str, Any]) -> dict[str, Any]:
        records = capabilities.get("capabilities", {})
        requested = list(profile.telemetry.get("required", [])) + list(profile.telemetry.get("optional", []))
        samplers: list[dict[str, Any]] = []
        for metric in requested:
            record = records.get(metric, {})
            paths = record.get("paths", [])
            if not paths:
                continue
            unit = record.get("unit")
            if record.get("derivation") == "delta_busy_over_delta_total":
                parser = "proc_stat_utilization"
            elif metric.endswith("temperature"):
                if unit in {"millidegree_celsius", "millicelsius"}:
                    parser = "millidegree_celsius"
                elif unit in {"degree_celsius", "celsius"}:
                    parser = "degree_celsius"
                else:
                    parser = "temperature_auto"
            elif metric.endswith("online"):
                parser = "int"
            elif unit in {"Hz", "kHz", "count", "us", "percent"}:
                parser = "number"
            else:
                parser = "text"
            samplers.append(
                {
                    "metric": metric,
                    "paths": list(paths),
                    "parser": parser,
                    "parser_by_path": dict(record.get("parser_by_path", {})),
                    "unit": "celsius" if parser in {"millidegree_celsius", "degree_celsius", "temperature_auto"} else unit,
                    "required": metric in profile.telemetry.get("required", []),
                }
            )
        return {
            "interval_ms": int(profile.telemetry.get("interval_ms", 1000)),
            "source": f"{profile.target}-telemetry",
            "samplers": samplers,
        }


@dataclass
class RunExecution:
    result: PolicyResult
    result_path: Path
    event_count: int
    launch_result: CommandResult | None = None


class RunOrchestrator:
    """Evaluate a serial transaction and finalize one reproducible artifact directory."""

    def __init__(self, output_root: Path):
        self.output_root = output_root.expanduser().resolve()

    def evaluate_stream(
        self,
        manifest: Mapping[str, Any],
        chunks: Iterable[bytes],
        *,
        capabilities: Mapping[str, Any] | None = None,
        deployment: Mapping[str, Any] | None = None,
        save_raw: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> RunExecution:
        run_id = str(manifest["run_id"])
        store = ArtifactStore.create(self.output_root, run_id)
        store.write_json("run-manifest.json", dict(manifest))
        store.write_json("capabilities.json", dict(capabilities or {}))
        store.write_json("deployment-manifest.json", dict(deployment or {}))
        if "profile" in manifest:
            store.write_json("effective-profile.json", dict(manifest["profile"]))
        decoder = EventDecoder(run_id)
        policy = PolicyEngine(self._limits(manifest))
        event_count = 0
        started = clock()
        last_heartbeat = started
        overall_timeout = float(manifest.get("overall_timeout_s", manifest.get("timeout_s", 300)))
        heartbeat_timeout = float(manifest.get("heartbeat_timeout_s", 45))
        timed_out = False
        final_seen = False
        try:
            for chunk in chunks:
                now = clock()
                if now - started > overall_timeout or (event_count > 0 and now - last_heartbeat > heartbeat_timeout):
                    timed_out = True
                    break
                if not chunk:
                    continue
                if save_raw:
                    store.append_raw_serial(chunk)
                try:
                    events = decoder.feed(chunk)
                except EventProtocolError as exc:
                    policy.protocol_failure(exc)
                    break
                for event in events:
                    event_count += 1
                    store.append_event(event)
                    policy.process(event)
                    if event.type in {"agent_start", "start", "heartbeat", "batch", "telemetry"}:
                        last_heartbeat = now
                    if event.type == "summary":
                        store.write_json("workload-summary.json", event.payload)
                    if event.type == "agent_final":
                        final_seen = True
                        break
                if final_seen:
                    break
            if not timed_out and not final_seen:
                try:
                    decoder.finish()
                except EventProtocolError as exc:
                    policy.protocol_failure(exc)
            result = policy.finalize(timed_out=timed_out, require_agent_final=True)
            baseline_info = manifest.get("baseline") or {}
            result_document = {
                "schema_version": 1,
                "producer": {"name": "vmin_judge", "component": "RunOrchestrator", "version": "2.0.0"},
                "run_id": run_id,
                "profile_id": manifest.get("profile", {}).get("id"),
                "baseline_id": baseline_info.get("id"),
                "event_count": event_count,
                "liveness": {"timed_out": timed_out, "agent_final_seen": final_seen},
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

        def serial_chunks() -> Iterator[bytes]:
            with serial.Serial(port=pc_serial, baudrate=baudrate, timeout=0.1) as stream:
                while not stop.is_set():
                    yield bytes(stream.read(4096))

        timeout = float(manifest.get("overall_timeout_s", 300)) + 30.0
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="device-agent") as executor:
            future: Future[CommandResult] = executor.submit(transport.invoke, agent_argv, timeout)
            try:
                execution = self.evaluate_stream(
                    manifest,
                    serial_chunks(),
                    capabilities=capabilities,
                    deployment=deployment,
                    save_raw=True,
                )
            finally:
                stop.set()
            try:
                launch_result = future.result(timeout=10.0)
            except TimeoutError as exc:
                raise RunError("device-agent transport command did not finish") from exc
            execution.launch_result = launch_result
            expected_exit = execution.result.workload_exit_code
            launch_invalid = (
                not launch_result.transport_ok
                or expected_exit is None
                or launch_result.return_code != expected_exit
            )
            if launch_invalid:
                reason = {
                    "scope": "agent-process",
                    "code": "AGENT_EXIT_MISMATCH",
                    "expected_workload_exit": expected_exit,
                    "agent_process_exit": launch_result.return_code,
                    "timed_out": launch_result.timed_out,
                    "stderr": launch_result.stderr,
                }
                execution.result.verdict = "INFRA_ERROR"
                execution.result.exit_code = 3
                execution.result.infrastructure_reasons.append(reason)
                document = json.loads(execution.result_path.read_text(encoding="utf-8"))
                document["verdict"] = "INFRA_ERROR"
                document["exit_code"] = 3
                document.setdefault("infrastructure_reasons", []).append(reason)
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
            return execution

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

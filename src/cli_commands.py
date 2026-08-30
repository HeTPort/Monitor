"""Target v2 CLI handlers built on the refactored service contracts."""

from __future__ import annotations

import csv
import json
import re
import shutil
import time
from argparse import Namespace
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .artifact_store import ArtifactStore, atomic_write_json, sha256_file
from .baselines import Baseline, BaselineError, BaselineRegistry
from .config_loader import ConfigError, PlatformConfig, ProfileConfig, load_document
from .deployment import AssetSpec, DeploymentError, DeploymentManager
from .events import EventProtocolError
from .events import EventDecoder
from .path_resolver import PathResolutionError, PathResolver
from .platform_probe import PlatformProbe, ProbeError
from .policy_engine import RunExitCode
from .qualification import (
    CalibrationPolicy,
    CalibrationSample,
    CalibrationService,
    GoldenService,
    QualificationError,
    correctness_fingerprint,
)
from .run_orchestrator import RunError, RunManifestBuilder, RunOrchestrator, new_run_id
from .transport import ADBTransport, HDCTransport, Transport, TransportError, TransportManager
from .transport_probe import TransportProbeBackend


CLI_VERSION = "2.0.1"


class CommandError(RuntimeError):
    def __init__(self, message: str, exit_code: int = int(RunExitCode.INFRA_ERROR)):
        super().__init__(message)
        self.exit_code = exit_code


def make_paths(args: Namespace) -> PathResolver:
    return PathResolver.create(
        config_dir=getattr(args, "config_dir", None),
        state_dir=getattr(args, "state_dir", None),
        output_dir=getattr(args, "output_dir", None),
        device_root=getattr(args, "device_root", "/data/local/tmp/avs"),
        entrypoint=Path(__file__).parents[1] / "main.py",
    )


def print_command_result(args: Namespace, data: Mapping[str, Any]) -> None:
    if getattr(args, "json_output", False):
        print(json.dumps(dict(data), indent=2, sort_keys=True, ensure_ascii=False))
        return
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            print(f"{key}: {value}")


def _concise_reason(reason: Mapping[str, Any]) -> dict[str, Any]:
    """Keep CMD failure output actionable without duplicating complete artifacts."""
    concise: dict[str, Any] = {
        key: reason[key]
        for key in ("scope", "code", "message", "seq")
        if key in reason
    }
    evidence = reason.get("evidence")
    if isinstance(evidence, Mapping):
        for key in (
            "phase",
            "path",
            "requested",
            "actual",
            "required",
            "metric",
            "value",
            "min",
            "max",
            "exit_code",
            "error_code",
            "line",
        ):
            if key in evidence:
                concise[key] = evidence[key]
    return concise


def command_boundary(handler):
    """Convert typed service errors into stable CLI exit codes without tracebacks."""

    def wrapped(args: Namespace) -> int:
        try:
            return int(handler(args))
        except CommandError as exc:
            print_command_result(args, {"error": str(exc), "exit_code": exc.exit_code})
            return exc.exit_code
        except (ConfigError, PathResolutionError, BaselineError, RunError, ValueError) as exc:
            code = int(RunExitCode.INVALID_CONFIGURATION)
            print_command_result(args, {"error": str(exc), "exit_code": code})
            return code
        except ProbeError as exc:
            code = int(RunExitCode.UNSUPPORTED)
            print_command_result(args, {"error": str(exc), "exit_code": code})
            return code
        except (TransportError, DeploymentError, QualificationError, OSError) as exc:
            code = int(RunExitCode.INFRA_ERROR)
            print_command_result(args, {"error": str(exc), "exit_code": code})
            return code

    return wrapped


def _profile_path(paths: PathResolver, name: str) -> Path:
    candidate = name if Path(name).suffix.lower() in {".json", ".yaml", ".yml"} else f"config/profiles/{name}.yaml"
    return paths.resolve_input(candidate)


def load_profile(paths: PathResolver, name: str) -> ProfileConfig:
    return ProfileConfig.from_file(_profile_path(paths, name))


def load_platform(paths: PathResolver, name: str) -> PlatformConfig:
    candidate = name if Path(name).suffix.lower() in {".json", ".yaml", ".yml"} else f"config/platforms/{name}.yaml"
    return PlatformConfig.from_file(paths.resolve_input(candidate))


def _transport(args: Namespace, paths: PathResolver) -> Transport:
    requested = getattr(args, "transport", "auto")
    serial = getattr(args, "device", None)
    transports: list[Transport] = []
    if requested in {"auto", "hdc"}:
        try:
            hdc = paths.resolve_tool("hdc", getattr(args, "hdc_bin", None))
            transports.append(HDCTransport(hdc, serial=serial))
        except PathResolutionError:
            if requested == "hdc":
                raise
    if requested in {"auto", "adb"}:
        try:
            adb = paths.resolve_tool("adb", getattr(args, "adb_bin", None))
            transports.append(ADBTransport(adb, serial=serial))
        except PathResolutionError:
            if requested == "adb":
                raise
    if not transports:
        raise TransportError(f"no {requested} host tool is available")
    manager = TransportManager(transports)
    manager.connect()
    return manager.require_active()


def _probe(args: Namespace, paths: PathResolver, transport: Transport, profile: ProfileConfig | None = None) -> dict[str, Any]:
    platform_name = getattr(args, "platform", None) or (profile.platform if profile else None)
    if not platform_name:
        raise ConfigError("platform is required")
    platform = load_platform(paths, platform_name)
    identity = transport.connect()
    probe = PlatformProbe(platform, TransportProbeBackend(transport, identity))
    result = probe.probe(
        full=bool(getattr(args, "full", False) or profile is not None),
        domains=(profile.target,) if profile is not None else None,
    )
    result["platform_config"] = {
        "path": str(platform.source_path),
        "sha256": platform.fingerprint,
        "external_override": bool(
            paths.config_dir is not None
            and (platform.source_path == paths.config_dir or paths.config_dir in platform.source_path.parents)
        ),
    }
    required = list(getattr(args, "require", []) or [])
    if profile is not None:
        required.extend(profile.telemetry.get("required", []))
        probe.apply_required_scope(result, required, scope=f"profile:{profile.name}")
    else:
        probe.require(result, sorted(set(required)))
    tool_checks: dict[str, tuple[tuple[str, ...], bool]] = {
        "device.shell": (("sh", "-c", "exit 0"), True),
        "device.sha256sum": (("sha256sum", "--help"), True),
        "device.dmesg": (("dmesg", "--help"), bool(profile and profile.kernel_monitor != "off")),
        "device.taskset": (("taskset", "--help"), bool(profile and profile.environment.get("affinity"))),
    }
    for capability_name, (command, required_tool) in tool_checks.items():
        check = transport.invoke(command, timeout_s=5.0)
        if capability_name == "device.sha256sum" and not check.success:
            check = transport.invoke(("toybox", "sha256sum", "--help"), timeout_s=5.0)
        result["capabilities"][capability_name] = {
            "available": check.success,
            "required": required_tool,
            "version": (check.stdout or check.stderr).strip().splitlines()[0] if (check.stdout or check.stderr).strip() else None,
            "provenance": "runtime-command",
        }
        if required_tool and not check.success:
            result["required_missing"] = sorted(set(result["required_missing"] + [capability_name]))
            result["supported"] = False
    return result


@command_boundary
def cmd_probe(args: Namespace) -> int:
    paths = make_paths(args)
    transport = _transport(args, paths)
    result = _probe(args, paths, transport)
    serial = result["device"].get("serial", "device")
    output = paths.resolve_output(f"probes/{serial}/capabilities.json", create_parent=True)
    atomic_write_json(output, result)
    print_command_result(args, {"supported": result["supported"], "capabilities": str(output), "required_missing": result["required_missing"]})
    return 0 if result["supported"] else int(RunExitCode.UNSUPPORTED)


def _resolve_baseline(args: Namespace, paths: PathResolver, profile: ProfileConfig) -> Baseline:
    registry = BaselineRegistry(paths.state_root)
    baseline_value = getattr(args, "baseline", None) or profile.baseline or "auto"
    if baseline_value == "auto":
        return registry.resolve(profile.name, {"profile": profile.fingerprint})
    baseline_path = Path(str(baseline_value))
    if baseline_path.suffix.lower() == ".json" and baseline_path.exists():
        return Baseline.from_mapping(json.loads(baseline_path.read_text(encoding="utf-8")))
    baseline = registry.get(str(baseline_value))
    if baseline.status != "approved":
        raise BaselineError(f"baseline is not approved: {baseline.id} ({baseline.status})")
    if not registry.verify_immutable(baseline.id):
        raise BaselineError(f"approved baseline hash verification failed: {baseline.id}")
    return baseline


def _asset_plan(
    paths: PathResolver,
    profile: ProfileConfig,
    baseline: Baseline | None,
) -> tuple[list[AssetSpec], Path, Path]:
    agent = paths.resolve_resource("device/avs_device_agent.sh")
    workload = paths.resolve_input(str(profile.workload["binary"]), owner=profile.source_path)
    workload_config_value = profile.workload.get("config")
    if not isinstance(workload_config_value, str):
        raise ConfigError(f"profile {profile.name} has no workload.config")
    workload_config = paths.resolve_input(workload_config_value, owner=profile.source_path)
    agent_remote = paths.remote("bin/avs-device-agent")
    assets = [
        AssetSpec(agent, agent_remote, executable=True, kind="agent"),
        AssetSpec(workload, paths.remote(str(profile.workload.get("remote_binary", f"bin/{profile.target}-avs-workload"))), executable=True, kind="workload"),
        AssetSpec(workload_config, paths.remote(f"configs/{profile.name}.json"), kind="workload-config"),
    ]
    declared_assets = profile.workload.get("assets", [])
    if not isinstance(declared_assets, list):
        raise ConfigError(f"profile {profile.name} workload.assets must be a list")
    for index, declared in enumerate(declared_assets):
        if not isinstance(declared, dict):
            raise ConfigError(f"profile {profile.name} workload.assets[{index}] must be a mapping")
        local_value = declared.get("local")
        remote_value = declared.get("remote")
        if not isinstance(local_value, str) or not isinstance(remote_value, str):
            raise ConfigError(f"profile {profile.name} workload.assets[{index}] requires local and remote strings")
        required = bool(declared.get("required", True))
        assets.append(
            AssetSpec(
                paths.resolve_input(local_value, owner=profile.source_path, required=required),
                paths.remote(remote_value),
                executable=bool(declared.get("executable", False)),
                required=required,
                kind=str(declared.get("kind", "workload-asset")),
            )
        )
    if baseline is not None and profile.target == "gpu":
        local_golden = baseline.golden.get("local_path")
        if local_golden:
            golden_path = paths.resolve_input(str(local_golden))
            try:
                workload_document = json.loads(workload_config.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigError(f"cannot read GPU workload config {workload_config}: {exc}") from exc
            configured_golden = workload_document.get("golden_file")
            if not isinstance(configured_golden, str) or not configured_golden:
                raise ConfigError(f"GPU workload config has no golden_file: {workload_config}")
            remote_golden = PurePosixPath(configured_golden)
            if not remote_golden.is_absolute() or not remote_golden.is_relative_to(paths.device_root):
                raise ConfigError(
                    f"GPU golden_file must be an absolute path below {paths.device_root}: {configured_golden}"
                )
            assets.append(AssetSpec(golden_path, remote_golden, kind="golden"))
    return assets, agent_remote, workload_config


def _shell_agent_argv(agent_remote: PurePosixPath, manifest: Mapping[str, Any], baudrate: int) -> list[str]:
    """Translate a resolved manifest into fixed POSIX-shell agent arguments."""

    def field(value: Any, name: str) -> str:
        text = str(value)
        if not text or any(character in text for character in "|\r\n\x00"):
            raise ConfigError(f"shell agent {name} contains an unsupported delimiter")
        return text

    def path_suffix(path: str, fallback: int) -> str:
        for pattern in (r"cpu(\d+)", r"thermal_zone(\d+)", r"state(\d+)", r"policy(\d+)"):
            match = re.search(pattern, path)
            if match:
                return match.group(1)
        return str(fallback)

    telemetry = manifest.get("telemetry", {})
    interval_ms = int(telemetry.get("interval_ms", 5000))
    argv = [
        "sh",
        str(agent_remote),
        "--run-id",
        field(manifest["run_id"], "run_id"),
        "--target",
        field(manifest["target"], "target"),
        "--uart",
        field(manifest["uart"], "uart"),
        "--spool-dir",
        field(manifest["spool_dir"], "spool_dir"),
        "--cwd",
        field(manifest["workload"].get("cwd", "/"), "workload.cwd"),
        "--baudrate",
        str(baudrate),
        "--timeout",
        str(max(1, int(float(manifest.get("timeout_s", 300))))),
        "--telemetry-interval",
        str(max(5, (interval_ms + 999) // 1000)),
        "--kernel-mode",
        field(manifest.get("kernel", {}).get("mode", "off"), "kernel.mode"),
    ]
    for action in manifest.get("environment", {}).get("actions", []):
        spec = "|".join(
            (
                field(action["path"], "environment.path"),
                field(action["value"], "environment.value"),
                "1" if action.get("required", False) else "0",
            )
        )
        argv.extend(("--environment", spec))
    for sampler in telemetry.get("samplers", []):
        paths = list(sampler.get("paths", []))
        parser_by_path = dict(sampler.get("parser_by_path", {}))
        for index, path in enumerate(paths):
            metric = str(sampler["metric"])
            if len(paths) > 1 and sampler.get("parser") != "proc_stat_utilization":
                metric = f"{metric}.{path_suffix(str(path), index)}"
            parser = parser_by_path.get(str(path), sampler.get("parser", "text"))
            spec = "|".join(
                (
                    field(metric, "telemetry.metric"),
                    field(parser, "telemetry.parser"),
                    field(path, "telemetry.path"),
                )
            )
            argv.extend(("--telemetry", spec))
    for rule in manifest.get("kernel", {}).get("rules", []):
        severity = field(rule.get("severity", "warning"), "kernel.severity")
        rule_id = field(rule.get("id", "kernel-rule"), "kernel.id")
        pattern = str(rule.get("pattern", ""))
        if not pattern or any(character in pattern for character in "\t\r\n\x00"):
            raise ConfigError("shell agent kernel pattern contains an unsupported delimiter")
        argv.extend(("--kernel-rule", severity, rule_id, pattern))
    workload_argv = manifest.get("workload", {}).get("argv", [])
    if not isinstance(workload_argv, list) or not workload_argv:
        raise ConfigError("shell agent requires a non-empty workload argv list")
    argv.append("--")
    argv.extend(field(value, "workload.argv") for value in workload_argv)
    return argv


def _workload_fingerprint_fields(paths: PathResolver, profile: ProfileConfig) -> dict[str, Any]:
    assets, _, workload_config = _asset_plan(paths, profile, None)
    files = {
        str(asset.remote): sha256_file(asset.local)
        for asset in assets
        if asset.kind != "agent" and asset.local.exists()
    }
    return {
        "profile_sha256": profile.fingerprint,
        "target": profile.target,
        "workload": profile.workload,
        "workload_config_sha256": sha256_file(workload_config),
        "deployed_file_sha256": files,
    }


def _require_current_correctness(paths: PathResolver, profile: ProfileConfig, golden: Mapping[str, Any]) -> None:
    expected = golden.get("correctness_fingerprint")
    if not isinstance(expected, str) or not expected:
        raise ConfigError("golden manifest has no correctness_fingerprint")
    actual = correctness_fingerprint(_workload_fingerprint_fields(paths, profile))
    if actual != expected:
        raise ConfigError(
            f"current workload/config/shader fingerprint does not match golden: {actual} != {expected}"
        )


def _verify_existing_assets(transport: Transport, assets: Iterable[AssetSpec]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for asset in assets:
        local = asset.local.expanduser().resolve(strict=asset.required)
        if not local.exists():
            continue
        local_hash = sha256_file(local)
        remote_hash = transport.sha256(asset.remote)
        if local_hash != remote_hash:
            raise DeploymentError(
                f"--no-deploy asset mismatch: {asset.remote}; local={local_hash} remote={remote_hash}"
            )
        records.append(
            {
                "kind": asset.kind,
                "local": str(local),
                "remote": str(asset.remote),
                "required": asset.required,
                "executable": asset.executable,
                "action": "verified-existing",
                "verified": True,
                "size": local.stat().st_size,
                "local_sha256": local_hash,
                "remote_sha256": remote_hash,
            }
        )
    return {
        "schema_version": 1,
        "transport": transport.name,
        "assets": records,
        "complete": True,
        "verified": True,
        "no_deploy": True,
    }


def _cleanup_device_spool_after_pass(
    paths: PathResolver,
    transport: Transport,
    *,
    run_id: str,
    spool_dir: str,
    keep: bool,
) -> tuple[str, str | None]:
    if keep:
        return "retained", None
    remote_spool = PurePosixPath(spool_dir)
    expected_spool = paths.remote(PurePosixPath("runs") / run_id / "spool")
    if remote_spool != expected_spool:
        return "retained", f"refusing to remove unexpected device spool path: {remote_spool}"
    removal = transport.invoke(("rm", "-rf", str(remote_spool)), timeout_s=30.0)
    if not removal.success:
        return "retained", removal.stderr or removal.stdout or "device spool cleanup failed"
    return "removed-after-pass", None


def _apply_saved_pairing(args: Namespace, paths: PathResolver) -> None:
    pairing_path = paths.resolve_state("pairing.conf")
    if not pairing_path.exists():
        return
    pairing = json.loads(pairing_path.read_text(encoding="utf-8"))
    if not args.pc_serial:
        args.pc_serial = pairing.get("pc_port")
    if not args.device_uart and pairing.get("device_port"):
        args.device_uart = pairing["device_port"]
    if args.baudrate is None and pairing.get("baudrate"):
        args.baudrate = int(pairing["baudrate"])


def _apply_platform_serial(args: Namespace, paths: PathResolver, profile: ProfileConfig) -> None:
    """Fill unresolved serial settings from the selected device platform."""
    platform = load_platform(paths, profile.platform)
    if args.baudrate is None:
        args.baudrate = int(platform.serial.get("baudrate", 9600))
    if not args.device_uart:
        candidates = list(platform.serial.get("uart_candidates", []))
        if not candidates:
            raise ConfigError(
                "--device-uart is required when neither saved pairing nor platform UART candidates are available"
            )
        args.device_uart = candidates[0]


def _execute_live_qualification(
    args: Namespace,
    paths: PathResolver,
    profile: ProfileConfig,
    *,
    mode: str,
    count: int,
    golden: dict[str, Any] | None = None,
) -> list[Path]:
    if count <= 0:
        return []
    _apply_saved_pairing(args, paths)
    _apply_platform_serial(args, paths, profile)
    if not args.pc_serial:
        raise ConfigError("--pc-serial is required for live qualification when no saved pairing exists")
    transport = _transport(args, paths)
    capabilities = _probe(args, paths, transport, profile)
    if not capabilities["supported"]:
        raise ProbeError(f"required capabilities unavailable: {capabilities['required_missing']}")
    effective_golden = dict(golden or {})
    assets, agent_remote, _ = _asset_plan(paths, profile, None)
    if mode == "calibration" and profile.target == "gpu":
        local_value = effective_golden.get("local_path")
        if not local_value:
            raise ConfigError("GPU calibration golden manifest must resolve a local_path")
        local_golden = Path(str(local_value)).expanduser().resolve(strict=True)
        remote_golden = paths.remote(f"golden/gpu/{profile.name}.rgba")
        assets.append(AssetSpec(local_golden, remote_golden, kind="golden"))
        effective_golden["remote_path"] = str(remote_golden)
    deployment = DeploymentManager(transport).deploy(assets, verify_hashes=True)
    rules_path = paths.resolve_input("config/kernel/critical.conf")
    run_dirs: list[Path] = []
    for _ in range(count):
        run_id = new_run_id(f"{mode}-{profile.target}")
        manifest = RunManifestBuilder(paths).build_qualification(
            profile=profile,
            golden=effective_golden,
            capabilities=capabilities,
            mode=mode,
            run_id=run_id,
            kernel_mode="critical",
            overall_timeout_s=float(getattr(args, "overall_timeout", 300.0)),
            heartbeat_timeout_s=float(getattr(args, "heartbeat_timeout", 45.0)),
            kernel_rules_path=rules_path,
            device_uart=args.device_uart,
        )
        manifest["assets"] = [
            {"path": record["remote"], "sha256": record["remote_sha256"]}
            for record in deployment["assets"]
            if record.get("remote_sha256")
        ]
        execution = RunOrchestrator(paths.output_root).run_serial(
            manifest,
            transport=transport,
            agent_argv=_shell_agent_argv(agent_remote, manifest, args.baudrate),
            pc_serial=args.pc_serial,
            baudrate=args.baudrate,
            capabilities=capabilities,
            deployment=deployment,
        )
        if execution.result.verdict != "PASS":
            raise QualificationError(
                f"live {mode} run {run_id} did not pass: {execution.result.verdict}; {execution.result_path}"
            )
        run_dir = execution.result_path.parent
        if mode == "golden" and profile.target == "gpu":
            remote_readback = PurePosixPath(manifest["spool_dir"]) / "gpu-golden.rgba"
            transfer = transport.pull(remote_readback, run_dir / "gpu-golden.rgba")
            if not transfer.success:
                raise QualificationError(f"failed to collect GPU readback for {run_id}: {transfer.message}")
        run_dirs.append(run_dir)
    return run_dirs


@command_boundary
def cmd_deploy(args: Namespace) -> int:
    paths = make_paths(args)
    transport = _transport(args, paths)
    assets: list[AssetSpec] = [
        AssetSpec(paths.resolve_resource("device/avs_device_agent.sh"), paths.remote("bin/avs-device-agent"), executable=True, kind="agent")
    ]
    requested_profiles: list[ProfileConfig] = []
    if getattr(args, "profile", None):
        requested_profiles.append(load_profile(paths, args.profile))
    else:
        default_profiles = {
            "cpu": "cpu_mixed_big4",
            "gpu": "gpu_vulkan_mixed",
        }
        targets = ("cpu", "gpu") if args.target == "all" else (args.target,)
        requested_profiles.extend(load_profile(paths, default_profiles[target]) for target in targets)
    planned: dict[str, AssetSpec] = {str(assets[0].remote): assets[0]}
    for profile in requested_profiles:
        if args.target != "all" and profile.target != args.target:
            raise ConfigError(f"profile target {profile.target} does not match --target {args.target}")
        baseline = _resolve_baseline(args, paths, profile) if getattr(args, "baseline", None) else None
        profile_assets, _, _ = _asset_plan(paths, profile, baseline)
        for asset in profile_assets:
            planned[str(asset.remote)] = asset
    assets = list(planned.values())
    output = paths.resolve_output("deployment-manifest.json", create_parent=True)
    previous_manifest = None
    if args.clean_stale and output.exists():
        try:
            previous_manifest = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeploymentError(f"cannot read previous deployment manifest {output}: {exc}") from exc
    manifest = DeploymentManager(transport).deploy(
        assets,
        force=bool(args.force),
        verify_hashes=bool(args.verify_hashes),
        manifest_path=output,
        clean_stale=bool(args.clean_stale),
        previous_manifest=previous_manifest,
        allowed_remote_root=paths.device_root,
    )
    print_command_result(args, {"complete": manifest["complete"], "verified": manifest["verified"], "manifest": str(output)})
    return 0


def _events_from_run(run_dir: Path, event_type: str) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        raise QualificationError(f"events artifact missing: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise QualificationError(f"invalid events.jsonl line {line_number}: {exc}") from exc
        payload = event.get("payload", {})
        if event.get("type") == event_type and isinstance(payload, dict):
            records.append(payload)
    return records


def _qualified_run_specs(values: Iterable[str], default_board_id: str) -> list[tuple[str, Path]]:
    specs: list[tuple[str, Path]] = []
    for value in values:
        board_id = default_board_id
        path_value = value
        if "=" in value:
            candidate_board, candidate_path = value.split("=", 1)
            if candidate_board and candidate_path:
                board_id, path_value = candidate_board, candidate_path
        specs.append((board_id, Path(path_value).expanduser().resolve(strict=True)))
    return specs


@command_boundary
def cmd_golden(args: Namespace) -> int:
    if not args.known_good:
        raise CommandError("--known-good acknowledgement is required", int(RunExitCode.INVALID_CONFIGURATION))
    paths = make_paths(args)
    profile = load_profile(paths, args.profile)
    if profile.target != args.golden_target:
        raise ConfigError(f"profile target is {profile.target}, command target is {args.golden_target}")
    run_specs = _qualified_run_specs(args.run_dir, args.board_id)
    if len(run_specs) < args.runs:
        live = _execute_live_qualification(
            args, paths, profile, mode="golden", count=args.runs - len(run_specs), golden=None
        )
        run_specs.extend((args.board_id, run_dir) for run_dir in live)
    records: list[dict[str, Any]] = []
    for _, run_dir in run_specs[: args.runs]:
        found = _events_from_run(run_dir, "golden")
        if len(found) != 1:
            raise QualificationError(f"expected one golden event in {run_dir}, found {len(found)}")
        records.append(found[0])
    service = GoldenService(paths.resolve_output("qualification"))
    qualification_id = args.qualification_id or new_run_id(f"golden-{profile.target}")
    fields = _workload_fingerprint_fields(paths, profile)
    board_ids = [board_id for board_id, _ in run_specs[: args.runs]]
    if profile.target == "cpu":
        if args.accept_checksum:
            observed = {str(record.get("checksum", "")).lower() for record in records}
            if observed != {args.accept_checksum.lower().removeprefix("0x")}:
                raise QualificationError(
                    f"observed CPU golden {sorted(observed)} does not match --accept-checksum {args.accept_checksum}"
                )
        manifest = service.create_cpu(
            qualification_id=qualification_id,
            profile=profile.name,
            fingerprint_fields=fields,
            golden_records=records,
            board_ids=board_ids,
        )
    else:
        readbacks = [run_dir / args.readback_name for _, run_dir in run_specs[: args.runs]]
        manifest = service.create_gpu(
            qualification_id=qualification_id,
            profile=profile.name,
            fingerprint_fields=fields,
            golden_records=records,
            readback_files=readbacks,
            board_ids=board_ids,
        )
    print_command_result(args, {"qualification_id": qualification_id, "golden_manifest": manifest["manifest_path"], "sha256": manifest["manifest_sha256"]})
    return 0


def _sample_from_run(run_dir: Path, board_id: str) -> CalibrationSample:
    result_path = run_dir / "result.json"
    summary_path = run_dir / "workload-summary.json"
    if not result_path.exists() or not summary_path.exists():
        raise QualificationError(f"run is missing result/summary: {run_dir}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    temperatures: list[float] = []
    telemetry_path = run_dir / "telemetry.jsonl"
    telemetry_complete = telemetry_path.exists()
    throttled = False
    if telemetry_path.exists():
        for line in telemetry_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            metrics = event.get("payload", {}).get("metrics", {})
            for key, value in metrics.items():
                if key.endswith("temperature") or ".temperature." in key:
                    if isinstance(value, (int, float)):
                        temperatures.append(float(value))
                if "throttle" in key and value not in {0, 0.0, "0", "none", "off", False}:
                    throttled = True
    return CalibrationSample(
        run_id=str(result.get("run_id", run_dir.name)),
        board_id=board_id,
        summary=summary,
        temperature_c=max(temperatures) if temperatures else None,
        environment_compliant=not bool(result.get("environment_violations")),
        telemetry_complete=telemetry_complete,
        throttled=throttled,
    )


@command_boundary
def cmd_calibrate(args: Namespace) -> int:
    paths = make_paths(args)
    profile = load_profile(paths, args.profile)
    if profile.target != args.calibration_target:
        raise ConfigError(f"profile target is {profile.target}, command target is {args.calibration_target}")
    run_specs = _qualified_run_specs(args.run_dir, args.board_id)
    policy_data = load_document(paths.resolve_input(args.policy))
    policy = CalibrationPolicy.from_mapping(policy_data)
    if args.min_accepted is not None:
        policy = replace(policy, minimum_accepted_samples=args.min_accepted)
    golden_path = paths.resolve_input(args.golden)
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    if golden.get("profile") != profile.name:
        raise QualificationError(f"golden profile {golden.get('profile')!r} does not match {profile.name!r}")
    _require_current_correctness(paths, profile, golden)
    if profile.target == "gpu" and golden.get("readback_file"):
        golden["local_path"] = str((golden_path.parent / str(golden["readback_file"])).resolve(strict=True))
    if len(run_specs) < args.runs:
        live = _execute_live_qualification(
            args,
            paths,
            profile,
            mode="calibration",
            count=args.runs - len(run_specs),
            golden=golden,
        )
        run_specs.extend((args.board_id, run_dir) for run_dir in live)
    samples = [_sample_from_run(run_dir, board_id) for board_id, run_dir in run_specs[: args.runs]]
    temp_range = tuple(float(item) for item in args.temperature_range.split(":"))
    if len(temp_range) != 2:
        raise ConfigError("--temperature-range must be MIN:MAX")
    baseline_id = args.baseline_id or f"{profile.platform}-{profile.name}-v1"
    proposal = CalibrationService().calibrate(
        profile=profile.name,
        target=profile.target,
        platform=profile.platform,
        fingerprints={
            "profile": profile.fingerprint,
            "correctness": str(golden.get("correctness_fingerprint", "")),
        },
        golden=golden,
        samples=samples,
        policy=policy,
        temperature_range=(temp_range[0], temp_range[1]),
        baseline_id=baseline_id,
    )
    draft = BaselineRegistry(paths.state_root).create_draft(proposal, baseline_id=baseline_id)
    output = paths.resolve_output(f"qualification/{baseline_id}/proposed-baseline.json", create_parent=True)
    atomic_write_json(output, proposal)
    print_command_result(args, {"baseline_id": draft.id, "status": draft.status, "proposal": str(output)})
    return 0


@command_boundary
def cmd_baseline(args: Namespace) -> int:
    paths = make_paths(args)
    registry = BaselineRegistry(paths.state_root)
    action = args.baseline_action
    if action == "list":
        records = registry.list(status=args.status, profile=args.profile)
        print_command_result(args, {"count": len(records), "baselines": records})
    elif action == "show":
        print_command_result(args, registry.get(args.baseline_id).raw | {"registry_status": registry.get(args.baseline_id).status})
    elif action == "approve":
        baseline = registry.approve(args.baseline_id, args.approver)
        print_command_result(args, {"id": baseline.id, "status": baseline.status, "sha256": baseline.sha256})
    elif action == "deprecate":
        baseline = registry.deprecate(args.baseline_id, args.reason)
        print_command_result(args, {"id": baseline.id, "status": baseline.status})
    elif action == "export":
        destination = Path(args.output).expanduser() if args.output else paths.resolve_output(f"baselines/{args.baseline_id}.zip")
        bundle = registry.export_bundle(args.baseline_id, destination)
        print_command_result(args, {"id": args.baseline_id, "bundle": str(bundle), "sha256": sha256_file(bundle)})
    elif action == "import":
        baseline = registry.import_bundle(Path(args.bundle))
        print_command_result(args, {"id": baseline.id, "status": baseline.status})
    else:
        raise ConfigError(f"unsupported baseline action: {action}")
    return 0


@command_boundary
def cmd_run(args: Namespace) -> int:
    if getattr(args, "no_launch", False):
        raise ConfigError("execute --no-launch is not part of the run alias; use the diagnostic monitor command")
    if getattr(args, "auto_pair", False):
        raise ConfigError("execute --auto-pair is not implicit in v2; run pair first, then run/execute")
    if args.repeat < 1:
        raise ConfigError("--repeat must be at least 1")
    paths = make_paths(args)
    paths.ensure_writable_roots()
    _apply_saved_pairing(args, paths)
    if not args.pc_serial:
        raise ConfigError("--pc-serial is required when no saved pairing exists")
    profile = load_profile(paths, args.profile)
    _apply_platform_serial(args, paths, profile)
    baseline = _resolve_baseline(args, paths, profile)
    _require_current_correctness(paths, profile, baseline.golden)
    transport = _transport(args, paths)
    capabilities = _probe(args, paths, transport, profile)
    if not capabilities["supported"]:
        raise ProbeError(f"required capabilities unavailable: {capabilities['required_missing']}")
    assets, agent_remote, _ = _asset_plan(paths, profile, baseline)
    deployment_path = paths.resolve_output("latest-deployment-manifest.json", create_parent=True)
    if args.no_deploy:
        deployment = _verify_existing_assets(transport, assets)
        atomic_write_json(deployment_path, deployment)
    else:
        deployment = DeploymentManager(transport).deploy(
            assets,
            force=False,
            verify_hashes=True,
            manifest_path=deployment_path,
        )
    rules_path = paths.resolve_input("config/kernel/critical.conf")
    results: list[dict[str, Any]] = []
    final_exit = 0
    for repetition in range(args.repeat):
        run_id = (
            f"{args.run_id}-{repetition + 1:03d}"
            if args.run_id and args.repeat > 1
            else (args.run_id or new_run_id(profile.target))
        )
        manifest = RunManifestBuilder(paths).build(
            profile=profile,
            baseline=baseline,
            capabilities=capabilities,
            run_id=run_id,
            kernel_mode=args.kernel_monitor,
            overall_timeout_s=args.overall_timeout,
            heartbeat_timeout_s=args.heartbeat_timeout,
            kernel_rules_path=rules_path,
            device_uart=args.device_uart,
        )
        manifest["assets"] = [
            {"path": record["remote"], "sha256": record["remote_sha256"]}
            for record in deployment["assets"]
            if record.get("remote_sha256")
        ]
        agent_argv = _shell_agent_argv(agent_remote, manifest, args.baudrate)
        execution = RunOrchestrator(paths.output_root).run_serial(
            manifest,
            transport=transport,
            agent_argv=agent_argv,
            pc_serial=args.pc_serial,
            baudrate=args.baudrate,
            capabilities=capabilities,
            deployment=deployment,
        )
        final_exit = max(final_exit, execution.result.exit_code)
        spool_status = "retained"
        spool_error = None
        if execution.result.verdict == "PASS":
            spool_status, spool_error = _cleanup_device_spool_after_pass(
                paths,
                transport,
                run_id=run_id,
                spool_dir=str(manifest["spool_dir"]),
                keep=bool(getattr(args, "keep_device_spool", False)),
            )
        run_result: dict[str, Any] = {
            "run_id": run_id,
            "verdict": execution.result.verdict,
            "exit_code": execution.result.exit_code,
            "result": str(execution.result_path),
        }
        if execution.result.verdict != "PASS":
            run_result["errors"] = [
                _concise_reason(reason)
                for reason in [
                    *execution.result.infrastructure_reasons,
                    *execution.result.dut_reasons,
                ]
            ]
            run_result["device_spool"] = spool_status
        if spool_error:
            run_result["spool_cleanup_error"] = spool_error
        results.append(run_result)
    print_command_result(args, {"repeat": args.repeat, "exit_code": final_exit, "runs": results})
    return final_exit


@command_boundary
def cmd_simulate(args: Namespace) -> int:
    paths = make_paths(args)
    source_value = args.events or args.raw_serial
    if not source_value:
        raise ConfigError("--events or --raw-serial is required")
    source = paths.resolve_input(source_value)
    data = source.read_bytes()
    first_line = next((line for line in data.splitlines() if line.strip()), None)
    if first_line is None:
        raise ConfigError("simulation input is empty")
    try:
        run_id = str(json.loads(first_line)["run_id"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ConfigError("simulation input does not begin with a framed event") from exc
    policy: dict[str, Any] = {"thresholds": {}, "required_telemetry": []}
    if args.profile:
        profile = load_profile(paths, args.profile)
        policy["required_telemetry"] = list(profile.telemetry.get("required", []))
        if args.baseline:
            policy["thresholds"] = _resolve_baseline(args, paths, profile).thresholds
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "profile": {"id": args.profile},
        "baseline": {"id": args.baseline},
        "policy": policy,
        "overall_timeout_s": 86400,
        "heartbeat_timeout_s": 86400,
    }
    chunks: Iterable[bytes] = [data]
    if args.realtime:
        def replay() -> Iterable[bytes]:
            previous_timestamp: int | None = None
            for line in data.splitlines(keepends=True):
                if not line.strip():
                    continue
                try:
                    timestamp = int(json.loads(line)["timestamp_ms"])
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    raise ConfigError(f"cannot replay timing from malformed event: {exc}") from exc
                if previous_timestamp is not None and timestamp > previous_timestamp:
                    time.sleep((timestamp - previous_timestamp) / 1000.0)
                previous_timestamp = timestamp
                yield line
        chunks = replay()
    execution = RunOrchestrator(paths.output_root).evaluate_stream(manifest, chunks, save_raw=bool(args.raw_serial))
    print_command_result(
        args,
        {"run_id": run_id, "verdict": execution.result.verdict, "exit_code": execution.result.exit_code, "result": str(execution.result_path)},
    )
    return execution.result.exit_code


@command_boundary
def cmd_monitor_events(args: Namespace) -> int:
    if args.schema_version != 1:
        raise ConfigError(f"unsupported event schema version: {args.schema_version}")
    if not args.pc_serial:
        raise ConfigError("--pc-serial is required for monitor")
    try:
        import serial
    except ImportError as exc:
        raise CommandError("pyserial is required for monitor; install requirements.txt") from exc
    paths = make_paths(args)
    expected_run = args.expected_run_id
    decoder: EventDecoder | None = None
    store = None
    buffered = bytearray()
    event_count = 0
    final_seen = False
    started = time.monotonic()
    last_usable = started
    try:
        with serial.Serial(port=args.pc_serial, baudrate=args.baudrate, timeout=0.1) as stream:
            while time.monotonic() - last_usable <= args.timeout:
                chunk = bytes(stream.read(4096))
                if not chunk:
                    continue
                buffered.extend(chunk)
                if decoder is None:
                    newline = buffered.find(b"\n")
                    if newline < 0:
                        continue
                    try:
                        first = json.loads(bytes(buffered[:newline]).decode("utf-8"))
                        discovered_run = str(first["run_id"])
                        first_seq = int(first["seq"])
                    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                        raise EventProtocolError("invalid_first_event", f"cannot identify first monitor event: {exc}") from exc
                    if expected_run and discovered_run != expected_run:
                        raise EventProtocolError(
                            "wrong_run_id", f"stream run_id {discovered_run!r} does not match {expected_run!r}"
                        )
                    expected_run = discovered_run
                    decoder = EventDecoder(expected_run, first_seq=first_seq)
                    session_id = f"monitor-{expected_run}-{new_run_id('session').split('-', 2)[-1]}"
                    store = ArtifactStore.create(paths.output_root, session_id)
                assert decoder is not None and store is not None
                if args.save_raw:
                    store.append_raw_serial(bytes(buffered))
                events = decoder.feed(bytes(buffered))
                buffered.clear()
                if events:
                    last_usable = time.monotonic()
                for event in events:
                    store.append_event(event)
                    event_count += 1
                    if event.type == "agent_final":
                        final_seen = True
                        break
                if final_seen:
                    break
        if decoder is None or store is None:
            raise EventProtocolError("monitor_timeout", "no usable framed event received")
        result_path = store.finalize(
            {
                "schema_version": 1,
                "diagnostic": True,
                "verdict": "NOT_EVALUATED",
                "exit_code": 0,
                "observed_run_id": expected_run,
                "event_count": event_count,
                "agent_final_seen": final_seen,
                "message": "Diagnostic monitor does not issue a DUT verdict without a resolved run manifest and baseline.",
            }
        )
        print_command_result(
            args,
            {"observed_run_id": expected_run, "event_count": event_count, "agent_final_seen": final_seen, "result": str(result_path)},
        )
        return 0
    except EventProtocolError as exc:
        if store is not None:
            store.close_incomplete(str(exc))
        raise CommandError(str(exc), int(RunExitCode.INFRA_ERROR)) from exc


@command_boundary
def cmd_collect(args: Namespace) -> int:
    paths = make_paths(args)
    transport = _transport(args, paths)
    remote = PurePosixPath(args.remote_run_dir) if args.remote_run_dir else paths.remote(PurePosixPath("runs") / args.run_id / "spool")
    local = paths.resolve_output(f"{args.run_id}/device-spool")
    transfer = transport.pull(remote, local)
    if not transfer.success:
        raise TransportError(f"collection failed: {transfer.message}")
    verified = False
    if args.verify_hashes:
        manifests = list(local.rglob("artifact-hashes.json")) if local.exists() else []
        if len(manifests) != 1:
            raise TransportError(f"expected one device artifact-hashes.json under {local}, found {len(manifests)}")
        hash_document = json.loads(manifests[0].read_text(encoding="utf-8"))
        hashes = hash_document.get("sha256", {})
        if not isinstance(hashes, dict):
            raise TransportError("device artifact hash manifest is malformed")
        mismatches: list[str] = []
        for relative, expected in hashes.items():
            artifact = manifests[0].parent / relative
            if not artifact.exists() or sha256_file(artifact) != expected:
                mismatches.append(relative)
        if mismatches:
            raise TransportError(f"collected artifact hash mismatch: {mismatches}")
        verified = True
    remote_removed = False
    if verified and not args.keep_remote:
        normalized_remote = PurePosixPath(remote)
        runs_root = paths.remote("runs")
        if normalized_remote == runs_root or runs_root not in normalized_remote.parents:
            raise ConfigError(f"refusing to remove remote path outside a specific run: {normalized_remote}")
        removal = transport.invoke(("rm", "-rf", str(normalized_remote)), timeout_s=30.0)
        if not removal.success:
            raise TransportError(f"collected artifacts verified but remote cleanup failed: {removal.stderr}")
        remote_removed = True
    record = {
        "schema_version": 1,
        "run_id": args.run_id,
        "remote": str(remote),
        "local": str(local),
        "bytes_transferred": transfer.bytes_transferred,
        "verified": verified,
        "remote_removed": remote_removed,
    }
    output = paths.resolve_output(f"{args.run_id}/collection.json", create_parent=True)
    atomic_write_json(output, record)
    print_command_result(args, {"run_id": args.run_id, "collection": str(output), "local": str(local)})
    return 0


@command_boundary
def cmd_report(args: Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve(strict=True)
    result_path = run_dir / "result.json"
    if not result_path.exists():
        raise ConfigError(f"result.json not found: {result_path}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    requested = {item.strip() for item in args.format.split(",") if item.strip()}
    outputs: list[str] = []
    if "json" in requested:
        destination = run_dir / "report.json"
        atomic_write_json(destination, result)
        outputs.append(str(destination))
    if "markdown" in requested:
        destination = run_dir / "report.md"
        lines = [
            f"# Run report: {result.get('run_id', run_dir.name)}",
            "",
            f"- Verdict: **{result.get('verdict', 'UNKNOWN')}**",
            f"- Exit code: {result.get('exit_code')}",
            f"- Profile: {result.get('profile_id')}",
            f"- Baseline: {result.get('baseline_id')}",
            f"- Workload result: {result.get('workload_result')}",
            f"- Workload exit: {result.get('workload_exit_code')}",
            "",
            "## DUT reasons",
            "",
            "```json",
            json.dumps(result.get("dut_reasons", []), indent=2, ensure_ascii=False),
            "```",
            "",
            "## Infrastructure reasons",
            "",
            "```json",
            json.dumps(result.get("infrastructure_reasons", []), indent=2, ensure_ascii=False),
            "```",
            "",
        ]
        destination.write_text("\n".join(lines), encoding="utf-8")
        outputs.append(str(destination))
    if "csv" in requested:
        destination = run_dir / "report.csv"
        with destination.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("run_id", "verdict", "exit_code", "profile_id", "baseline_id", "workload_result", "workload_exit_code"))
            writer.writerow(tuple(result.get(key) for key in ("run_id", "verdict", "exit_code", "profile_id", "baseline_id", "workload_result", "workload_exit_code")))
        outputs.append(str(destination))
    unsupported = requested - {"json", "markdown", "csv"}
    if unsupported:
        raise ConfigError(f"unsupported report formats: {sorted(unsupported)}")
    print_command_result(args, {"run_id": result.get("run_id"), "reports": outputs})
    return 0


@command_boundary
def cmd_validate_v2(args: Namespace) -> int:
    paths = make_paths(args)
    errors: list[str] = []
    checked: list[str] = []
    resolved_configs: list[dict[str, Any]] = []
    profile_names: list[str] = []
    validate_all = bool(args.all or args.package or not (args.profile or args.baseline or args.package))
    if args.profile:
        profile_names.append(args.profile)
    elif validate_all:
        profile_root = paths.resolve_input("config/profiles", required=False)
        if profile_root.exists():
            profile_names.extend(path.stem for path in sorted(profile_root.glob("*.yaml")))
    for name in profile_names:
        try:
            profile = load_profile(paths, name)
            checked.append(f"profile:{profile.name}")
            resolved_configs.append(
                {"kind": "profile", "name": profile.name, "path": str(profile.source_path), "sha256": profile.fingerprint}
            )
            platform = load_platform(paths, profile.platform)
            checked.append(f"platform:{profile.platform}")
            platform_record = {
                "kind": "platform",
                "name": platform.name,
                "path": str(platform.source_path),
                "sha256": platform.fingerprint,
            }
            if platform_record not in resolved_configs:
                resolved_configs.append(platform_record)
            if profile.workload.get("config"):
                paths.resolve_input(str(profile.workload["config"]), owner=profile.source_path)
                checked.append(f"workload-config:{profile.name}")
            paths.resolve_input(str(profile.workload["binary"]), owner=profile.source_path)
            checked.append(f"workload-binary:{profile.name}")
            for declared in profile.workload.get("assets", []):
                paths.resolve_input(
                    str(declared["local"]),
                    owner=profile.source_path,
                    required=bool(declared.get("required", True)),
                )
                checked.append(f"workload-asset:{profile.name}:{declared['remote']}")
        except (ConfigError, OSError, PathResolutionError) as exc:
            errors.append(str(exc))
    if args.baseline:
        try:
            value = Path(args.baseline)
            baseline = Baseline.from_mapping(json.loads(value.read_text(encoding="utf-8"))) if value.exists() else BaselineRegistry(paths.state_root).get(args.baseline)
            checked.append(f"baseline:{baseline.id}")
        except (BaselineError, OSError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
    if args.package:
        for resource in ("device/avs_device_agent.sh", "config/kernel/critical.conf"):
            try:
                paths.resolve_resource(resource)
                checked.append(f"resource:{resource}")
            except PathResolutionError as exc:
                errors.append(str(exc))
    report = {
        "valid": not errors,
        "checked": checked,
        "resolved_configs": resolved_configs,
        "errors": errors,
        "offline": bool(args.offline),
    }
    print_command_result(args, report)
    return 0 if not errors else int(RunExitCode.INVALID_CONFIGURATION)


@command_boundary
def cmd_list_profiles_v2(args: Namespace) -> int:
    paths = make_paths(args)
    root = paths.resolve_input("config/profiles")
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.yaml")) + sorted(root.glob("*.json")):
        profile = ProfileConfig.from_file(path)
        if args.target and profile.target != args.target:
            continue
        if args.status and args.status != "implemented":
            continue
        records.append(
            {
                "name": profile.name,
                "target": profile.target,
                "platform": profile.platform,
                "baseline": profile.baseline,
                "status": "implemented",
                "path": str(path),
            }
        )
    print_command_result(args, {"count": len(records), "profiles": records})
    return 0

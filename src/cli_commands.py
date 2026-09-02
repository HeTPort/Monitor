"""Target v2 CLI handlers built on the refactored service contracts."""

from __future__ import annotations

import csv
import json
import re
import shutil
import sys
import time
from argparse import Namespace
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .artifact_store import ArtifactStore, atomic_write_bytes, atomic_write_json, sha256_file
from .baselines import Baseline, BaselineError, BaselineRegistry
from .config_loader import ConfigError, PlatformConfig, ProfileConfig, load_document, validate_workload_config
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
from .qualification_artifacts import resolve_qualification_run
from .run_orchestrator import RunError, RunInfrastructureError, RunManifestBuilder, RunOrchestrator, new_run_id
from .transport import ADBTransport, HDCTransport, Transport, TransportError, TransportManager
from .transport_probe import TransportProbeBackend
from .uart_protocol import UART_PROTOCOL, UartV2SessionDecoder, discover_uart_session


CLI_VERSION = "2.1.0"


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
        except RunInfrastructureError as exc:
            code = int(RunExitCode.INFRA_ERROR)
            print_command_result(args, {"error": str(exc), "exit_code": code})
            return code
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


def _probe(args: Namespace, paths: PathResolver, transport: Transport) -> dict[str, Any]:
    """Run the explicit, platform-scoped, read-only capability probe."""
    platform_name = getattr(args, "platform", None)
    if not platform_name:
        raise ConfigError("platform is required")
    platform = load_platform(paths, platform_name)
    identity = transport.connect()
    probe = PlatformProbe(platform, TransportProbeBackend(transport, identity))
    result = probe.probe(
        full=bool(getattr(args, "full", False)),
        domains=None,
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
    probe.require(result, sorted(set(required)))
    tool_checks: dict[str, tuple[tuple[str, ...], bool]] = {
        "device.shell": (("sh", "-c", "exit 0"), True),
        "device.sha256sum": (("sha256sum", "--help"), True),
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


@command_boundary
def cmd_relay_probe(args: Namespace) -> int:
    """Inspect device ABI and, when deployed, exercise relay runtime capabilities."""
    paths = make_paths(args)
    platform = load_platform(paths, args.platform)
    _apply_saved_pairing(args, paths)
    if args.baudrate is None:
        args.baudrate = int(platform.serial.get("baudrate", 9600))
    if not args.device_uart:
        candidates = list(platform.serial.get("uart_candidates", []))
        args.device_uart = candidates[0] if candidates else None
    relay_config = dict(platform.serial.get("relay", {}))
    relay_remote = paths.remote(str(relay_config.get("remote_asset", "bin/avs-uart-relay")))
    transport = _transport(args, paths)

    def check(argv: tuple[str, ...], timeout_s: float = 10.0) -> dict[str, Any]:
        result = transport.invoke(argv, timeout_s=timeout_s)
        return {
            "available": result.success,
            "return_code": result.return_code,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }

    machine = check(("uname", "-m"))
    long_bit = check(("getconf", "LONG_BIT"))
    executable = check(("test", "-x", str(relay_remote)))
    payload: dict[str, Any] = {
        "platform": platform.name,
        "device_abi": {
            "uname_m": machine["stdout"] if machine["available"] else None,
            "long_bit": long_bit["stdout"] if long_bit["available"] else None,
            "raw_checks": {"uname": machine, "getconf": long_bit},
        },
        "relay": {
            "remote": str(relay_remote),
            "deployed_executable": executable["available"],
        },
        "uart": args.device_uart,
        "baudrate": args.baudrate,
    }
    if executable["available"]:
        payload["relay"]["version"] = check((str(relay_remote), "--version"))
        payload["relay"]["self_test"] = check((str(relay_remote), "--self-test"))
        if args.check_uart:
            if not args.device_uart:
                raise ConfigError("relay probe --check-uart requires a device UART or platform candidate")
            payload["relay"]["uart_check"] = check(
                (str(relay_remote), "--check-uart", args.device_uart, "--baud", str(args.baudrate))
            )
        checks = [payload["relay"]["version"], payload["relay"]["self_test"]]
        if "uart_check" in payload["relay"]:
            checks.append(payload["relay"]["uart_check"])
        payload["supported"] = all(item["available"] for item in checks)
    else:
        payload["supported"] = False
        payload["next_action"] = (
            "build native/uart_relay/avs_uart_relay.c with the workload OpenHarmony toolchain, "
            f"stage it as {relay_config.get('local_asset', 'the configured relay.local_asset')}, then deploy and repeat"
        )
    print_command_result(args, payload)
    return 0 if payload["supported"] else int(RunExitCode.UNSUPPORTED)


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
    validate_workload_config(workload_config, profile.target, device_root=paths.device_root)
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


def _telemetry_parser(metric: str, interface: Mapping[str, Any]) -> str:
    if interface.get("derivation") == "delta_busy_over_delta_total":
        return "proc_stat_utilization"
    if metric.endswith("temperature"):
        return "temperature_auto"
    if metric.endswith("online"):
        return "int"
    if interface.get("unit") in {"Hz", "kHz", "count", "us", "percent"}:
        return "number"
    return "text"


def _telemetry_plan_bytes(profile: ProfileConfig, platform: PlatformConfig) -> bytes:
    """Resolve a profile/platform pair into a shell-readable, data-only telemetry plan."""
    domain = platform.cpu if profile.target == "cpu" else platform.gpu
    interfaces = domain.get("interfaces", {}) if isinstance(domain, Mapping) else {}
    requested = list(dict.fromkeys([
        *profile.telemetry.get("required", []),
        *profile.telemetry.get("optional", []),
    ]))
    lines = ["# metric|parser|device-path-glob"]
    for metric in requested:
        if not isinstance(metric, str) or not metric.startswith(f"{profile.target}."):
            continue
        name = metric.split(".", 1)[1]
        if name == "temperature":
            candidates = ["/sys/class/thermal/thermal_zone*/temp"]
            parser = "temperature_auto"
        else:
            interface = interfaces.get(name, {}) if isinstance(interfaces, Mapping) else {}
            if not isinstance(interface, Mapping):
                continue
            candidates = interface.get("candidates", [])
            parser = _telemetry_parser(metric, interface)
        for candidate in candidates if isinstance(candidates, list) else []:
            if isinstance(candidate, str) and candidate and "|" not in candidate:
                lines.append(f"{metric}|{parser}|{candidate}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _telemetry_assets(paths: PathResolver, profile: ProfileConfig) -> list[AssetSpec]:
    platform = load_platform(paths, profile.platform)
    local_plan = paths.resolve_output(f"prepared/telemetry/{profile.name}.conf", create_parent=True)
    atomic_write_bytes(local_plan, _telemetry_plan_bytes(profile, platform))
    return [
        AssetSpec(
            paths.resolve_resource("device/avs_telemetry_agent.sh"),
            paths.remote("bin/avs-telemetry-agent"),
            executable=True,
            kind="telemetry-agent",
        ),
        AssetSpec(
            local_plan,
            paths.remote(f"configs/telemetry/{profile.name}.conf"),
            kind="telemetry-plan",
        ),
    ]


def _requested_deployment_profiles(args: Namespace, paths: PathResolver) -> list[ProfileConfig]:
    if getattr(args, "profile", None):
        profiles = [load_profile(paths, args.profile)]
    else:
        defaults = {"cpu": "cpu_mixed_big4", "gpu": "gpu_vulkan_mixed"}
        targets = ("cpu", "gpu") if args.target == "all" else (args.target,)
        profiles = [load_profile(paths, defaults[target]) for target in targets]
    for profile in profiles:
        if args.target and args.target != "all" and profile.target != args.target:
            raise ConfigError(f"profile target {profile.target} does not match --target {args.target}")
    return profiles


def _deployment_assets(args: Namespace, paths: PathResolver) -> list[AssetSpec]:
    agent = AssetSpec(
        paths.resolve_resource("device/avs_device_agent.sh"),
        paths.remote("bin/avs-device-agent"),
        executable=True,
        kind="agent",
    )
    planned: dict[str, AssetSpec] = {str(agent.remote): agent}
    if getattr(args, "baseline", None) and getattr(args, "target", None) == "all":
        raise ConfigError("--baseline requires --profile or one specific --target")
    for profile in _requested_deployment_profiles(args, paths):
        platform = load_platform(paths, profile.platform)
        relay = dict(platform.serial.get("relay", {}))
        relay_local = str(relay.get("local_asset", f"tools/relay/{platform.name}/avs-uart-relay"))
        relay_remote = paths.remote(str(relay.get("remote_asset", "bin/avs-uart-relay")))
        relay_asset = AssetSpec(
            paths.resolve_resource(relay_local, required=False),
            relay_remote,
            executable=True,
            required=True,
            kind="uart-relay",
        )
        planned[str(relay_asset.remote)] = relay_asset
        baseline = _resolve_baseline(args, paths, profile) if getattr(args, "baseline", None) else None
        if baseline is not None:
            RunManifestBuilder._validate_baseline(profile, baseline)
        profile_assets, _, _ = _asset_plan(paths, profile, baseline)
        for asset in [*profile_assets, *_telemetry_assets(paths, profile)]:
            planned[str(asset.remote)] = asset
    return list(planned.values())


def _shell_agent_argv(agent_remote: PurePosixPath, manifest: Mapping[str, Any], baudrate: int) -> list[str]:
    """Translate a manifest into a short, fixed agent invocation."""

    def field(value: Any, name: str) -> str:
        text = str(value)
        if not text or any(character in text for character in "\r\n\x00"):
            raise ConfigError(f"shell agent {name} contains an unsupported delimiter")
        return text

    argv = [
        "sh",
        str(agent_remote),
        "--test-id",
        field(manifest.get("test_id", manifest["run_id"]), "test_id"),
        "--attempt-id",
        field(manifest.get("attempt_id", manifest["run_id"]), "attempt_id"),
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
        "--relay",
        field(manifest["serial_transport"]["relay"], "serial_transport.relay"),
        "--max-frame",
        str(int(manifest["serial_transport"].get("max_frame_bytes", 512))),
        "--tail-guard",
        str(int(manifest["serial_transport"].get("tail_guard_bytes", 64))),
        "--safe-utilization",
        str(int(float(manifest["serial_transport"].get("safe_utilization", 0.70)) * 100)),
        "--timeout",
        str(max(1, int(float(manifest.get("timeout_s", 300))))),
    ]
    telemetry = manifest.get("telemetry", {})
    if isinstance(telemetry, Mapping) and telemetry.get("enabled"):
        argv.extend(
            (
                "--telemetry-agent",
                field(telemetry["agent"], "telemetry.agent"),
                "--telemetry-plan",
                field(telemetry["plan"], "telemetry.plan"),
                "--telemetry-interval",
                str(max(1, int(telemetry.get("interval_s", 5)))),
            )
        )
    workload_argv = manifest.get("workload", {}).get("argv", [])
    if not isinstance(workload_argv, list) or not workload_argv:
        raise ConfigError("shell agent requires a non-empty workload argv list")
    thresholds = manifest.get("policy", {}).get("thresholds", {})
    performance = thresholds.get("performance", {}) if isinstance(thresholds, Mapping) else {}
    if isinstance(performance, Mapping):
        for metric in sorted(performance):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(metric)):
                raise ConfigError(f"baseline performance metric is not relay-safe: {metric!r}")
            argv.extend(("--summary-metric", str(metric)))
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
                f"deployed asset mismatch: {asset.remote}; local={local_hash} remote={remote_hash}"
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
        "operation": "verify-only",
    }


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
    effective_golden = dict(golden or {})
    agent_remote = paths.remote("bin/avs-device-agent")
    run_dirs: list[Path] = []
    test_id = new_run_id(f"{mode}-{profile.target}")
    for repetition in range(count):
        attempt_id = (
            f"{test_id}-{repetition + 1:03d}"
            if count > 1
            else new_run_id(f"{test_id}-attempt")
        )
        manifest = RunManifestBuilder(paths).build_qualification(
            profile=profile,
            golden=effective_golden,
            capabilities=None,
            mode=mode,
            test_id=test_id,
            attempt_id=attempt_id,
            overall_timeout_s=float(getattr(args, "overall_timeout", 300.0)),
            heartbeat_timeout_s=float(getattr(args, "heartbeat_timeout", 45.0)),
            device_uart=args.device_uart,
            telemetry_enabled=mode == "golden",
        )
        execution = RunOrchestrator(paths.output_root).run_serial(
            manifest,
            transport=transport,
            agent_argv=_shell_agent_argv(agent_remote, manifest, args.baudrate),
            pc_serial=args.pc_serial,
            baudrate=args.baudrate,
        )
        if execution.result.verdict != "PASS":
            raise QualificationError(
                f"live {mode} attempt {attempt_id} did not pass: {execution.result.verdict}; {execution.result_path}"
            )
        run_dir = execution.result_path.parent
        evidence_dir = run_dir / "device-evidence"
        transfer = transport.pull(PurePosixPath(manifest["device_attempt_dir"]), evidence_dir)
        if not transfer.success:
            raise QualificationError(
                f"failed to collect complete qualification evidence for {attempt_id}: {transfer.message}"
            )
        normalized = resolve_qualification_run(run_dir)
        if normalized.events_path is None:
            raise QualificationError(f"collected qualification events are missing for {attempt_id}")
        if normalized.summary:
            atomic_write_json(run_dir / "workload-summary-full.json", normalized.summary)
        if mode == "golden" and profile.target == "gpu" and normalized.readback_path is None:
            raise QualificationError(f"collected GPU golden readback is missing for {attempt_id}")
        run_dirs.append(run_dir)
    return run_dirs


@command_boundary
def cmd_smoke(args: Namespace) -> int:
    """Deprecated compatibility alias for a short-profile, baseline-free run."""
    print("warning: smoke is deprecated; use run with the same short profile", file=sys.stderr)
    args.baseline = None
    return _execute_run_command(args, smoke=True)


@command_boundary
def cmd_deploy(args: Namespace) -> int:
    if args.clean_stale and args.target != "all":
        raise ConfigError("--clean-stale is valid only with --target all")
    if args.baseline and args.target == "all":
        raise ConfigError("--baseline requires --profile or one specific --target")
    paths = make_paths(args)
    paths.ensure_writable_roots()
    transport = _transport(args, paths)
    assets = _deployment_assets(args, paths)
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
        verify_hashes=True,
        manifest_path=output,
        clean_stale=bool(args.clean_stale),
        previous_manifest=previous_manifest,
        allowed_remote_root=paths.device_root,
    )
    print_command_result(args, {"complete": manifest["complete"], "verified": manifest["verified"], "manifest": str(output)})
    return 0


@command_boundary
def cmd_verify_deployment(args: Namespace) -> int:
    """Read-only verification of the assets selected by one or more profiles."""
    paths = make_paths(args)
    paths.ensure_writable_roots()
    transport = _transport(args, paths)
    manifest = _verify_existing_assets(transport, _deployment_assets(args, paths))
    output = paths.resolve_output("deployment-verification.json", create_parent=True)
    atomic_write_json(output, manifest)
    print_command_result(args, {"complete": True, "verified": True, "manifest": str(output)})
    return 0


def _events_from_run(run_dir: Path, event_type: str) -> list[dict[str, Any]]:
    normalized = resolve_qualification_run(run_dir)
    path = normalized.events_path
    if path is None:
        raise QualificationError(f"events artifact missing for qualification run: {run_dir}")
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
    if args.runs < 1:
        raise ConfigError("--runs must be at least 1")
    run_specs = _qualified_run_specs(args.run_dir, args.board_id)
    if run_specs and len(run_specs) != args.runs:
        raise ConfigError(
            f"golden requires either zero --run-dir values for live capture or exactly {args.runs}; "
            f"received {len(run_specs)}"
        )
    if not run_specs:
        live = _execute_live_qualification(args, paths, profile, mode="golden", count=args.runs, golden=None)
        run_specs = [(args.board_id, run_dir) for run_dir in live]
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
        readbacks: list[Path] = []
        for _, run_dir in run_specs[: args.runs]:
            normalized = resolve_qualification_run(run_dir)
            readback = normalized.readback_path
            if readback is None and args.readback_name != "gpu-golden.rgba":
                readback = next(
                    (
                        candidate
                        for candidate in (
                            run_dir / args.readback_name,
                            *( [normalized.spool_dir / args.readback_name] if normalized.spool_dir else [] ),
                        )
                        if candidate.exists()
                    ),
                    None,
                )
            if readback is None:
                raise QualificationError(f"GPU readback {args.readback_name!r} is missing for {run_dir}")
            readbacks.append(readback)
        manifest = service.create_gpu(
            qualification_id=qualification_id,
            profile=profile.name,
            fingerprint_fields=fields,
            golden_records=records,
            readback_files=readbacks,
            board_ids=board_ids,
        )
    print_command_result(
        args,
        {
            "qualification_id": qualification_id,
            "golden_manifest": manifest["manifest_path"],
            "sha256": manifest["manifest_sha256"],
            "source_mode": "supplied" if args.run_dir else "live-capture",
            "source_runs": [str(run_dir) for _, run_dir in run_specs[: args.runs]],
        },
    )
    return 0


def _sample_from_run(run_dir: Path, board_id: str) -> CalibrationSample:
    normalized = resolve_qualification_run(run_dir)
    if normalized.result_path is None or not normalized.summary:
        raise QualificationError(f"run is missing PC result or full workload summary: {run_dir}")
    result = json.loads(normalized.result_path.read_text(encoding="utf-8"))
    summary = normalized.summary
    temperatures: list[float] = []
    telemetry_path = normalized.telemetry_path
    telemetry_complete = telemetry_path is not None
    throttled = False
    if telemetry_path is not None:
        for line in telemetry_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise QualificationError(f"invalid telemetry in {telemetry_path}: {exc}") from exc
            payload = event.get("payload", {})
            metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
            if isinstance(payload, dict) and isinstance(payload.get("metric"), str):
                metrics = {payload["metric"]: payload.get("value")}
            for key, value in metrics.items():
                if key.endswith("temperature") or ".temperature." in key:
                    if isinstance(value, (int, float)):
                        temperatures.append(float(value))
                if "throttle" in key and value not in {0, 0.0, "0", "none", "off", False}:
                    throttled = True
    return CalibrationSample(
        run_id=str(result.get("run_id", normalized.pc_run_dir.name if normalized.pc_run_dir else run_dir.name)),
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
    if args.runs < 1:
        raise ConfigError("--runs must be at least 1")
    if len(run_specs) != args.runs:
        raise QualificationError(
            f"calibrate requires exactly {args.runs} collected --run-dir samples; received {len(run_specs)}"
        )
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


def _execute_run_command(args: Namespace, *, smoke: bool = False) -> int:
    if args.repeat < 1:
        raise ConfigError("--repeat must be at least 1")
    paths = make_paths(args)
    paths.ensure_writable_roots()
    _apply_saved_pairing(args, paths)
    if not args.pc_serial:
        raise ConfigError("--pc-serial is required when no saved pairing exists")
    profile = load_profile(paths, args.profile)
    _apply_platform_serial(args, paths, profile)
    baseline_value = getattr(args, "baseline", None)
    baseline = None
    if baseline_value and str(baseline_value).lower() != "none":
        baseline = _resolve_baseline(args, paths, profile)
        _require_current_correctness(paths, profile, baseline.golden)
    transport = _transport(args, paths)
    agent_remote = paths.remote("bin/avs-device-agent")
    test_id = getattr(args, "test_id", None) or new_run_id(f"test-{profile.target}")
    explicit_attempt = getattr(args, "attempt_id", None)
    if explicit_attempt and args.repeat != 1:
        raise ConfigError("--attempt-id may only be used with --repeat 1")
    results: list[dict[str, Any]] = []
    final_exit = 0
    for repetition in range(args.repeat):
        if explicit_attempt:
            attempt_id = explicit_attempt
        elif args.repeat > 1:
            attempt_id = f"{test_id}-{repetition + 1:03d}"
        else:
            attempt_id = new_run_id(f"{test_id}-attempt")
        manifest = RunManifestBuilder(paths).build(
            profile=profile,
            baseline=baseline,
            test_id=test_id,
            attempt_id=attempt_id,
            overall_timeout_s=args.overall_timeout,
            heartbeat_timeout_s=args.heartbeat_timeout,
            device_uart=args.device_uart,
            telemetry_enabled=bool(getattr(args, "telemetry", False)),
            pc_artifacts=str(getattr(args, "pc_artifacts", "result")),
        )
        agent_argv = _shell_agent_argv(agent_remote, manifest, args.baudrate)
        execution = RunOrchestrator(paths.output_root).run_serial(
            manifest,
            transport=transport,
            agent_argv=agent_argv,
            pc_serial=args.pc_serial,
            baudrate=args.baudrate,
        )
        final_exit = max(final_exit, execution.result.exit_code)
        run_result: dict[str, Any] = {
            "test_id": test_id,
            "attempt_id": attempt_id,
            "run_id": attempt_id,
            "verdict": execution.result.verdict,
            "exit_code": execution.result.exit_code,
            "result": str(execution.result_path),
            "device_evidence": str(manifest["device_attempt_dir"]),
            "device_spool": "retained",
        }
        if execution.result.verdict != "PASS":
            run_result["errors"] = [
                _concise_reason(reason)
                for reason in [
                    *execution.result.infrastructure_reasons,
                    *execution.result.dut_reasons,
                ]
            ]
        results.append(run_result)
    payload: dict[str, Any] = {
        "test_id": test_id,
        "validation_mode": "baseline" if baseline is not None else "error-only",
        "repeat": args.repeat,
        "exit_code": final_exit,
        "runs": results,
    }
    if smoke:
        payload["minimum_closed_loop"] = final_exit == 0 and all(
            run.get("verdict") == "PASS" for run in results
        )
    print_command_result(args, payload)
    return final_exit


@command_boundary
def cmd_run(args: Namespace) -> int:
    return _execute_run_command(args, smoke=False)


@command_boundary
def cmd_simulate(args: Namespace) -> int:
    paths = make_paths(args)
    source_value = args.events or args.raw_serial
    if args.baseline and not args.profile:
        raise ConfigError("--baseline requires --profile")
    source = paths.resolve_input(source_value)
    data = source.read_bytes()
    if not data:
        raise ConfigError("simulation input is empty")
    test_id: str | None = None
    if args.raw_serial:
        session = discover_uart_session(data)
        if session is None:
            raise ConfigError("raw serial input has no valid UART-v2 agent_start frame")
        test_id, run_id = session
    else:
        first_line = next((line for line in data.splitlines() if line.strip()), None)
        if first_line is None:
            raise ConfigError("simulation input is empty")
        try:
            first_record = json.loads(first_line)
            run_id = str(first_record["run_id"])
            raw_test_id = first_record.get("test_id")
            test_id = str(raw_test_id) if raw_test_id else None
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ConfigError("events input does not begin with a framed JSON event") from exc
    policy: dict[str, Any] = {"thresholds": {}, "required_telemetry": []}
    if args.profile:
        profile = load_profile(paths, args.profile)
        if args.baseline:
            policy["thresholds"] = _resolve_baseline(args, paths, profile).thresholds
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        **({"test_id": test_id, "attempt_id": run_id} if test_id is not None else {}),
        "profile": {"id": args.profile},
        "baseline": {"id": args.baseline},
        "policy": policy,
        "overall_timeout_s": 86400,
        "heartbeat_timeout_s": 86400,
        **(
            {
                "pc_artifacts": "full",
                "serial_transport": {"protocol": UART_PROTOCOL, "max_frame_bytes": 512},
            }
            if args.raw_serial
            else {}
        ),
    }
    chunks: Iterable[bytes] = [data]
    if args.realtime:
        if args.raw_serial:
            raise ConfigError("--realtime currently requires --events; raw UART-v2 replay is deterministic and immediate")
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
        {
            "test_id": test_id,
            "run_id": run_id,
            "verdict": execution.result.verdict,
            "exit_code": execution.result.exit_code,
            "result": str(execution.result_path),
        },
    )
    return execution.result.exit_code


@command_boundary
def cmd_monitor_events(args: Namespace) -> int:
    if args.schema_version != 1:
        raise ConfigError(f"unsupported event schema version: {args.schema_version}")
    paths = make_paths(args)
    _apply_saved_pairing(args, paths)
    if not args.pc_serial:
        raise ConfigError("--pc-serial is required for monitor")
    if args.baudrate is None:
        raise ConfigError("--baudrate is required for monitor when no saved pairing exists")
    try:
        import serial
    except ImportError as exc:
        raise CommandError("pyserial is required for monitor; install requirements.txt") from exc
    expected_run = args.expected_run_id
    decoder = UartV2SessionDecoder(expected_run_id=expected_run)
    store = None
    raw_preamble = bytearray()
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
                if store is None and args.save_raw:
                    raw_preamble.extend(chunk)
                events = decoder.feed(chunk)
                if decoder.run_id is not None and store is None:
                    expected_run = decoder.run_id
                    session_id = f"monitor-{expected_run}-{new_run_id('session').split('-', 2)[-1]}"
                    store = ArtifactStore.create(paths.output_root, session_id)
                    if args.save_raw and raw_preamble:
                        store.append_raw_serial(bytes(raw_preamble))
                        raw_preamble.clear()
                elif store is not None and args.save_raw:
                    store.append_raw_serial(chunk)
                if events:
                    last_usable = time.monotonic()
                for event in events:
                    assert store is not None
                    store.append_event(event)
                    event_count += 1
                    if event.type == "agent_final":
                        final_seen = True
                        break
                if final_seen:
                    break
        if store is None:
            raise EventProtocolError("monitor_timeout", "no matching UART-v2 agent_start frame received")
        decoder.finish()
        if not final_seen:
            raise EventProtocolError("missing_final", "UART-v2 diagnostic session ended without agent_final")
        result_path = store.finalize(
            {
                "schema_version": 1,
                "diagnostic": True,
                "verdict": "NOT_EVALUATED",
                "exit_code": 0,
                "observed_run_id": expected_run,
                "event_count": event_count,
                "agent_final_seen": final_seen,
                "message": "Diagnostic monitor does not issue a DUT verdict without a resolved run manifest.",
            }
        )
        print_command_result(
            args,
            {
                "verdict": "NOT_EVALUATED",
                "observed_run_id": expected_run,
                "event_count": event_count,
                "agent_final_seen": final_seen,
                "result": str(result_path),
            },
        )
        return 0
    except EventProtocolError as exc:
        if store is not None:
            store.close_incomplete(str(exc))
        raise CommandError(str(exc), int(RunExitCode.INFRA_ERROR)) from exc


@command_boundary
def cmd_telemetry(args: Namespace) -> int:
    """Run the already deployed telemetry collector without UART or workload dependencies."""
    paths = make_paths(args)
    profile = load_profile(paths, args.profile)
    test_id = args.test_id
    attempt_id = args.attempt_id or new_run_id(f"{test_id}-telemetry")
    identifier = re.compile(r"^[A-Za-z0-9._:-]+$")
    if not identifier.fullmatch(test_id) or not identifier.fullmatch(attempt_id):
        raise ConfigError("test/attempt IDs may contain only letters, digits, dot, underscore, colon, and dash")
    interval_s = args.interval or max(1, (int(profile.telemetry.get("interval_ms", 5000)) + 999) // 1000)
    if args.duration < 0 or interval_s < 1:
        raise ConfigError("telemetry duration must be non-negative and interval must be positive")
    transport = _transport(args, paths)
    remote_attempt = paths.remote(PurePosixPath("tests") / test_id / attempt_id)
    remote_output = remote_attempt / "spool" / "telemetry.jsonl"
    argv = [
        "sh",
        str(paths.remote("bin/avs-telemetry-agent")),
        "--test-id",
        test_id,
        "--attempt-id",
        attempt_id,
        "--target",
        profile.target,
        "--output",
        str(remote_output),
        "--plan",
        str(paths.remote(f"configs/telemetry/{profile.name}.conf")),
        "--interval",
        str(interval_s),
        "--duration",
        str(args.duration),
    ]
    result = transport.invoke(argv, timeout_s=max(30.0, float(args.duration) + float(interval_s) + 15.0))
    if not result.success:
        raise TransportError(result.stderr or result.stdout or "telemetry collector failed")
    print_command_result(
        args,
        {
            "test_id": test_id,
            "attempt_id": attempt_id,
            "profile": profile.name,
            "device_output": str(remote_output),
            "append_only": True,
        },
    )
    return 0


@command_boundary
def cmd_collect(args: Namespace) -> int:
    paths = make_paths(args)
    test_id = args.test_id
    if args.remove_remote_after_verify and not args.verify_hashes:
        raise ConfigError("--remove-remote-after-verify requires --verify-hashes")
    transport = _transport(args, paths)
    attempt_id = getattr(args, "attempt_id", None)
    remote = (
        PurePosixPath(args.remote_run_dir)
        if args.remote_run_dir
        else paths.remote(PurePosixPath("tests") / test_id / attempt_id)
        if attempt_id
        else paths.remote(PurePosixPath("tests") / test_id)
    )
    local_relative = Path(test_id) / "device-evidence"
    if attempt_id:
        local_relative /= attempt_id
    local = paths.resolve_output(local_relative)
    transfer = transport.pull(remote, local)
    if not transfer.success:
        raise TransportError(f"collection failed: {transfer.message}")
    verified = False
    if args.verify_hashes:
        manifests = list(local.rglob("artifact-hashes.json")) if local.exists() else []
        if not manifests:
            raise TransportError(f"no device artifact-hashes.json found under {local}")
        mismatches: list[str] = []
        for manifest_path in manifests:
            hash_document = json.loads(manifest_path.read_text(encoding="utf-8"))
            hashes = hash_document.get("sha256", {})
            if not isinstance(hashes, dict):
                raise TransportError(f"device artifact hash manifest is malformed: {manifest_path}")
            for relative, expected in hashes.items():
                artifact = manifest_path.parent / relative
                if not artifact.exists() or sha256_file(artifact) != expected:
                    mismatches.append(str(artifact.relative_to(local)))
        if mismatches:
            raise TransportError(f"collected artifact hash mismatch: {mismatches}")
        verified = True
    remote_removed = False
    remove_requested = bool(getattr(args, "remove_remote_after_verify", False))
    if verified and remove_requested:
        normalized_remote = PurePosixPath(remote)
        tests_root = paths.remote("tests")
        if normalized_remote == tests_root or tests_root not in normalized_remote.parents:
            raise ConfigError(f"refusing to remove remote path outside a specific test: {normalized_remote}")
        removal = transport.invoke(("rm", "-rf", str(normalized_remote)), timeout_s=30.0)
        if not removal.success:
            raise TransportError(f"collected artifacts verified but remote cleanup failed: {removal.stderr}")
        remote_removed = True
    record = {
        "schema_version": 1,
        "test_id": test_id,
        "attempt_id": attempt_id,
        "remote": str(remote),
        "local": str(local),
        "bytes_transferred": transfer.bytes_transferred,
        "verified": verified,
        "remote_removed": remote_removed,
    }
    output = paths.resolve_output(Path(test_id) / "collection.json", create_parent=True)
    atomic_write_json(output, record)
    print_command_result(args, {"test_id": test_id, "attempt_id": attempt_id, "collection": str(output), "local": str(local)})
    return 0


@command_boundary
def cmd_report(args: Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve(strict=True)
    result_path = run_dir / "result.json"
    if not result_path.exists():
        raise ConfigError(f"result.json not found: {result_path}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    requested = {item.strip() for item in args.format.split(",") if item.strip()}
    if not requested:
        raise ConfigError("--format must include at least one of markdown,json,csv")
    unsupported = requested - {"json", "markdown", "csv"}
    if unsupported:
        raise ConfigError(f"unsupported report formats: {sorted(unsupported)}")
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
    print_command_result(args, {"run_id": result.get("run_id"), "reports": outputs})
    return 0


@command_boundary
def cmd_validate_v2(args: Namespace) -> int:
    paths = make_paths(args)
    errors: list[str] = []
    checked: list[str] = []
    resolved_configs: list[dict[str, Any]] = []
    profile_names: list[str] = []
    validate_all = bool(args.package or not (args.profile or args.baseline))
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
                workload_config = paths.resolve_input(str(profile.workload["config"]), owner=profile.source_path)
                validate_workload_config(workload_config, profile.target, device_root=paths.device_root)
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
        for resource in ("device/avs_device_agent.sh", "device/avs_telemetry_agent.sh"):
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
        "offline": True,
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

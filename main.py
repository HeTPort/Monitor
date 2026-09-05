#!/usr/bin/env python3
"""
Vmin Judge Tool - Main Entry Point

A PC-side Python tool that monitors serial port output from embedded device tests
to determine PASS/FAIL verdicts for Vmin (Voltage Minimum) testing.

Usage:
    # Direct Python execution
    python main.py --help

    # After packaging with PyInstaller
    vmin_judge.exe --help
    vmin_judge.exe probe --platform kirin9030 --full
    vmin_judge.exe run --profile gpu_vulkan_mixed --test-id TEST-001

Version: 2.0
"""

import sys
import io
import os
import time
import logging
import argparse
import json
from typing import Optional

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Add src to path for direct execution
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import directly from submodules to avoid broken __init__.py imports
from src.channel_manager import create_channel_manager
from src.path_resolver import PathResolver


# Configure logging
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG) 


console_handler = logging.StreamHandler()
console_handler.setLevel(logging.WARNING)
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))

root_logger.addHandler(console_handler)

logger = logging.getLogger('vmin_judge')
runtime_paths = PathResolver.create(entrypoint=__file__)


# =============================================================================
# Pairing Result Persistence
# =============================================================================




def _get_pairing_config_path() -> str:
    """Get the path to the pairing configuration file."""
    return str(runtime_paths.resolve_state('pairing.conf'))

def _ensure_config_dir() -> str:
    """Ensure the config directory exists, return the path."""
    config_dir = os.path.dirname(_get_pairing_config_path())
    if not os.path.exists(config_dir):
        os.makedirs(config_dir, exist_ok=True)
    return config_dir

def save_pairing_result(device_port: str, pc_port: str, baudrate: int = 9600, confidence: float = 1.0) -> bool:
    """
    Save pairing result to config file for persistence.

    Args:
        device_port: Device serial port (e.g., '/dev/tty0')
        pc_port: PC serial port (for example, 'COM4' or '/dev/ttyUSB0')
        confidence: Pairing confidence (0.0-1.0)

    Returns:
        True if saved successfully, False otherwise.
    """
    try:
        config_path = _get_pairing_config_path()
        config_data = {
            'device_port': device_port,
            'pc_port': pc_port,
            'baudrate':baudrate,
            'confidence': confidence,
            'saved_at': time.strftime('%Y-%m-%d %H:%M:%S')
        }

        _ensure_config_dir()

        with open(config_path, 'w') as f:
            json.dump(config_data, f, indent=2)

        logger.debug("Pairing result saved to %s", config_path)
        return True

    except Exception as e:
        logger.warning(f"Failed to save pairing result: {e}")
        return False


def save_pairing_diagnostic(result, *, verification: Optional[bool] = None) -> Optional[str]:
    """Persist bounded pair-stage evidence without printing UART contents."""
    try:
        destination = runtime_paths.resolve_state("pair-diagnostic.json", create_parent=True)
        payload = {
            "schema_version": 1,
            "saved_at": time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            "success": bool(result.success),
            "failure_code": result.failure_code,
            "attempts": result.attempts,
            "duration_sec": result.duration_sec,
            "device_port": result.device_port,
            "pc_port": result.pc_port,
            "diagnostics": list(result.diagnostics),
        }
        if verification is not None:
            payload["verification_success"] = verification
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write('\n')
        os.replace(temporary, destination)
        return str(destination)
    except Exception as e:
        logger.warning("Failed to save pairing diagnostic: %s", e)
        return None

def setup_argparser() -> argparse.ArgumentParser:
    """Create the main argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog='vmin_judge',
        description='Vmin Judge Tool - Monitor device tests and determine verdicts',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-pair PC and device serial ports
  vmin_judge --transport hdc --device DEVICE_ID --pc-serial COM4 --device-uart /dev/ttyHW0 pair --platform kirin9030 --verify

  # Prepare explicitly, then run with or without a baseline
  vmin_judge --transport hdc --device DEVICE_ID probe --platform kirin9030 --full
  vmin_judge --transport hdc --device DEVICE_ID deploy --profile gpu_vulkan_mixed
  vmin_judge --transport hdc --device DEVICE_ID verify-deployment --profile gpu_vulkan_mixed
  vmin_judge --pc-serial COM4 run --profile gpu_vulkan_mixed --test-id TEST-001

  # List available profiles
  vmin_judge list-profiles

  # Simulate from a framed serial capture
  vmin_judge simulate --raw-serial serial.raw

  # Validate all bundled profiles and package resources
  vmin_judge validate --package
        """
    )

    # Global options
    parser.add_argument('--config-dir', help='External configuration override root')
    parser.add_argument('--output-dir', help='Writable qualification/run artifact root')
    parser.add_argument('--state-dir', help='Persistent pairing and baseline registry root')
    parser.add_argument('--transport', choices=['auto', 'adb', 'hdc'], default='auto')
    parser.add_argument('--device', help='ADB/HDC target serial')
    parser.add_argument('--adb-bin', help='Explicit ADB executable')
    parser.add_argument('--hdc-bin', help='Explicit HDC executable')
    parser.add_argument('--device-root', default='/data/local/tmp/avs')
    parser.add_argument('--pc-serial', help='PC-side UART port')
    parser.add_argument('--device-uart', help='Device UART path; saved pairing or selected platform fills it when omitted')
    parser.add_argument('--baudrate', dest='global_baudrate', type=int, default=None)
    parser.add_argument('--log-level', choices=['debug', 'info', 'warning', 'error'], default='warning')
    parser.add_argument('--json', dest='json_output', action='store_true', help='Print machine-readable command output')
    parser.add_argument(
        '--version',
        action='version',
        version='vmin_judge 2.1.0 (config=1 event=1 manifest=1 baseline=1 result=1)'
    )

    # Create subparsers
    subparsers = parser.add_subparsers(
        title='commands',
        dest='command',
        help='Available commands'
    )

    # ─────────────────────────────────────────────────────────────────
    # pair command (auto-pair PC and device serial ports)
    # ─────────────────────────────────────────────────────────────────
    pair_parser = subparsers.add_parser(
        'pair',
        help='Auto-pair PC and device serial ports',
        description='Automatically discover and pair PC serial port with device serial port'
    )
    pair_parser.add_argument('--platform', help='Platform supplying UART candidates and default baud rate')
    pair_parser.add_argument('--timeout', type=float, default=2.0, help='Pairing test timeout in seconds')
    pair_parser.add_argument('--verify', action='store_true', help='Verify the saved pair after discovery')

    # ─────────────────────────────────────────────────────────────────
    # monitor command
    # ─────────────────────────────────────────────────────────────────
    monitor_parser = subparsers.add_parser(
        'monitor',
        help='Diagnostically decode a live UART-v2 stream',
        description='Discover and decode one UART-v2 session; diagnostic only, no DUT verdict'
    )
    monitor_parser.add_argument('--save-raw', action='store_true')
    monitor_parser.add_argument('--expected-run-id')
    monitor_parser.add_argument('--schema-version', type=int, default=1)
    monitor_parser.add_argument('--timeout', type=float, default=60.0, help='Seconds without a usable event before timeout')

    # ─────────────────────────────────────────────────────────────────
    # simulate command
    # ─────────────────────────────────────────────────────────────────
    simulate_parser = subparsers.add_parser(
        'simulate',
        help='Replay stored JSONL events or a UART-v2 raw capture',
        description='Evaluate stored evidence offline without contacting a device'
    )
    simulate_input = simulate_parser.add_mutually_exclusive_group(required=True)
    simulate_input.add_argument('--events', help='Framed events.jsonl to replay')
    simulate_input.add_argument('--raw-serial', help='Deterministically discover and replay one UART-v2 session')
    simulate_parser.add_argument('--profile', help='Profile policy context')
    simulate_parser.add_argument('--baseline', help='Approved baseline ID')
    simulate_parser.add_argument('--realtime', action='store_true', help='Pace JSONL --events replay; not valid with --raw-serial')

    # ─────────────────────────────────────────────────────────────────
    # list-profiles command
    # ─────────────────────────────────────────────────────────────────
    list_parser = subparsers.add_parser(
        'list-profiles',
        help='List available workload profiles',
        description='Show all available workload profiles'
    )
    list_parser.add_argument('--target', choices=['cpu', 'gpu'])

    # ─────────────────────────────────────────────────────────────────
    # validate command
    # ─────────────────────────────────────────────────────────────────
    validate_parser = subparsers.add_parser(
        'validate',
        help='Validate configuration',
        description='Check configuration files without running tests'
    )
    validate_parser.add_argument('--profile')
    validate_parser.add_argument('--baseline')
    validate_parser.add_argument('--package', action='store_true')

    probe_parser = subparsers.add_parser('probe', help='Discover normalized device capabilities')
    probe_parser.add_argument('--platform', required=True)
    probe_parser.add_argument('--full', action='store_true')
    probe_parser.add_argument('--require', action='append', default=[])

    relay_parser = subparsers.add_parser('relay', help='Probe native UART relay ABI and runtime support')
    relay_subparsers = relay_parser.add_subparsers(dest='relay_action', required=True)
    relay_probe = relay_subparsers.add_parser('probe', help='Read device ABI and test an already deployed relay')
    relay_probe.add_argument('--platform', required=True)
    relay_probe.add_argument('--check-uart', action='store_true', help='Open/configure/drain the UART without sending payload')

    deploy_parser = subparsers.add_parser('deploy', help='Hash-verified device asset deployment')
    deploy_selection = deploy_parser.add_mutually_exclusive_group(required=True)
    deploy_selection.add_argument('--target', choices=['cpu', 'gpu', 'all'])
    deploy_selection.add_argument('--profile')
    deploy_reference = deploy_parser.add_mutually_exclusive_group()
    deploy_reference.add_argument('--baseline', help='Approved baseline whose GPU golden asset must be deployed')
    deploy_reference.add_argument('--golden', metavar='MANIFEST', help='Correctness-only golden for a pre-baseline qualification run')
    deploy_parser.add_argument('--force', action='store_true')
    deploy_parser.add_argument('--clean-stale', action='store_true', help='Remove stale assets; valid only with --target all')

    verify_parser = subparsers.add_parser('verify-deployment', help='Verify already deployed assets without changing the device')
    verify_selection = verify_parser.add_mutually_exclusive_group(required=True)
    verify_selection.add_argument('--target', choices=['cpu', 'gpu', 'all'])
    verify_selection.add_argument('--profile')
    verify_reference = verify_parser.add_mutually_exclusive_group()
    verify_reference.add_argument('--baseline')
    verify_reference.add_argument('--golden', metavar='MANIFEST')

    golden_parser = subparsers.add_parser('golden', help='Create CPU/GPU golden artifacts from qualified runs')
    golden_subparsers = golden_parser.add_subparsers(dest='golden_target', required=True)
    for target in ('cpu', 'gpu'):
        target_parser = golden_subparsers.add_parser(target)
        target_parser.add_argument('--profile', required=True)
        target_parser.add_argument('--runs', type=int, default=10, help='Exact number of qualified runs to consume or capture')
        target_parser.add_argument('--board-id', required=True)
        target_parser.add_argument('--known-good', action='store_true', required=True, help='Acknowledge that the source board is known-good')
        target_parser.add_argument(
            '--run-dir',
            action='append',
            default=[],
            metavar='[BOARD_ID=]PATH',
            help='Supply exactly --runs collected runs, or omit all values for live capture',
        )
        target_parser.add_argument('--qualification-id')
        if target == 'cpu':
            target_parser.add_argument('--accept-checksum')
        else:
            target_parser.add_argument('--readback-name', default='gpu-golden.rgba')

    calibrate_parser = subparsers.add_parser('calibrate', help='Propose CPU/GPU limits from qualified runs')
    calibrate_subparsers = calibrate_parser.add_subparsers(dest='calibration_target', required=True)
    for target in ('cpu', 'gpu'):
        target_parser = calibrate_subparsers.add_parser(target)
        target_parser.add_argument('--profile', required=True)
        target_parser.add_argument('--runs', type=int, default=30)
        target_parser.add_argument('--board-id', required=True)
        target_parser.add_argument('--temperature-range', default='35:60')
        target_parser.add_argument('--min-accepted', type=int)
        target_parser.add_argument('--policy', default='config/policies/calibration.yaml')
        target_parser.add_argument('--golden', required=True, metavar='MANIFEST', help='Golden manifest file')
        target_parser.add_argument(
            '--run-dir',
            action='append',
            default=[],
            metavar='[BOARD_ID=]PATH',
            help='One collected run; repeat exactly --runs times (BOARD_ID prefix supports multi-board cohorts)',
        )
        target_parser.add_argument('--baseline-id')

    smoke_parser = subparsers.add_parser(
        'smoke',
        help='Deprecated baseline-free alias for run',
        description='DEPRECATED: use run with a short smoke profile. Preparation remains explicit.',
    )
    smoke_parser.add_argument('--profile', required=True)
    smoke_attempts = smoke_parser.add_mutually_exclusive_group()
    smoke_attempts.add_argument('--repeat', type=int, default=1)
    smoke_attempts.add_argument('--attempt-id')
    smoke_parser.add_argument('--test-id')
    smoke_parser.add_argument('--telemetry', action='store_true')
    smoke_parser.add_argument('--pc-artifacts', choices=['result', 'full'], default='result')
    smoke_parser.add_argument('--overall-timeout', type=float, default=180.0)
    smoke_parser.add_argument('--heartbeat-timeout', type=float, default=30.0)

    baseline_parser = subparsers.add_parser('baseline', help='Manage immutable approved baselines')
    baseline_subparsers = baseline_parser.add_subparsers(dest='baseline_action', required=True)
    baseline_list = baseline_subparsers.add_parser('list')
    baseline_list.add_argument('--status', choices=['draft', 'approved', 'deprecated', 'invalid'])
    baseline_list.add_argument('--profile')
    baseline_show = baseline_subparsers.add_parser('show')
    baseline_show.add_argument('baseline_id')
    baseline_approve = baseline_subparsers.add_parser('approve')
    baseline_approve.add_argument('baseline_id')
    baseline_approve.add_argument('--approver', required=True)
    baseline_deprecate = baseline_subparsers.add_parser('deprecate')
    baseline_deprecate.add_argument('baseline_id')
    baseline_deprecate.add_argument('--reason', required=True)
    baseline_export = baseline_subparsers.add_parser('export')
    baseline_export.add_argument('baseline_id')
    baseline_export.add_argument('--output')
    baseline_import = baseline_subparsers.add_parser('import')
    baseline_import.add_argument('bundle')

    run_parser = subparsers.add_parser('run', help='Launch an already deployed profile and judge UART events')
    run_parser.add_argument('--profile', required=True)
    run_reference = run_parser.add_mutually_exclusive_group()
    run_reference.add_argument('--baseline', help='Optional approved baseline ID; omit for error-only judgement')
    run_reference.add_argument(
        '--golden',
        metavar='MANIFEST',
        help='Correctness-only reference for sustained qualification capture; does not apply performance limits',
    )
    run_attempts = run_parser.add_mutually_exclusive_group()
    run_attempts.add_argument('--repeat', type=int, default=1)
    run_attempts.add_argument('--attempt-id', help='Explicit unique attempt ID')
    run_parser.add_argument('--test-id', help='Operator test ID grouping all attempts')
    run_parser.add_argument('--telemetry', action='store_true', help='Launch the independent deployed telemetry collector alongside the workload')
    run_parser.add_argument('--pc-artifacts', choices=['result', 'full'], default='result')
    run_parser.add_argument('--overall-timeout', type=float, default=300.0)
    run_parser.add_argument('--heartbeat-timeout', type=float, default=45.0)

    telemetry_parser = subparsers.add_parser('telemetry', help='Run device-local telemetry independently')
    telemetry_subparsers = telemetry_parser.add_subparsers(dest='telemetry_action', required=True)
    telemetry_run = telemetry_subparsers.add_parser('run')
    telemetry_run.add_argument('--profile', required=True)
    telemetry_run.add_argument('--test-id', required=True)
    telemetry_run.add_argument('--attempt-id')
    telemetry_run.add_argument('--duration', type=int, default=60, help='Collection duration in seconds; 0 samples once')
    telemetry_run.add_argument('--interval', type=int, help='Sampling interval in seconds; defaults to the profile interval')

    collect_parser = subparsers.add_parser('collect', help='Pull append-only device evidence by test ID')
    collect_parser.add_argument('--test-id', required=True)
    collect_parser.add_argument('--attempt-id')
    collect_parser.add_argument('--remote-run-dir', help=argparse.SUPPRESS)
    collect_parser.add_argument('--verify-hashes', action='store_true')
    collect_parser.add_argument('--remove-remote-after-verify', action='store_true')

    report_parser = subparsers.add_parser('report', help='Regenerate reports from stored run artifacts')
    report_parser.add_argument('--run-dir', required=True)
    report_parser.add_argument('--format', default='markdown,json', metavar='FORMATS', help='Comma-separated subset of markdown,json,csv')

    return parser






def cmd_pair(args) -> int:
    """
    Handle pair command - Auto-pair PC and device serial ports.

    This command:
    1. Scans PC serial ports
    2. Scans device serial ports (via HDC/ADB)
    3. Tests each PC-device port combination
    4. Returns the best pairing result
    5. Optionally verifies the saved pairing
    """
    from src.serial_port_manager import (
        create_serial_port_manager,
        SerialPortConfig,
    )

    platform_serial = {}
    if getattr(args, 'platform', None):
        from src.cli_commands import load_platform
        platform_serial = load_platform(runtime_paths, args.platform).serial

    args.baudrate = (
        args.baudrate
        if args.baudrate is not None
        else platform_serial.get('baudrate', 9600)
    )
    logger.debug("Starting serial port pairing")

    # Create channel
    prefer_hdc = (args.transport == 'hdc')
    channel = create_channel_manager(
        prefer_hdc=prefer_hdc,
        hdc_serial=getattr(args, 'device', None),
        adb_serial=getattr(args, 'device', None),
    )

    if not channel.connect():
        logger.error("Failed to connect to device")
        return 1

    logger.debug("Connected via %s", channel.get_current_channel_type())

    # Build configuration
    config = SerialPortConfig(
        auto_discover=True,
        auto_pair=True,
        explicit_device_port=args.device_uart,
        explicit_pc_port=args.pc_serial,
        baudrate=args.baudrate,
        timeout_sec=args.timeout,
        fallback_device_ports=list(platform_serial.get('uart_candidates') or []),
    )

    # Use real implementation (scans actual hardware)
    # Set use_real_impl=False for testing without hardware
    use_real_impl = True

    try:
        manager = create_serial_port_manager(
            channel=channel,
            config=config,
            use_real_impl=use_real_impl
        )

        # Perform discovery/pairing
        result = manager.discover()
        diagnostic_path = save_pairing_diagnostic(result)

        if result.success:
            print(
                f"[OK] Pairing {result.device_port} -> {result.pc_port} "
                f"@ {args.baudrate} baud ({result.pair.latency_ms:.1f} ms)"
            )

            # Save pairing result for future use
            save_pairing_result(
                device_port=result.device_port,
                pc_port=result.pc_port,
                baudrate=args.baudrate,
                confidence=result.pair.confidence
            )

            # Optional verification
            if args.verify:
                verified = manager.verify_connection()
                verification_diagnostic = manager.get_last_diagnostic()
                if verification_diagnostic:
                    result.diagnostics.append(verification_diagnostic)
                save_pairing_diagnostic(result, verification=verified)
                if verified:
                    print("[OK] Pairing verified")
                else:
                    print("[X] Pairing verification failed")
                    return 1

            return 0
        else:
            print(
                f"[X] Pairing failed: {result.error or 'no working pair'}; "
                f"baud={args.baudrate}, attempts={result.attempts}, duration={result.duration_sec:.1f}s"
                + (f"; diagnostic={diagnostic_path}" if diagnostic_path else "")
            )
            return 1

    except Exception as e:
        logger.error(f"Pairing error: {e}")
        channel.disconnect()
        return 1
    finally:
        channel.disconnect()




def main():
    """Main entry point."""
    global runtime_paths
    parser = setup_argparser()
    args = parser.parse_args()

    runtime_paths = PathResolver.create(
        config_dir=args.config_dir,
        state_dir=args.state_dir,
        output_dir=args.output_dir,
        device_root=args.device_root,
        entrypoint=__file__,
    )

    # Normalize global/compatibility aliases without changing the caller CWD.
    args.baudrate = args.global_baudrate

    # Set logging level
    if args.log_level == 'debug':
        console_handler.setLevel(logging.DEBUG)
    elif args.log_level == 'error':
        console_handler.setLevel(logging.ERROR)
    elif args.log_level == 'info':
        console_handler.setLevel(logging.INFO)
    else:
        console_handler.setLevel(logging.WARNING)

    # Default command if none specified
    if not args.command:
        parser.print_help()
        return 0

    from src.cli_commands import (
        cmd_baseline as cmd_baseline_v2,
        cmd_calibrate as cmd_calibrate_v2,
        cmd_collect as cmd_collect_v2,
        cmd_deploy as cmd_deploy_v2,
        cmd_golden as cmd_golden_v2,
        cmd_list_profiles_v2,
        cmd_monitor_events,
        cmd_probe as cmd_probe_v2,
        cmd_relay_probe,
        cmd_report as cmd_report_v2,
        cmd_run as cmd_run_v2,
        cmd_smoke as cmd_smoke_v2,
        cmd_telemetry as cmd_telemetry_v2,
        cmd_verify_deployment as cmd_verify_deployment_v2,
        cmd_simulate as cmd_simulate_v2,
        cmd_validate_v2,
    )

    # Dispatch to target services; pair and diagnostic monitor retain legacy adapters.
    command_handlers = {
        'probe': cmd_probe_v2,
        'relay': cmd_relay_probe,
        'deploy': cmd_deploy_v2,
        'verify-deployment': cmd_verify_deployment_v2,
        'golden': cmd_golden_v2,
        'calibrate': cmd_calibrate_v2,
        'baseline': cmd_baseline_v2,
        'smoke': cmd_smoke_v2,
        'telemetry': cmd_telemetry_v2,
        'run': cmd_run_v2,
        'collect': cmd_collect_v2,
        'report': cmd_report_v2,
        'list-profiles': cmd_list_profiles_v2,
        'validate': cmd_validate_v2,
        'simulate': cmd_simulate_v2,
        'monitor': cmd_monitor_events,
        'pair': cmd_pair,
    }

    handler = command_handlers.get(args.command)
    if handler:
        return handler(args)
    else:
        parser.print_help()
        return 0


if __name__ == '__main__':
    sys.exit(main())

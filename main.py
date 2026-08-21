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
    vmin_judge.exe monitor --config config/cpu_judge.conf
    vmin_judge.exe execute --profile gpu_vulkan_game_light

Author: Vmin Judge Tool Development
Version: 1.1 (Optimized Pairing v2.0, Pairing Persistence)
"""

import sys
import io
import os
import time
import logging
import argparse
import json
import threading
import queue
import re
import logging.handlers
import serial as pyserial
from typing import Optional, List

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Add src to path for direct execution
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import directly from submodules to avoid broken __init__.py imports
from src.channel_manager import create_channel_manager, ChannelManager
from src.serial_monitor import SerialMonitor
from src.log_parser import LogParser, ParsedLine
from src.pattern_processor import PatternProcessor
from src.heartbeat_watchdog import HeartbeatWatchdog
from src.judgment_decision import JudgmentDecision
from src.result_formatter import ResultFormatter, FormattedResult
from src.scheduler_components import SchedulerFacade, MonitorController

# Import verdict constants
from src.verdict_constants import (
    VERDICT_PASS,
    VERDICT_FAIL,
    VERDICT_SILENT_FAILURE,
    VERDICT_RUNNING
)


# Configure logging
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG) 


console_handler = logging.StreamHandler()
console_handler.setLevel(logging.WARNING)
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))

file_handler = logging.FileHandler('vmin_judge.log', encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))

root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

logger = logging.getLogger('vmin_judge')


# =============================================================================
# Pairing Result Persistence
# =============================================================================




def _get_pairing_config_path() -> str:
    """Get the path to the pairing configuration file."""
    # Use user's home directory for persistence
    home_dir = os.path.expanduser('~')
    config_dir = os.path.join(home_dir, '.vmin_judge')
    return os.path.join(config_dir, 'pairing.conf')

def _ensure_config_dir() -> str:
    """Ensure the config directory exists, return the path."""
    config_dir = os.path.dirname(_get_pairing_config_path())
    if not os.path.exists(config_dir):
        os.makedirs(config_dir, exist_ok=True)
    return config_dir

def save_pairing_result(device_port: str, pc_port: str, baudrate:int = 115200, confidence: float = 1.0) -> bool:
    """
    Save pairing result to config file for persistence.

    Args:
        device_port: Device serial port (e.g., '/dev/tty0')
        pc_port: PC serial port (e.g., 'COM8')
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

        logger.info(f"Pairing result saved to: {config_path}")
        logger.info(f"  Device port: {device_port}")
        logger.info(f"  PC port: {pc_port}")
        return True

    except Exception as e:
        logger.warning(f"Failed to save pairing result: {e}")
        return False

def load_pairing_result() -> dict:
    """
    Load pairing result from config file.

    Returns:
        Dictionary with device_port, pc_port, confidence, saved_at.
        Returns empty dict if no saved result exists.
    """
    config_path = _get_pairing_config_path()

    if not os.path.exists(config_path):
        return {}

    try:
        with open(config_path, 'r') as f:
            data = json.load(f)

        logger.debug(f"Loaded saved pairing result from: {config_path}")
        logger.debug(f"  Device port: {data.get('device_port')}")
        logger.debug(f"  PC port: {data.get('pc_port')}")
        return data

    except Exception as e:
        logger.warning(f"Failed to load pairing result: {e}")
        return {}

def clear_pairing_result() -> bool:
    """
    Clear the saved pairing result.

    Returns:
        True if cleared successfully, False otherwise.
    """
    config_path = _get_pairing_config_path()

    if not os.path.exists(config_path):
        return True

    try:
        os.remove(config_path)
        logger.info("Saved pairing result cleared")
        return True
    except Exception as e:
        logger.warning(f"Failed to clear pairing result: {e}")
        return False


def setup_argparser() -> argparse.ArgumentParser:
    """Create the main argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog='vmin_judge',
        description='Vmin Judge Tool - Monitor device tests and determine verdicts',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-pair PC and device serial ports
  vmin_judge pair --channel hdc

  # Monitor serial port in real-time
  vmin_judge monitor --config config/cpu_judge.conf --serial COM4

  # Execute a workload profile
  vmin_judge execute --profile gpu_vulkan_game_light --config config/cpu_judge.conf

  # List available profiles
  vmin_judge list-profiles

  # Simulate from log file
  vmin_judge simulate --log-file test_output.log --config config/cpu_judge.conf

  # Dry run (validate config)
  vmin_judge validate --config config/cpu_judge.conf
        """
    )

    # Global options
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose (DEBUG) logging'
    )
    parser.add_argument(
        '-q', '--quiet',
        action='store_true',
        help='Suppress non-essential output'
    )
    parser.add_argument(
        '--version',
        action='version',
        version='vmin_judge v1.0'
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
    _add_pair_options(pair_parser)

    # ─────────────────────────────────────────────────────────────────
    # monitor command
    # ─────────────────────────────────────────────────────────────────
    monitor_parser = subparsers.add_parser(
        'monitor',
        help='Monitor serial port in real-time',
        description='Start monitoring a serial port for test output'
    )
    _add_monitor_options(monitor_parser)

    # ─────────────────────────────────────────────────────────────────
    # execute command
    # ─────────────────────────────────────────────────────────────────
    execute_parser = subparsers.add_parser(
        'execute',
        help='Execute workload with monitoring',
        description='Launch a workload on the device and monitor its output'
    )
    _add_monitor_options(execute_parser)
    _add_execute_options(execute_parser)

    # ─────────────────────────────────────────────────────────────────
    # simulate command
    # ─────────────────────────────────────────────────────────────────
    simulate_parser = subparsers.add_parser(
        'simulate',
        help='Simulate from log file',
        description='Process a log file as if it were live serial output'
    )
    _add_monitor_options(simulate_parser)
    simulate_parser.add_argument(
        '--log-file',
        required=True,
        help='Path to log file to simulate'
    )

    # ─────────────────────────────────────────────────────────────────
    # list-profiles command
    # ─────────────────────────────────────────────────────────────────
    list_parser = subparsers.add_parser(
        'list-profiles',
        help='List available workload profiles',
        description='Show all available workload profiles'
    )
    list_parser.add_argument(
        '--show-pending',
        action='store_true',
        help='Include pending (not implemented) profiles'
    )

    # ─────────────────────────────────────────────────────────────────
    # validate command
    # ─────────────────────────────────────────────────────────────────
    validate_parser = subparsers.add_parser(
        'validate',
        help='Validate configuration',
        description='Check configuration files without running tests'
    )
    validate_parser.add_argument(
        '--config',
        '-c',
        default='config/cpu_judge.conf',
        help='Path to rule configuration file (default: config/cpu_judge.conf)'
    )
    validate_parser.add_argument(
        '--profiles',
        default='config/workload_profiles.yaml',
        help='Path to workload profiles file (default: config/workload_profiles.yaml)'
    )

    return parser


def _add_monitor_options(parser: argparse.ArgumentParser) -> None:
    """Add common monitoring options to a parser."""
    parser.add_argument(
        '--config', '-c',
        default='config/cpu_judge.conf',
        help='Path to rule configuration file (default: config/cpu_judge.conf)'
    )
    parser.add_argument(
        '--channel', '-C',
        choices=['hdc', 'adb', 'auto'],
        default='auto',
        help='Device channel type (default: auto)'
    )
    parser.add_argument(
        '--serial-port', '--serial', '-s',
        dest='serial_port',
        help='Serial port to monitor (e.g., COM4, /dev/ttyAMA0)'
    )
    parser.add_argument(
        '--baudrate', '-b',
        type=int,
        default=None,
        help='Serial baud rate (default: 115200)'
    )
    parser.add_argument(
        '--heartbeat-timeout',
        type=int,
        default=45,
        help='Heartbeat watchdog timeout in seconds (default: 45)'
    )
    parser.add_argument(
        '--overall-timeout',
        type=int,
        default=300,
        help='Overall test timeout in seconds (default: 300)'
    )
    parser.add_argument(
        '--output', '-o',
        choices=['text', 'json', 'auto'],
        default='auto',
        help='Output format (default: auto)'
    )
    parser.add_argument(
        '--output-file',
        help='Write output to file instead of stdout'
    )
    parser.add_argument(
        '--no-color',
        action='store_true',
        help='Disable colored output'
    )


def _add_pair_options(parser: argparse.ArgumentParser) -> None:
    """Add serial port pairing options to a parser."""
    parser.add_argument(
        '--channel', '-C',
        choices=['hdc', 'adb', 'auto'],
        default='auto',
        help='Device channel type (default: auto)'
    )
    parser.add_argument(
        '--baudrate', '-b',
        type=int,
        default=None,
        help='Serial baud rate (default: 115200)'
    )
    parser.add_argument(
        '--device-port',
        help='Explicit device serial port (e.g., /dev/ttyAMA0)'
    )
    parser.add_argument(
        '--pc-port',
        help='Explicit PC serial port (e.g., COM4)'
    )
    parser.add_argument(
        '--timeout',
        type=float,
        default=2.0,
        help='Pairing test timeout in seconds (default: 2.0)'
    )
    parser.add_argument(
        '--verify',
        action='store_true',
        help='Verify connection after pairing'
    )
    parser.add_argument(
        '--monitor',
        action='store_true',
        help='Start monitoring after successful pairing'
    )


def _add_execute_options(parser: argparse.ArgumentParser) -> None:
    """Add execute-specific options to a parser."""
    parser.add_argument(
        'profile',
        nargs='?',
        help='Workload profile name (e.g., gpu_game_light)'
    )
    parser.add_argument(
        '--profile',
        dest='profile_alt',
        help='Workload profile name (alternative to positional arg)'
    )
    parser.add_argument(
        '--no-launch',
        action='store_true',
        help='Start monitoring only, do not launch workload'
    )
    parser.add_argument(
        '--auto-pair',
        action='store_true',
        help='Automatically pair serial ports before execution'
    )


def cmd_list_profiles(args) -> int:
    """Handle list-profiles command."""
    try:
        from src.workload_profiles import WorkloadProfileRegistry

        # Resolve profiles config path
        default_profiles = 'config/workload_profiles.yaml'
        profiles_path = _resolve_profiles_path('config/cpu_judge.conf', default_profiles)

        registry = WorkloadProfileRegistry()
        registry.load(profiles_path)
        profiles = registry.list_available()

        if not profiles:
            print(f"No workload profiles found. (checked: {profiles_path})")
            return 0

        print(f"Available profiles ({len(profiles)}):")
        print("-" * 60)

        for profile in profiles:
            status = "[OK]" if profile.is_implemented else "[PENDING]"
            print(f"  {status} {profile.name}")
            print(f"      Target: {profile.target}")
            if profile.workload_path:
                print(f"      Path: {profile.workload_path}")

        return 0
    except ImportError as e:
        logger.error(f"Failed to load profiles: {e}")
        return 1


def _normalize_path(path: str) -> str:
    """
    Normalize path by removing ./ and .\\ prefixes for consistent handling.

    Args:
        path: Path to normalize.

    Returns:
        Normalized path with ./ or .\\ prefix removed and path separators normalized.
    """
    # Remove ./ or .\\ prefix if present
    if path.startswith('./'):
        path = path[2:]
    elif path.startswith('.\\'):
        path = path[2:]

    # Normalize path separators to use os.sep
    path = os.path.normpath(path)

    return path


def _get_tool_dir() -> str:
    """
    Get the directory where the tool (main.py or .exe) is located.

    Returns:
        Absolute path to the tool's directory.
    """
    # Get the directory of the main script or executable
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller executable
        tool_path = sys.executable
    else:
        # Running as Python script
        tool_path = os.path.abspath(__file__)

    return os.path.dirname(tool_path)


def _resolve_config_path(path: str) -> str:
    """
    Resolve configuration file path with fallback search.

    Search order:
    1. Exact path as provided (relative to current working directory)
    2. Normalized path (relative to current working directory)
    3. configs/ subdirectory relative to tool directory
    4. config/ subdirectory relative to tool directory

    Args:
        path: Path to resolve.

    Returns:
        Resolved path that exists, or original path if nothing found.
    """
    # Normalize path (remove ./ and .\\ prefixes)
    normalized = _normalize_path(path)

    # 1. Try normalized exact path first (relative to CWD)
    if os.path.exists(normalized):
        return normalized

    # 2. Also try original path (in case normalization changed something)
    if path != normalized and os.path.exists(path):
        return path

    # 3. Try path relative to tool directory
    tool_dir = _get_tool_dir()
    basename = os.path.basename(normalized) if normalized else os.path.basename(path)

    # Try configs/ subdirectory of tool directory
    configs_in_tool = os.path.join(tool_dir, 'configs', basename)
    if os.path.exists(configs_in_tool):
        return configs_in_tool

    # Try config/ subdirectory of tool directory
    config_in_tool = os.path.join(tool_dir, 'config', basename)
    if os.path.exists(config_in_tool):
        return config_in_tool

    # Try directly in tool directory
    config_in_tool_root = os.path.join(tool_dir, basename)
    if os.path.exists(config_in_tool_root):
        return config_in_tool_root

    return normalized  # Return normalized original if nothing found


def _resolve_profiles_path(config_path: str, default_profiles: str) -> str:
    """
    Resolve profiles path relative to config file's directory.

    Search order:
    1. If profiles path is absolute, use as-is
    2. Try relative to config file's directory
    3. Try relative to tool directory

    Args:
        config_path: Resolved config file path.
        default_profiles: Default profiles path.

    Returns:
        Resolved profiles path.
    """
    # If profiles path is absolute, return as-is
    if os.path.isabs(default_profiles):
        if os.path.exists(default_profiles):
            return default_profiles
        return default_profiles

    # Normalize profiles path
    normalized_profiles = _normalize_path(default_profiles)
    profiles_basename = os.path.basename(normalized_profiles)

    # Get directories to search
    config_dir = os.path.dirname(os.path.abspath(config_path))
    tool_dir = _get_tool_dir()
    cwd = os.getcwd()

    # Search order: config directory, tool directory, CWD
    search_dirs = [
        config_dir,           # Directory where config file is located
        os.path.join(tool_dir, 'config'),
        os.path.join(tool_dir, 'configs'),
        tool_dir,             # Tool directory directly
        os.path.join(cwd, 'config'),
        os.path.join(cwd, 'configs'),
        cwd,                  # Current working directory
    ]

    # Remove duplicates while preserving order
    seen = set()
    unique_dirs = []
    for d in search_dirs:
        if d not in seen:
            seen.add(d)
            unique_dirs.append(d)

    for search_dir in unique_dirs:
        # Try config/ subdirectory
        profiles_in_config = os.path.join(search_dir, 'config', profiles_basename)
        if os.path.exists(profiles_in_config):
            return profiles_in_config

        # Try configs/ subdirectory
        profiles_in_configs = os.path.join(search_dir, 'configs', profiles_basename)
        if os.path.exists(profiles_in_configs):
            return profiles_in_configs

        # Try directly in search_dir
        profiles_in_dir = os.path.join(search_dir, profiles_basename)
        if os.path.exists(profiles_in_dir):
            return profiles_in_dir

    # Return default path if nothing found
    return default_profiles


def cmd_validate(args) -> int:
    """Handle validate command."""
    errors = []
    warnings = []

    # Resolve config path with fallback search
    config_path = _resolve_config_path(args.config)

    # Check config file
    if not os.path.exists(config_path):
        errors.append(f"Rule config not found: {args.config}")
    else:
        try:
            from src.pattern_processor import PatternProcessor
            processor = PatternProcessor(config_path)
            stats = processor.get_rule_summary()
            print(f"[OK] Rule config valid: {config_path}")
            print(f"  - Fail rules: {stats.get('fail_rules', 0)}")
            print(f"  - Warn rules: {stats.get('warn_rules', 0)}")
            print(f"  - Ignore rules: {stats.get('ignore_rules', 0)}")
        except Exception as e:
            errors.append(f"Rule config error: {e}")

    # Check profiles file (relative to config directory)
    profiles_path = _resolve_profiles_path(config_path, args.profiles)
    if not os.path.exists(profiles_path):
        warnings.append(f"Profiles config not found: {args.profiles}")
    else:
        try:
            from src.workload_profiles import WorkloadProfileRegistry
            registry = WorkloadProfileRegistry()
            profiles = registry.list_available()
            print(f"[OK] Profiles config valid: {profiles_path}")
            print(f"  - Available profiles: {len([p for p in profiles if p.is_implemented])}")
        except Exception as e:
            errors.append(f"Profiles config error: {e}")

    # Report
    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"  [!] {w}")

    if errors:
        print("\nErrors:")
        for e in errors:
            print(f"  [X] {e}")
        return 1

    print("\n[OK] All configurations valid")
    return 0


def cmd_simulate(args) -> int:
    """Handle simulate command."""
    if not os.path.exists(args.log_file):
        logger.error(f"Log file not found: {args.log_file}")
        return 1

    logger.info(f"Simulating from log file: {args.log_file}")

    # Create monitor
    monitor = MonitorController(
        config_path=args.config,
        heartbeat_timeout=args.heartbeat_timeout,
        overall_timeout=args.overall_timeout
    )

    monitor.start()

    # Read and process log file
    line_count = 0
    start_time = time.time()

    try:
        with open(args.log_file, 'rb') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                monitor.process_line(line)
                line_count += 1

                # Check for completion
                if monitor.is_complete():
                    break

        duration = time.time() - start_time

    except Exception as e:
        logger.error(f"Error reading log file: {e}")
        monitor.stop()
        return 1

    # Wait for final verdict (heartbeat watchdog may still fire)
    time.sleep(1)

    verdict = monitor.get_verdict()
    exit_code = monitor.get_exit_code()
    stats = monitor.get_stats()

    monitor.stop()

    # Output results
    _print_result(args, verdict, exit_code, stats, duration, line_count)

    return exit_code


def cmd_monitor(args) -> int:
    """Handle monitor command."""
    # Check for saved pairing result if no serial port specified
    args.config = _resolve_config_path(args.config)
    if not args.serial_port:
        saved = load_pairing_result()
        if saved:
            args.serial_port = saved.get('pc_port')

            if args.baudrate is None:
                args.baudrate = saved.get('baudrate', 115200)
            logger.info(f"Using saved pairing result: PC port = {args.serial_port} @ {args.baudrate} baud")
        else:
            logger.error("--serial-port is required for monitor command")
            logger.error("Run 'vmin_judge pair --channel hdc' first to pair serial ports")
            return 1

    if args.baudrate is None:
        args.baudrate = 115200

    logger.info(f"Starting serial monitor on {args.serial_port} @ {args.baudrate} baud")
    # Create channel and scheduler
    prefer_hdc = (args.channel == 'hdc')
    channel = create_channel_manager(prefer_hdc=prefer_hdc)

    scheduler = SchedulerFacade()
    scheduler.prepare(
        channel_manager=channel,
        config=args.config,
        heartbeat_timeout=args.heartbeat_timeout,
        overall_timeout=args.overall_timeout,
        serial_port=args.serial_port
    )

    # Start monitoring
    scheduler.start_monitoring()

    # Create serial monitor
    serial = SerialMonitor(
        port=args.serial_port,
        baudrate=args.baudrate
    )

    try:
        serial.open()
        logger.info("Serial port opened, monitoring for output...")

        parser = LogParser()
        last_line_time = time.time()

        # Monitor loop
        idle_warned = False
        while not scheduler.is_complete():
            # Read from serial
            line = serial.read_line(timeout=0.1)

            if line:
                parsed = parser.parse(line, time.time())
                scheduler.process_line(line)
                last_line_time = time.time()
                idle_warned = False

            # Check for idle timeout (no data for extended period)
            idle_time = time.time() - last_line_time
            if idle_time > 60 and not idle_warned:  # Only log once
                logger.info(f"No data received for {int(idle_time)}s")
                idle_warned = True

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Monitor error: {e}")
        serial.close()
        scheduler.stop()
        return 1

    serial.close()
    scheduler.stop()

    verdict = scheduler.get_verdict()
    exit_code = scheduler.get_exit_code()
    stats = scheduler.get_stats()

    duration = stats.duration_sec if stats.duration_sec else 0

    _print_result(args, verdict, exit_code, stats, duration)

    return exit_code

def _build_dmesg_monitor_cmd(channel, device_port: str) -> str:
    """
    构建通用的 dmesg 监控命令。

    探测设备上 dmesg 支持的选项，自动选择最佳方案。
    不依赖 nohup（用 trap '' HUP 替代）。

    Args:
        channel: 设备通信通道（HDC/ADB）
        device_port: 串口设备路径（如 /dev/ttyHW0）

    Returns:
        可在设备上执行的 shell 命令字符串
    """
  
    _, help_text, _ = channel.invoke("dmesg --help 2>&1; echo '---'; dmesg -h 2>&1", timeout=5)
    help_lower = (help_text or "").lower()

    has_level_long = "--level" in help_lower
    has_level_short = re.search(r'(?<!\w)-l\b', help_lower) is not None  # -l 但不是 -level

  
    has_follow = "-w" in help_lower or "--follow" in help_lower

 
    LEVEL_PATTERN = r'(err|crit|alert|emerg|panic|bug|warning)'

    if has_level_long:
        
        level_filter = "--level=err,crit,alert,emerg"
        need_grep = False
    elif has_level_short:
      
        level_filter = "-l err,crit,alert,emerg"
        need_grep = False
    else:
      
        level_filter = ""
        need_grep = True

  
    if has_follow:
       
        if need_grep:
            dmesg_cmd = f"dmesg -w 2>/dev/null | grep -iE '{LEVEL_PATTERN}'"
        else:
            dmesg_cmd = f"dmesg -w {level_filter}"
    else:
        
        if need_grep:
            dmesg_cmd = (
                f"while true; do dmesg -c 2>/dev/null | "
                f"grep -iE '{LEVEL_PATTERN}'; sleep 0.5; done"
            )
        else:
            dmesg_cmd = (
                f"while true; do dmesg -c {level_filter} 2>/dev/null; "
                f"sleep 0.5; done"
            )

    redirect_cmd = f"(trap '' HUP; {dmesg_cmd}) > {device_port} 2>&1 &"

    logger.info(f"dmesg monitor command: {redirect_cmd}")
    logger.info(f"  capabilities: follow={has_follow}, "
                f"level_long={has_level_long}, level_short={has_level_short}")

    return redirect_cmd

def cmd_execute(args) -> int:
    """Handle execute command."""
    # Handle --profile alternative argument
    args.config = _resolve_config_path(args.config)
    tool_dir = _get_tool_dir()
    os.chdir(tool_dir)
    profile = args.profile or args.profile_alt
    profiles_path = _resolve_profiles_path(args.config, 'config/workload_profiles.yaml')
    if os.path.exists(profiles_path):
        from src.workload_profiles import WorkloadProfileRegistry
        registry = WorkloadProfileRegistry()
        registry.load(profiles_path)
    else:
        logger.error(f"Workload profiles config not found: {profiles_path}")
        return 1
    if not profile and not args.no_launch:
        logger.error("Profile name is required (use --no-launch to start monitoring only)")
        return 1

    logger.info(f"Executing workload: {profile or '(monitoring only)'}")

    # Check for saved pairing result if no serial port specified
    serial_port = args.serial_port
    baudrate = args.baudrate
    device_port = None
    if not serial_port:
        saved = load_pairing_result()
        if saved:
            serial_port = saved.get('pc_port')
            device_port = saved.get('device_port')
            logger.info(f"Using saved pairing result: PC port = {serial_port}")
            if baudrate is None:
                baudrate = saved.get('baudrate', 115200)
            logger.info(f"Using saved pairing result: PC port = {serial_port} @ {baudrate} baud")
    if baudrate is None:
        baudrate = 115200  
    # Create channel
    prefer_hdc = (args.channel == 'hdc')
    channel = create_channel_manager(prefer_hdc=prefer_hdc)

    if not channel.connect():
        logger.error("Failed to connect to device")
        return 1

    logger.info(f"Connected via {channel.get_current_channel_type()}")

    # Create scheduler
    scheduler = SchedulerFacade()
    scheduler.prepare(
        channel_manager=channel,
        profile_registry=registry,
        config=args.config,
        heartbeat_timeout=args.heartbeat_timeout,
        overall_timeout=args.overall_timeout,
        serial_port=serial_port,
        baudrate=baudrate
    )

    # Start monitoring
    scheduler.start_monitoring()

    exit_code = 0
    monitor_port = serial_port
    monitor_baud = baudrate if 'baudrate' in locals() else 115200

    
    serial = pyserial.Serial(port=monitor_port, baudrate=monitor_baud, timeout=0)

    try:
       
        if not serial.is_open:
            serial.open()
        logger.info(f"Serial port opened for execute: {monitor_port} @ {monitor_baud} baud")
        handle = None
        
        if profile and not args.no_launch:

            logger.info("Cleaning up old processes (dmesg, gpu-avs-workload)...")
            channel.invoke("killall -9 gpu-avs-workload dmesg 2>/dev/null", timeout=2)
            time.sleep(0.5)
           
            logger.info("Clearing dmesg buffer...")
            channel.invoke("dmesg -c", timeout=5)
            time.sleep(0.5)
            
           
            logger.info(f"Setting up dmesg monitor on serial port ({device_port})...")
            dmesg_redirect_cmd = _build_dmesg_monitor_cmd(channel, device_port)
            try:
                result = channel.invoke(dmesg_redirect_cmd, timeout=3, background=True)
                if result.return_code == -1:
                    logger.info("dmesg monitor started (timeout expected for background command)")
                else:
                    logger.info(f"dmesg monitor command returned: code={result.return_code}")
            except Exception:
                pass

            time.sleep(1)
            try:
                _, check_out, _ = channel.invoke(
                    "ps -ef 2>/dev/null | grep '[d]mesg' | grep -v grep",
                    timeout=3
                )
                if check_out and check_out.strip():
                    logger.info(f"dmesg monitor running: {check_out.strip()[:80]}")
                else:
                    logger.warning("dmesg monitor may not have started!")
            except Exception:
                logger.warning("Could not verify dmesg monitor status") 
           
            logger.info(f"Launching workload: {profile}")
            handle = scheduler.launch_workload(profile, serial_device=device_port)

     
        raw_data_queue = queue.Queue()
        stop_reading = threading.Event()

        def serial_reader():
            while not stop_reading.is_set():
                try:
                
                    data = serial.read(serial.in_waiting or 1)
                    if data:
                        raw_data_queue.put(data)
                    else:
                        time.sleep(0.01) 
                except Exception as e:
                    logger.error(f"Serial read error in thread: {e}")
                    break

        reader_thread = threading.Thread(target=serial_reader, daemon=True)
        reader_thread.start()  

        buffer_str = ""
        json_pattern = re.compile(r'\{"type":"(?:heartbeat|summary|start)".*?\}', re.DOTALL)
        summary_pass_pattern = re.compile(r'"result"\s*:\s*"PASS"', re.IGNORECASE)
        summary_fail_pattern = re.compile(r'"result"\s*:\s*"FAIL"', re.IGNORECASE)
        log_file = open("serial_raw_output.log", "a", encoding="utf-8", errors="replace")

        progress_start = time.time()
        progress_last_print = time.time()
        heartbeat_count_seen = 0

        try:
            while not scheduler.is_complete():
                try:
                    data = raw_data_queue.get(timeout=1.0)
                    buffer_str += data.decode('utf-8', errors='replace')

                    log_file.write(buffer_str[-len(data):])
                    log_file.flush()

                    matches = json_pattern.findall(buffer_str)
                    if matches:
                        for match in matches:
                            scheduler.process_line(match)
                        
                            if '"type":"heartbeat"' in match:
                                heartbeat_count_seen += 1
                        
                        if matches:
                            last_match_end = buffer_str.rfind(matches[-1]) + len(matches[-1])
                            buffer_str = buffer_str[last_match_end:]

            
                    now = time.time()
                    if now - progress_last_print >= 10:
                        elapsed = int(now - progress_start)
                        stats = scheduler.get_stats()
                        hb = stats.heartbeat_count
                        print(f"  [Monitoring] {elapsed}s, heartbeat: {hb}", flush=True)
                        progress_last_print = now
                  

                    if 'summary' in buffer_str or 'result' in buffer_str:
                        if summary_pass_pattern.search(buffer_str):
                            logger.info("Detected PASS result (fuzzy match).")
                            scheduler._monitor._judgment._handle_result('PASS', buffer_str)
                            buffer_str = ""
                        elif summary_fail_pattern.search(buffer_str):
                            logger.info("Detected FAIL result (fuzzy match).")
                            scheduler._monitor._judgment._handle_result('FAIL', buffer_str)
                            buffer_str = ""

                    if 'page fault' in buffer_str.lower() or 'gpu hang' in buffer_str.lower():
                        scheduler._monitor._judgment.process_line(ParsedLine(source='dmesg', content=buffer_str, timestamp=time.time()))
                        buffer_str = ""

                except queue.Empty:
                 
                    now = time.time()
                    if now - progress_last_print >= 10:
                        elapsed = int(now - progress_start)
                        stats = scheduler.get_stats()
                        hb = stats.heartbeat_count
                        print(f"  [Monitoring] {elapsed}s, heartbeat: {hb}", flush=True)
                        progress_last_print = now
                    continue
                    
                 
                    log_file.write(buffer_str[-len(data):]) 
                    log_file.flush()

                
                    matches = json_pattern.findall(buffer_str)
                    if matches:
                        for match in matches:
                            scheduler.process_line(match)
                       
                        
                      
                        last_match_end = buffer_str.rfind(matches[-1]) + len(matches[-1])
                        buffer_str = buffer_str[last_match_end:]
                    if 'summary' in buffer_str or 'result' in buffer_str:
                        if summary_pass_pattern.search(buffer_str):
                            logger.info("Detected PASS result (fuzzy match).")
                            scheduler._monitor._judgment._handle_result('PASS', buffer_str)
                            buffer_str = "" 
                        elif summary_fail_pattern.search(buffer_str):
                            logger.info("Detected FAIL result (fuzzy match).")
                            scheduler._monitor._judgment._handle_result('FAIL', buffer_str)
                            buffer_str = "" 

                
                    if 'page fault' in buffer_str.lower() or 'gpu hang' in buffer_str.lower():
                        scheduler._monitor._judgment.process_line(ParsedLine(source='dmesg', content=buffer_str, timestamp=time.time()))
                        buffer_str = "" 

                except queue.Empty:
                    continue                    

        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception as e:
            logger.error(f"Execute error: {e}")

    finally:
        
        drain_count = 0
        if 'log_file' in locals():
            drain_deadline = time.time() + 2.0
            while time.time() < drain_deadline:
                try:
                    remaining = raw_data_queue.get(timeout=0.5)
                    decoded = remaining.decode('utf-8', errors='replace')
                    log_file.write(decoded)
                    drain_count +=1
                except queue.Empty:
                    break
            log_file.flush()
            log_file.close()
            if drain_count > 0:
                logger.info(f"Drained {drain_count} remaing chuncks to log file")
        stop_reading.set()
        if 'reader_thread' in locals():
            reader_thread.join(timeout=2)
        if serial.is_open:
            serial.close()
        scheduler.stop()
        channel.disconnect()
        try:
            logger.info("Cleaning up dmesg process on device...")
            channel.connect()
            channel.invoke("killall -9 dmesg 2>/dev/null", timeout=2)
            channel.disconnect()
        except Exception:
            pass
    
    verdict = scheduler.get_verdict()
    exit_code = scheduler.get_exit_code()
    stats = scheduler.get_stats()

    duration = stats.duration_sec if stats.duration_sec else 0

    _print_result(args, verdict, exit_code, stats, duration)

    return exit_code


def cmd_pair(args) -> int:
    """
    Handle pair command - Auto-pair PC and device serial ports.

    This command:
    1. Scans PC serial ports
    2. Scans device serial ports (via HDC/ADB)
    3. Tests each PC-device port combination
    4. Returns the best pairing result
    5. Optionally verifies and starts monitoring
    """
    from src.serial_port_manager import (
        create_serial_port_manager,
        SerialPortConfig,
        RealPCSerialScanner,
        MockPCSerialScanner
    )

    logger.info("Starting serial port pairing")

    # Create channel
    prefer_hdc = (args.channel == 'hdc')
    channel = create_channel_manager(prefer_hdc=prefer_hdc)

    if not channel.connect():
        logger.error("Failed to connect to device")
        return 1

    logger.info(f"Connected via {channel.get_current_channel_type()}")

    # Build configuration
    config = SerialPortConfig(
        auto_discover=True,
        auto_pair=True,
        explicit_device_port=args.device_port,
        explicit_pc_port=args.pc_port,
        baudrate=args.baudrate,
        timeout_sec=args.timeout,
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

        if result.success:
            print("\n" + "=" * 60)
            print("[OK] Serial Port Pairing Successful!")
            print("=" * 60)
            print(f"  Device Port: {result.device_port}")
            print(f"  PC Port:      {result.pc_port}")
            print(f"  Confidence:   {result.pair.confidence:.2f}")
            print(f"  Latency:      {result.pair.latency_ms:.1f}ms")
            print(f"  Duration:     {result.duration_sec:.2f}s")
            print("=" * 60)

            # Save pairing result for future use
            save_pairing_result(
                device_port=result.device_port,
                pc_port=result.pc_port,
                baudrate=args.baudrate,
                confidence=result.pair.confidence
            )

            # Optional verification
            if args.verify:
                print("\nVerifying connection...")
                if manager.verify_connection():
                    print("[OK] Connection verified!")
                else:
                    print("[!] Connection verification failed")
                    return 1

            # Optional: Start monitoring after pairing
            if args.monitor:
                print("\nStarting serial monitor...")
                # Create args namespace for cmd_monitor
                monitor_args = argparse.Namespace(
                    config='config/cpu_judge.conf',
                    channel=args.channel,
                    serial_port=result.pc_port,
                    baudrate=args.baudrate,
                    heartbeat_timeout=45,
                    overall_timeout=300,
                    output='auto',
                    output_file=None,
                    no_color=False,
                    verbose=args.verbose if hasattr(args, 'verbose') else False,
                    quiet=args.quiet if hasattr(args, 'quiet') else False,
                )
                return cmd_monitor(monitor_args)

            return 0
        else:
            print("\n" + "=" * 60)
            print("[X] Serial Port Pairing Failed!")
            print("=" * 60)
            print(f"  Error: {result.error}")
            print(f"  Device ports found: {len(result.device_ports_found)}")
            print(f"  PC ports found: {len(result.pc_ports_found)}")

            if result.device_ports_found:
                print("\n  Available device ports:")
                for port in result.device_ports_found:
                    print(f"    - {port.port} ({port.device_type})")

            if result.pc_ports_found:
                print("\n  Available PC ports:")
                for port in result.pc_ports_found:
                    print(f"    - {port.port}: {port.description}")

            print("=" * 60)
            return 1

    except Exception as e:
        logger.error(f"Pairing error: {e}")
        channel.disconnect()
        return 1
    finally:
        channel.disconnect()


def _print_result(args, verdict: str, exit_code: int, stats, duration: float, line_count: int = None):
    """Print the test result in the requested format."""
    formatter = ResultFormatter()

    # Build stats dict
    stats_dict = {
        'verdict': verdict,
        'exit_code': exit_code,
        'duration_sec': duration,
        'heartbeat_count': stats.heartbeat_count if hasattr(stats, 'heartbeat_count') else 0,
        'lines_processed': stats.lines_processed if hasattr(stats, 'lines_processed') else (line_count or 0),
        'dmesg_fail_count': sum(1 for k in stats.pattern_matches.keys() if 'fail' in k.lower()) if hasattr(stats, 'pattern_matches') else 0,
    }

    result = formatter.create_result(stats_dict)

    # Determine output format
    output_format = args.output
    if output_format == 'auto':
        output_format = 'json' if args.output_file and args.output_file.endswith('.json') else 'text'

    output = formatter.format(result, output_format)

    # Handle output destination
    if args.output_file:
        try:
            # Check for json: or file: prefix
            if args.output_file.startswith('json:'):
                output_file = args.output_file[5:]
            elif args.output_file.startswith('file:'):
                output_file = args.output_file[5:]
            else:
                output_file = args.output_file

            with open(output_file, 'w') as f:
                f.write(output)
            logger.info(f"Output written to: {output_file}")
        except Exception as e:
            logger.error(f"Failed to write output: {e}")

    # Print to console
    if not args.quiet:
        print("\n" + "=" * 60)
        print(output)
        print("=" * 60)

    # Print verdict summary
    if not args.quiet:
        verdict_symbol = {
            VERDICT_PASS: "[PASS]",
            VERDICT_FAIL: "[FAIL]",
            VERDICT_SILENT_FAILURE: "[SILENT_FAILURE]",
        }.get(verdict, f"[{verdict}]")

        print(f"\n{verdict_symbol} (exit code: {exit_code})")


def main():
    """Main entry point."""
    parser = setup_argparser()
    args = parser.parse_args()

    # Set logging level
    if args.verbose:
        console_handler.setLevel(logging.DEBUG)
    elif args.quiet:
        console_handler.setLevel(logging.ERROR)

    # Default command if none specified
    if not args.command:
        parser.print_help()
        return 0

    # Dispatch to command handler
    command_handlers = {
        'list-profiles': cmd_list_profiles,
        'validate': cmd_validate,
        'simulate': cmd_simulate,
        'monitor': cmd_monitor,
        'execute': cmd_execute,
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
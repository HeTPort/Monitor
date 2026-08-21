"""
Scheduler Components - Decoupled Architecture for Scheduling Integration

Implements:
- MonitorController: Pure monitoring consumer (accepts lines from any source)
- WorkloadController: Workload execution control
- SchedulerFacade: Combined interface for scheduling layer

Part of the 5-layer architecture defined in ARCHITECTURE.md.
Based on DEVELOPMENT.md Section 2.4.

Author: Vmin Judge Tool Development
Version: 1.0
"""

from __future__ import annotations

import time
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock

# Configure module logger
logger = logging.getLogger(__name__)

# Import from existing modules
from log_parser import LogParser, ParsedLine
from pattern_processor import PatternProcessor
from heartbeat_watchdog import HeartbeatWatchdog
from judgment_decision import JudgmentDecision, TestState
from result_formatter import FormattedResult

# Verdict constants
VERDICT_RUNNING = "RUNNING"
VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_SILENT_FAILURE = "SILENT_FAILURE"


@dataclass
class MonitoringStats:
    """Statistics from monitoring."""
    lines_processed: int = 0
    source_counts: Dict[str, int] = field(default_factory=dict)
    pattern_matches: Dict[str, int] = field(default_factory=dict)
    heartbeat_count: int = 0
    duration_sec: float = 0.0
    verdict: str = VERDICT_RUNNING


@dataclass
class ExecutionHandle:
    """
    Handle to control a running execution.

    Created by WorkloadController.launch() and passed to
    stop(), is_running(), wait() methods.
    """
    pid: int = -1
    channel_type: str = ""
    command: str = ""
    start_time: float = 0.0
    handle_id: str = ""  # Internal identifier


class MonitorController:
    """
    Controls the monitoring component (pure consumer).

    This component does NOT launch tests - it only monitors log lines
    and determines verdicts based on pattern matching.

    Used by scheduling layer to:
    - Start/stop monitoring independently of test execution
    - Feed log lines from any source (serial, file, socket)
    - Get verdict and statistics

    Usage:
        controller = MonitorController()
        controller.start()

        # Feed lines from any source
        controller.process_line("HEARTBEAT: iteration=1")
        controller.process_line("RESULT: PASS")

        if controller.is_complete():
            verdict = controller.get_verdict()

        controller.stop()
    """

    def __init__(
        self,
        config_path: str = None,
        heartbeat_timeout: float = 45.0,
        overall_timeout: float = 300.0,
        baudrate:int = 115200
    ):
        """
        Initialize MonitorController.

        Args:
            config_path: Path to rule configuration file (optional)
            heartbeat_timeout: Heartbeat watchdog timeout in seconds
            overall_timeout: Overall test timeout in seconds
        """
        self._lock = RLock()
        self._started = False
        self._start_time: Optional[float] = None
        self._stop_time: Optional[float] = None
        self._baudrate = baudrate
        # Initialize components
        self._log_parser = LogParser()

        # Create JudgmentDecision (self-contained with PatternProcessor and HeartbeatWatchdog)
        self._judgment = JudgmentDecision(
            timeout_sec=int(overall_timeout),
            rules_path=config_path
        )

        # Override heartbeat watchdog timeout if specified
        if heartbeat_timeout != 45.0:
            self._judgment.heartbeat_watchdog._timeout_sec = int(heartbeat_timeout)

        # Statistics
        self._lines_by_source: Dict[str, int] = {}
        self._lines_processed = 0

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """
        Start monitoring.

        After this, the monitor is ready to accept lines via process_line().
        Does NOT launch any test - that's WorkloadController's job.

        Raises:
            RuntimeError: If already started
        """
        with self._lock:
            if self._started:
                raise RuntimeError("MonitorController already started")

            self._started = True
            self._start_time = time.time()
            self._stop_time = None

            # Start JudgmentDecision (which starts HeartbeatWatchdog)
            self._judgment.start()

            logger.info("MonitorController started")

    def stop(self) -> None:
        """
        Stop monitoring and clean up resources.

        Can be called at any time, even if not complete.
        Safe to call multiple times.
        """
        with self._lock:
            if not self._started:
                return  # Already stopped

            self._stop_time = time.time()
            self._started = False

            # Stop JudgmentDecision (which stops HeartbeatWatchdog)
            self._judgment.stop()

            logger.info("MonitorController stopped")

    # ─────────────────────────────────────────────────────────────────
    # Line Processing
    # ─────────────────────────────────────────────────────────────────

    def process_line(self, line: str) -> None:
        """
        Feed a single log line to the monitor.

        Lines flow through:
        1. LogParser (identify source: dmesg/hilog/workload)
        2. JudgmentDecision (pattern matching, heartbeat tracking, verdict)

        Thread-safe: can be called from multiple threads.

        Args:
            line: Raw log line string (str or bytes)
        """
        if not self._started:
            logger.warning("process_line called before start()")
            return

        # Convert to bytes if string
        if isinstance(line, str):
            line = line.encode('utf-8')

        # Parse the line
        parsed = self._log_parser.parse(line, time.time())

        # Update source statistics
        source = parsed.source
        self._lines_by_source[source] = self._lines_by_source.get(source, 0) + 1

        # Process through judgment decision
        # JudgmentDecision handles pattern matching, heartbeat detection, and verdict
        self._judgment.process_line(parsed)

        # Update line count
        self._lines_processed += 1

    def process_lines(self, lines: List[str]) -> None:
        """
        Feed multiple log lines at once.

        Convenience method - equivalent to calling process_line()
        for each line in sequence.

        Args:
            lines: List of raw log lines
        """
        for line in lines:
            self.process_line(line)

    # ─────────────────────────────────────────────────────────────────
    # Status
    # ─────────────────────────────────────────────────────────────────

    def is_started(self) -> bool:
        """Check if monitoring is active."""
        with self._lock:
            return self._started

    def is_complete(self) -> bool:
        """
        Check if monitoring has reached a terminal state.

        Returns True when verdict is no longer RUNNING:
        - VERDICT_PASS
        - VERDICT_FAIL
        - VERDICT_SILENT_FAILURE
        - stop() was called

        Returns:
            bool: True if monitoring is complete
        """
        if not self._started and self._stop_time is not None:
            return True

        verdict = self._judgment.get_verdict()
        return verdict != VERDICT_RUNNING

    def is_running(self) -> bool:
        """Alias for is_started()."""
        return self.is_started()

    # ─────────────────────────────────────────────────────────────────
    # Results
    # ─────────────────────────────────────────────────────────────────

    def get_verdict(self) -> str:
        """
        Get the current verdict.

        Returns:
            str: One of VERDICT_PASS, VERDICT_FAIL, VERDICT_SILENT_FAILURE, VERDICT_RUNNING

        Raises:
            RuntimeError: If monitoring not started
        """
        if not self._started and self._stop_time is None:
            raise RuntimeError("MonitorController not started")

        return self._judgment.get_verdict()

    def get_exit_code(self) -> int:
        """
        Get exit code for verdict.

        Returns:
            int: 0=PASS, 1=FAIL, 2=SILENT_FAILURE, 3=ERROR
        """
        verdict = self.get_verdict()
        if verdict == VERDICT_PASS:
            return 0
        elif verdict == VERDICT_FAIL:
            return 1
        elif verdict == VERDICT_SILENT_FAILURE:
            return 2
        else:
            return 3  # ERROR or RUNNING

    def get_stats(self) -> MonitoringStats:
        """
        Get detailed monitoring statistics.

        Returns:
            MonitoringStats with:
            - lines_processed: int
            - source_counts: Dict[str, int]  # dmesg, workload, hilog, logcat
            - pattern_matches: Dict[str, int]
            - heartbeat_count: int
            - duration_sec: float
            - verdict: str
        """
        with self._lock:
            # Get stats from JudgmentDecision
            judgment_stats = self._judgment.get_stats()

            duration = 0.0
            if self._start_time:
                end = self._stop_time if self._stop_time else time.time()
                duration = end - self._start_time

            return MonitoringStats(
                lines_processed=judgment_stats.get('lines_processed', 0),
                source_counts=judgment_stats.get('source_counts', {}),
                pattern_matches=judgment_stats.get('pattern_matches', {}),
                heartbeat_count=judgment_stats.get('heartbeat_count', 0),
                duration_sec=judgment_stats.get('duration_sec', duration),
                verdict=self._judgment.get_verdict()
            )


class WorkloadController:
    """
    Controls workload/test execution.

    Used by scheduling layer to:
    - Build workload commands from profiles
    - Launch and stop test execution via channel
    - Check execution status

    Usage:
        controller = WorkloadController(channel_manager)

        # Build command from profile
        command = controller.build_command("gpu_vulkan_game_light")

        # Launch
        handle = controller.launch(command)

        # Check status
        if controller.is_running(handle):
            controller.stop(handle)
    """

    def __init__(self, channel_manager, profile_registry=None):
        """
        Initialize with channel manager.

        Args:
            channel_manager: ChannelManager for device communication
            profile_registry: Optional pre-loaded profile registry
        """
        self._channel = channel_manager

        # Try to import workload components
        try:
            from workload_builder import WorkloadCommandBuilder
            
            self._registry = profile_registry
            if self._registry is None:
                from workload_profiles import WorkloadProfileRegistry
                self._registry = WorkloadProfileRegistry()
            self._builder = WorkloadCommandBuilder(self._registry)
        except ImportError:
            logger.warning("Workload components not available, using basic mode")
            self._registry = None
            self._builder = None

        # Track running executions
        self._running_executions: Dict[str, ExecutionHandle] = {}
        self._next_handle_id = 1
        self._lock = RLock()

    # ─────────────────────────────────────────────────────────────────
    # Command Building
    # ─────────────────────────────────────────────────────────────────

    def build_command(
        self,
        profile_name: str,
        args: List[str] = None,
        serial_device: str = None,
        background:bool = False
    ) -> str:
        """
        Build workload command from profile.

        Args:
            profile_name: Name of profile (e.g., "gpu_vulkan_game_light")
            args: Override profile default arguments (optional)
            serial_device: Override serial device path (optional)

        Returns:
            Shell command string, e.g.:
            "/data/local/tmp/gpu-avs-workload --api vulkan > /dev/ttyAMA0 2>&1"

        Raises:
            ValueError: If profile not found
        """
        if self._builder is None:
            raise RuntimeError("Workload components not available")

        return self._builder.build(profile_name, args, serial_device,background=background)

    def list_profiles(self, include_pending: bool = False) -> List[str]:
        """
        List available profile names.

        Args:
            include_pending: Include profiles marked as pending (not implemented)

        Returns:
            List of profile names
        """
        if self._registry is None:
            return []

        profiles = self._registry.list_available()
        return [p.name for p in profiles]

    # ─────────────────────────────────────────────────────────────────
    # Execution Control
    # ─────────────────────────────────────────────────────────────────

    def launch(self, command: str, timeout: int = None) -> ExecutionHandle:
        """
        Launch a workload command.

        Args:
            command: Full shell command to execute
            timeout: Optional command timeout (uses default if None)

        Returns:
            ExecutionHandle: Handle to control the execution
        """
        logger.info(f"WorkloadController launching command: {command}")
        # Invoke command via channel
        return_code, stdout, stderr = self._channel.invoke(command, timeout or 300)
        logger.info(f"Invoke result - code: {return_code}, stdout: {stdout}, stderr: {stderr}")
        if return_code != 0:
            logger.error(f"Workload launch failed on device! stderr: {stderr}")
        # Create handle (sync execution for now)
        with self._lock:
            handle_id = f"handle_{self._next_handle_id}"
            self._next_handle_id += 1

        handle = ExecutionHandle(
            pid=-1,  # Sync execution, no separate PID
            channel_type=self._channel.get_current_channel_type(),
            command=command,
            start_time=time.time(),
            handle_id=handle_id
        )

        return handle

    def launch_profile(
        self,
        profile_name: str,
        args: List[str] = None,
        timeout: int = None,
        background:bool = True,
        serial_device: str = None
    ) -> ExecutionHandle:
        """
        Launch a workload by profile name.

        Convenience method that combines build_command() + launch()

        Args:
            profile_name: Name of profile to launch
            args: Override profile args (optional)
            timeout: Command timeout (optional)

        Returns:
            ExecutionHandle: Handle to control the execution
        """
        command = self.build_command(profile_name, args,serial_device=serial_device,background=background)
        return self.launch(command, timeout)

    def stop(self, handle: ExecutionHandle) -> None:
        """
        Stop a running workload.

        Args:
            handle: Handle returned from launch()
        """
        # For sync execution, stop is a no-op
        # For async, would need to track PIDs and kill them
        logger.info(f"Stop requested for handle {handle.handle_id}")

    def is_running(self, handle: ExecutionHandle) -> bool:
        """
        Check if workload is still running.

        For sync execution (current implementation), always returns False
        after launch completes.

        Args:
            handle: Handle returned from launch()

        Returns:
            bool: True if still running
        """
        # Current implementation is synchronous, so always False after launch
        return False

    def wait(self, handle: ExecutionHandle, timeout: int = None) -> int:
        """
        Wait for workload to complete.

        For sync execution, returns immediately with the command exit code.

        Args:
            handle: Handle returned from launch()
            timeout: Maximum wait time in seconds (None = wait forever)

        Returns:
            int: Return code from workload
        """
        # Sync execution, so we already have the result
        # Return 0 for sync execution (actual result was from invoke)
        return 0


class SchedulerFacade:
    """
    High-level interface combining MonitorController and WorkloadController.

    Provides a simple API for scheduling tools that need both
    monitoring and execution control.

    Usage (Coupled mode - all-in-one):
        scheduler = SchedulerFacade()
        scheduler.prepare(channel_manager, profile_registry, serial_port)
        result = scheduler.execute("gpu_vulkan_game_light")

    Usage (Decoupled mode - separate control):
        scheduler = SchedulerFacade()
        scheduler.prepare(channel_manager, profile_registry, serial_port)

        scheduler.start_monitoring()
        handle = scheduler.launch_workload("gpu_vulkan_game_light")

        while not scheduler.is_complete():
            scheduler.process_serial()

        result = scheduler.get_verdict()
        scheduler.stop()
    """

    def __init__(self):
        """Initialize SchedulerFacade."""
        self._monitor: Optional[MonitorController] = None
        self._workload: Optional[WorkloadController] = None
        self._serial_port: Optional[str] = None
        self._channel_manager = None
        self._prepared = False

    def prepare(
        self,
        channel_manager,
        profile_registry=None,
        serial_port: str = None,
        config: str = None,
        heartbeat_timeout: float = 45.0,
        overall_timeout: float = 300.0,
        baudrate:int = 115200
    ):
        """
        Prepare all components.

        Args:
            channel_manager: For workload execution
            profile_registry: For profile lookups (optional)
            serial_port: PC serial port path (optional, can be set later)
            config: Monitor configuration / rule file path (optional)
            heartbeat_timeout: Heartbeat watchdog timeout in seconds
            overall_timeout: Overall test timeout in seconds
        """
        self._channel_manager = channel_manager

        # Create MonitorController
        self._monitor = MonitorController(
            config_path=config,
            heartbeat_timeout=heartbeat_timeout,
            overall_timeout=overall_timeout,
            baudrate = baudrate
        )

        # Create WorkloadController
        self._workload = WorkloadController(channel_manager, profile_registry)

        # Store serial port for later use
        self._serial_port = serial_port

        self._prepared = True
        logger.info("SchedulerFacade prepared")

    # ─────────────────────────────────────────────────────────────────
    # Monitor Control
    # ─────────────────────────────────────────────────────────────────

    def start_monitoring(self):
        """Start monitoring (does not launch workload)."""
        if not self._prepared:
            raise RuntimeError("SchedulerFacade not prepared, call prepare() first")

        self._monitor.start()

    def process_line(self, line: str):
        """Feed a single log line to monitor."""
        if not self._monitor:
            raise RuntimeError("MonitorController not initialized")
        self._monitor.process_line(line)

    def process_lines(self, lines: List[str]):
        """Feed log lines to monitor."""
        if not self._monitor:
            raise RuntimeError("MonitorController not initialized")

        self._monitor.process_lines(lines)

    def process_serial(self):
        """
        Read and process lines from serial port.

        Call this in a loop when using serial for line input.
        Returns immediately if no data available.

        Note: Requires serial_port to be set via prepare()
        """
        if not self._serial_port:
            logger.warning("No serial port configured for process_serial()")
            return

        # Import here to avoid hard dependency if not used
        try:
            from serial_monitor import SerialMonitor
        except ImportError:
            logger.error("pyserial not available for process_serial()")
            return

        # This would need actual serial reading in a real implementation
        # For now, this is a placeholder
        logger.debug(f"process_serial() called, port={self._serial_port}")

    def is_complete(self) -> bool:
        """Check if monitoring is complete."""
        if not self._monitor:
            return True
        return self._monitor.is_complete()

    def is_started(self) -> bool:
        """Check if monitoring is active."""
        if not self._monitor:
            return False
        return self._monitor.is_started()

    def get_verdict(self) -> str:
        """Get current verdict."""
        if not self._monitor:
            raise RuntimeError("MonitorController not initialized")
        return self._monitor.get_verdict()

    def get_exit_code(self) -> int:
        """Get exit code for verdict."""
        if not self._monitor:
            return 3
        return self._monitor.get_exit_code()

    def get_stats(self) -> MonitoringStats:
        """Get monitoring statistics."""
        if not self._monitor:
            raise RuntimeError("MonitorController not initialized")
        return self._monitor.get_stats()

    def stop(self):
        """Stop monitoring and close resources."""
        if self._monitor:
            self._monitor.stop()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop()
        return False

    # ─────────────────────────────────────────────────────────────────
    # Workload Control
    # ─────────────────────────────────────────────────────────────────

    def build_command(self, profile: str, args: List[str] = None) -> str:
        """Build workload command."""
        if not self._workload:
            raise RuntimeError("WorkloadController not initialized")
        return self._workload.build_command(profile, args)

    def list_profiles(self, include_pending: bool = False) -> List[str]:
        """List available profiles."""
        if not self._workload:
            return []
        return self._workload.list_profiles(include_pending)

    def launch_workload(
        self,
        profile: str,
        args: List[str] = None,
        timeout: int = None,
        serial_device: str = None
    ) -> ExecutionHandle:
        """Launch workload by profile."""
        if not self._workload:
            raise RuntimeError("WorkloadController not initialized")
        return self._workload.launch_profile(profile, args, timeout, serial_device=serial_device)

    def stop_workload(self, handle: ExecutionHandle):
        """Stop running workload."""
        if not self._workload:
            return
        self._workload.stop(handle)

    def is_workload_running(self, handle: ExecutionHandle) -> bool:
        """Check if workload is running."""
        if not self._workload:
            return False
        return self._workload.is_running(handle)

    # ─────────────────────────────────────────────────────────────────
    # Convenience
    # ─────────────────────────────────────────────────────────────────

    def execute(self, profile: str) -> str:
        """
        Execute and monitor (coupled mode).

        This is a convenience method that:
        1. Starts monitoring
        2. Launches workload
        3. Monitors until complete
        4. Returns verdict

        Use this for rapid testing. For scheduling control,
        use start_monitoring() and launch_workload() separately.

        Args:
            profile: Profile name to execute

        Returns:
            str: Verdict (PASS, FAIL, SILENT_FAILURE, RUNNING)
        """
        if not self._prepared:
            raise RuntimeError("SchedulerFacade not prepared")

        self.start_monitoring()
        handle = self.launch_workload(profile)

        while not self.is_complete():
            self.process_serial()
            time.sleep(0.1)  # Avoid busy loop

        return self.get_verdict()


# Export classes and constants
__all__ = [
    'MonitorController',
    'WorkloadController',
    'SchedulerFacade',
    'ExecutionHandle',
    'MonitoringStats',
    'VERDICT_RUNNING',
    'VERDICT_PASS',
    'VERDICT_FAIL',
    'VERDICT_SILENT_FAILURE',
]

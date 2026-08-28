"""
Test Orchestrator - High-Level Integration Layer

Integrates ChannelManager, SerialPortManager, and workload execution
into a unified test orchestration system.

Per DEVELOPMENT.md:
- Abstract naming (target, workload) not specific (gpu, cpu)
- Profile-based configuration
- 5-layer architecture integration

Part of the 5-layer architecture (L4 integration point).

Author: Vmin Judge Tool Development
Version: 1.0
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Callable
from enum import Enum

# Configure module logger
logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

class RunMode(str, Enum):
    """Test execution mode."""
    PROD = "prod"
    DEBUG = "debug"


class LogChannel(str, Enum):
    """Log channels to monitor."""
    DMESG = "dmesg"
    HILOG = "hilog"
    LOGCAT = "logcat"


@dataclass
class TestOrchestratorConfig:
    """
    Configuration for test orchestration.

    Attributes:
        prefer_hdc: Prefer HDC over ADB for device connection.
        channel_timeout: Timeout for channel operations (seconds).
        max_channel_retries: Maximum retry attempts for channel operations.

        auto_discover_ports: Enable automatic serial port discovery.
        explicit_device_port: Explicit device serial port (bypasses discovery).
        explicit_pc_port: Explicit PC serial port (bypasses discovery).
        serial_baudrate: Serial baud rate.

        timeout_sec: Test execution timeout (seconds).
        grace_period_sec: Grace period after test completion (seconds).
        fail_on_non_zero: Fail test if workload exits with non-zero code.

        mode: Execution mode (prod or debug).
        hot_reload_rules: Enable hot-reload of rules (debug mode).
        channels: List of log channels to monitor.

        log_dir: Directory for log output.
        log_level: Logging level.

        verify_before_execute: Verify serial connection before execution.
        check_workload_exists: Check if workload binary exists on device.
    """
    # Channel settings
    prefer_hdc: bool = True
    channel_timeout: int = 30
    max_channel_retries: int = 3

    # Serial port settings
    auto_discover_ports: bool = True
    explicit_device_port: Optional[str] = None
    explicit_pc_port: Optional[str] = None
    serial_baudrate: int = 9600

    # Test settings
    timeout_sec: int = 60
    grace_period_sec: int = 2
    fail_on_non_zero: bool = True

    # Mode settings
    mode: RunMode = RunMode.PROD
    hot_reload_rules: bool = False
    channels: List[LogChannel] = field(
        default_factory=lambda: [LogChannel.DMESG]
    )

    # Logging settings
    log_dir: str = "./logs"
    log_level: str = "INFO"

    # Pre-flight checks
    verify_before_execute: bool = True
    check_workload_exists: bool = True

    def __post_init__(self):
        """Validate configuration."""
        if self.timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        if self.grace_period_sec < 0:
            raise ValueError("grace_period_sec must be non-negative")


# =============================================================================
# Test Session State
# =============================================================================

@dataclass
class TestSession:
    """
    Represents the state of a test session.

    Attributes:
        prepared: Whether prepare() has been called successfully.
        connected: Whether device is connected.
        ports_verified: Whether serial ports have been verified.
        workload_started: Whether workload has been started.
        workload_pid: Workload process ID (if applicable).
        start_time: Session start timestamp.
    """
    __test__ = False  # Exclude from pytest test collection

    prepared: bool = False
    connected: bool = False
    ports_verified: bool = False
    workload_started: bool = False
    workload_pid: Optional[int] = None
    start_time: Optional[float] = None

    @property
    def is_ready(self) -> bool:
        """Check if session is ready for execution."""
        return self.prepared and self.connected and self.ports_verified


# =============================================================================
# Result Types
# =============================================================================

@dataclass
class PrepareResult:
    """Result of prepare() operation."""
    success: bool
    error: Optional[str] = None
    channel_used: Optional[str] = None
    device_port: Optional[str] = None
    pc_port: Optional[str] = None


@dataclass
class ExecutionResult:
    """Result of workload execution."""
    success: bool
    return_code: int
    channel_used: str
    device_port: str
    pc_port: str
    duration_sec: float
    error: Optional[str] = None
    stdout: str = ""
    stderr: str = ""

    @property
    def passed(self) -> bool:
        """Check if execution passed (return code 0)."""
        return self.success and self.return_code == 0


# =============================================================================
# Test Orchestrator
# =============================================================================

class OrchestratorCore:
    """
    High-level test orchestration.

    Integrates:
    - ChannelManager (device communication)
    - SerialPortManager (port discovery and pairing)
    - WorkloadProfileRegistry (profile management)
    - SerialRedirectCommandBuilder (command building)

    Usage:
        # Create orchestrator with config
        config = TestOrchestratorConfig(
            prefer_hdc=True,
            timeout_sec=60,
            auto_discover_ports=True
        )
        orchestrator = OrchestratorCore(config)

        # Prepare (connect, discover ports, verify)
        if orchestrator.prepare():
            # Execute using profile
            result = orchestrator.execute("gpu_vulkan_game_light")

            if result.passed:
                print("Test PASSED")
            else:
                print(f"Test FAILED: {result.error}")

            # Cleanup
            orchestrator.cleanup()

        # Or with context manager
        with OrchestratorCore(config) as orchestrator:
            result = orchestrator.execute("gpu_vulkan_game_light")
            print(f"Result: {result.passed}")
    """

    def __init__(
        self,
        config: TestOrchestratorConfig,
        channel_manager: Optional["ChannelManager"] = None,
        serial_port_manager: Optional["SerialPortManager"] = None,
        profile_registry: Optional["WorkloadProfileRegistry"] = None,
        command_builder: Optional["WorkloadCommandBuilder"] = None
    ):
        """
        Initialize test orchestrator.

        Args:
            config: TestOrchestratorConfig instance.
            channel_manager: Optional pre-configured ChannelManager.
                             Created internally if None.
            serial_port_manager: Optional pre-configured SerialPortManager.
                                Created internally if None.
            profile_registry: Optional pre-configured WorkloadProfileRegistry.
                             Created internally if None.
            command_builder: Optional pre-configured WorkloadCommandBuilder.
                            Created internally if None.
        """
        self._config = config
        self._session = TestSession()

        # Components (initialized in prepare)
        self._channel_manager = channel_manager
        self._serial_port_manager = serial_port_manager
        self._profile_registry = profile_registry
        self._command_builder = command_builder

        # Callbacks
        self._on_prepare: Optional[Callable[[PrepareResult], None]] = None
        self._on_execute: Optional[Callable[[ExecutionResult], None]] = None
        self._on_cleanup: Optional[Callable[[], None]] = None

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def config(self) -> TestOrchestratorConfig:
        """Get configuration."""
        return self._config

    @property
    def session(self) -> TestSession:
        """Get current session state."""
        return self._session

    @property
    def is_prepared(self) -> bool:
        """Check if orchestrator is prepared."""
        return self._session.prepared

    @property
    def channel_manager(self):
        """Get channel manager (raises if not prepared)."""
        self._require_prepared()
        return self._channel_manager

    @property
    def serial_port_manager(self):
        """Get serial port manager (raises if not prepared)."""
        self._require_prepared()
        return self._serial_port_manager

    # =========================================================================
    # Setup Methods
    # =========================================================================

    def prepare(self) -> PrepareResult:
        """
        Prepare for test execution.

        Performs:
        1. Create and connect ChannelManager
        2. Create and discover SerialPortManager
        3. Verify serial connection

        Returns:
            PrepareResult indicating success or failure.
        """
        logger.info("Preparing test orchestrator...")

        try:
            # 1. Setup channel manager
            result = self._prepare_channel()
            if not result.success:
                return result

            # 2. Setup serial port manager
            result = self._prepare_serial()
            if not result.success:
                return result

            # 3. Pre-flight checks
            if self._config.verify_before_execute:
                verify_result = self._verify_connection()
                if not verify_result.success:
                    return verify_result

            # Mark as prepared
            self._session.prepared = True
            self._session.connected = True
            self._session.ports_verified = True
            self._session.start_time = time.time()

            prepare_result = PrepareResult(
                success=True,
                channel_used=self._channel_manager.get_current_channel_type(),
                device_port=self._serial_port_manager.get_device_port(),
                pc_port=self._serial_port_manager.get_pc_port()
            )

            # Call callback
            if self._on_prepare:
                self._on_prepare(prepare_result)

            logger.info(
                f"Preparation complete: "
                f"channel={prepare_result.channel_used}, "
                f"device={prepare_result.device_port}, "
                f"pc={prepare_result.pc_port}"
            )

            return prepare_result

        except Exception as e:
            logger.error(f"Preparation failed: {e}")
            return PrepareResult(success=False, error=str(e))

    def _prepare_channel(self) -> PrepareResult:
        """Prepare channel manager."""
        from channel_manager import create_channel_manager

        logger.info("Setting up channel manager...")

        if self._channel_manager is None:
            self._channel_manager = create_channel_manager(
                prefer_hdc=self._config.prefer_hdc,
                mode="AUTO"
            )

        if not self._channel_manager.connect():
            return PrepareResult(
                success=False,
                error="Failed to connect to device"
            )

        logger.info(
            f"Connected via {self._channel_manager.get_current_channel_type()}"
        )

        return PrepareResult(success=True)

    def _prepare_serial(self) -> PrepareResult:
        """Prepare serial port manager."""
        from serial_port_manager import (
            create_serial_port_manager,
            SerialPortConfig
        )

        logger.info("Setting up serial port manager...")

        if self._serial_port_manager is None:
            # Create serial config
            serial_config = SerialPortConfig(
                auto_discover=self._config.auto_discover_ports,
                explicit_device_port=self._config.explicit_device_port,
                explicit_pc_port=self._config.explicit_pc_port,
                baudrate=self._config.serial_baudrate
            )

            self._serial_port_manager = create_serial_port_manager(
                channel=self._channel_manager,
                config=serial_config,
                use_real_impl=False  # Use mock for testing
            )

        # Discover ports
        pairing_result = self._serial_port_manager.discover()

        if not pairing_result.success:
            # Try fallback with explicit ports if configured
            if self._config.explicit_device_port and self._config.explicit_pc_port:
                logger.warning("Auto-discovery failed, trying explicit ports...")
                # Create a new pairing result with explicit ports
                from serial_port_manager import PairingResult, PortPair
                pairing_result = PairingResult(
                    success=True,
                    pair=PortPair(
                        device_port=self._config.explicit_device_port,
                        pc_port=self._config.explicit_pc_port,
                        confidence=1.0
                    )
                )
            else:
                return PrepareResult(
                    success=False,
                    error=f"Serial port discovery failed: {pairing_result.error}"
                )

        logger.info(
            f"Serial ports: device={pairing_result.device_port}, "
            f"pc={pairing_result.pc_port}"
        )

        return PrepareResult(success=True)

    def _verify_connection(self) -> PrepareResult:
        """Verify serial connection."""
        logger.info("Verifying serial connection...")

        if not self._serial_port_manager.verify_connection():
            return PrepareResult(
                success=False,
                error="Serial connection verification failed"
            )

        logger.info("Serial connection verified")
        return PrepareResult(success=True)

    # =========================================================================
    # Execution Methods
    # =========================================================================

    def execute(
        self,
        profile_name: str,
        args: Optional[List[str]] = None,
        serial_device: Optional[str] = None,
        background: bool = False
    ) -> ExecutionResult:
        """
        Execute workload using a profile.

        Args:
            profile_name: Name of profile in registry.
            args: Override profile default arguments.
            serial_device: Override profile default serial device.
            background: Run in background (don't wait for completion).

        Returns:
            ExecutionResult with execution outcome.
        """
        self._require_prepared()

        start_time = time.time()

        # Load profile registry and command builder if not set
        self._ensure_command_components()

        # Check if workload exists (if enabled)
        if self._config.check_workload_exists:
            profile = self._profile_registry.get(profile_name)
            if profile and profile.is_pending:
                logger.warning(
                    f"Profile '{profile_name}' is pending. "
                    f"Workload may not exist: {profile.workload_path}"
                )

        try:
            # Build command
            cmd = self._command_builder.build(
                profile_name,
                args=args,
                serial_device=serial_device,
                background=background
            )

            logger.info(f"Executing: {cmd}")

            # Invoke via channel
            invoke_result = self._channel_manager.invoke(
                cmd,
                timeout=self._config.timeout_sec if not background else 10
            )

            duration = time.time() - start_time

            result = ExecutionResult(
                success=invoke_result.success,
                return_code=invoke_result.return_code,
                channel_used=invoke_result.channel_used,
                device_port=self._serial_port_manager.get_device_port() or "",
                pc_port=self._serial_port_manager.get_pc_port() or "",
                duration_sec=duration,
                stdout=invoke_result.stdout,
                stderr=invoke_result.stderr,
                error=invoke_result.stderr if not invoke_result.success else None
            )

            # Check fail_on_non_zero
            if self._config.fail_on_non_zero and invoke_result.return_code != 0:
                result.success = False
                result.error = f"Workload exited with code {invoke_result.return_code}"

            self._session.workload_started = True

            # Call callback
            if self._on_execute:
                self._on_execute(result)

            return result

        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"Execution failed: {e}")

            return ExecutionResult(
                success=False,
                return_code=-1,
                channel_used=self._channel_manager.get_current_channel_type() or "",
                device_port=self._serial_port_manager.get_device_port() or "",
                pc_port=self._serial_port_manager.get_pc_port() or "",
                duration_sec=duration,
                error=str(e)
            )

    def execute_custom(
        self,
        workload_path: str,
        serial_device: Optional[str] = None,
        args: Optional[List[str]] = None,
        background: bool = False
    ) -> ExecutionResult:
        """
        Execute a custom workload (no profile).

        Args:
            workload_path: Full path to workload binary on device.
            serial_device: Serial device for output redirection.
            args: Workload arguments.
            background: Run in background.

        Returns:
            ExecutionResult with execution outcome.
        """
        self._require_prepared()

        # Ensure command builder exists
        self._ensure_command_components()

        # Build command
        cmd = self._command_builder.build_custom(
            workload_path=workload_path,
            serial_device=serial_device,
            args=args,
            background=background
        )

        logger.info(f"Executing custom workload: {cmd}")

        start_time = time.time()

        try:
            invoke_result = self._channel_manager.invoke(
                cmd,
                timeout=self._config.timeout_sec if not background else 10
            )

            duration = time.time() - start_time

            result = ExecutionResult(
                success=invoke_result.success,
                return_code=invoke_result.return_code,
                channel_used=invoke_result.channel_used,
                device_port=self._serial_port_manager.get_device_port() or "",
                pc_port=self._serial_port_manager.get_pc_port() or "",
                duration_sec=duration,
                stdout=invoke_result.stdout,
                stderr=invoke_result.stderr,
                error=invoke_result.stderr if not invoke_result.success else None
            )

            if self._config.fail_on_non_zero and invoke_result.return_code != 0:
                result.success = False

            self._session.workload_started = True

            return result

        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"Custom execution failed: {e}")

            return ExecutionResult(
                success=False,
                return_code=-1,
                channel_used=self._channel_manager.get_current_channel_type() or "",
                device_port=self._serial_port_manager.get_device_port() or "",
                pc_port=self._serial_port_manager.get_pc_port() or "",
                duration_sec=duration,
                error=str(e)
            )

    def _ensure_command_components(self) -> None:
        """Ensure profile registry and command builder are initialized."""
        from workload_profiles import (
            WorkloadProfileRegistry,
            get_default_config_path
        )
        from workload_builder import WorkloadCommandBuilder

        if self._profile_registry is None:
            self._profile_registry = WorkloadProfileRegistry(get_default_config_path())
            self._profile_registry.load()

        if self._command_builder is None:
            self._command_builder = WorkloadCommandBuilder(self._profile_registry)

    # =========================================================================
    # Cleanup
    # =========================================================================

    def cleanup(self) -> None:
        """
        Clean up resources.

        Disconnects channel and resets session state.
        Safe to call multiple times.
        """
        logger.info("Cleaning up test orchestrator...")

        if self._channel_manager:
            self._channel_manager.disconnect()
            self._channel_manager = None

        self._session = TestSession()

        if self._on_cleanup:
            self._on_cleanup()

        logger.info("Cleanup complete")

    def __enter__(self) -> "TestOrchestrator":
        """Context manager entry - prepare orchestrator."""
        self.prepare()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - cleanup."""
        self.cleanup()

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _require_prepared(self) -> None:
        """Raise if orchestrator is not prepared."""
        if not self._session.prepared:
            raise RuntimeError(
                "Orchestrator not prepared. Call prepare() first or use "
                "context manager: 'with TestOrchestrator(config) as orchestrator:'"
            )

    def set_prepare_callback(self, callback: Callable[[PrepareResult], None]) -> None:
        """Set callback for prepare() completion."""
        self._on_prepare = callback

    def set_execute_callback(self, callback: Callable[[ExecutionResult], None]) -> None:
        """Set callback for execute() completion."""
        self._on_execute = callback

    def set_cleanup_callback(self, callback: Callable[[], None]) -> None:
        """Set callback for cleanup()."""
        self._on_cleanup = callback


# =============================================================================
# Factory Function
# =============================================================================

def create_test_orchestrator(
    config: Optional[TestOrchestratorConfig] = None,
    profile_config_path: Optional[str] = None,
    use_real_serial: bool = False
) -> TestOrchestrator:
    """
    Factory function to create a configured TestOrchestrator.

    Args:
        config: Test configuration. Uses defaults if None.
        profile_config_path: Path to workload_profiles.yaml.
        use_real_serial: If True, use real serial implementations.

    Returns:
        Configured TestOrchestrator instance.
    """
    from workload_profiles import (
        WorkloadProfileRegistry,
        get_default_config_path
    )
    from workload_builder import WorkloadCommandBuilder

    config = config or TestOrchestratorConfig()

    # Load profile registry
    if profile_config_path is None:
        profile_config_path = get_default_config_path()

    registry = WorkloadProfileRegistry(profile_config_path)
    registry.load()

    # Create command builder
    command_builder = WorkloadCommandBuilder(registry)

    return OrchestratorCore(
        config=config,
        profile_registry=registry,
        command_builder=command_builder
    )

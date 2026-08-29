"""
Channel Manager - L1: Communication Layer

Provides device channel abstraction for HDC (HiSilicon Device Connector) and
ADB (Android Debug Bridge). Supports auto-switching and health monitoring.

Part of the 5-layer architecture defined in ARCHITECTURE.md.

Author: Vmin Judge Tool Development
Version: 1.0
"""

from __future__ import annotations

import subprocess
import time
import threading
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Callable, Tuple
from pathlib import Path

# Configure module logger
logger = logging.getLogger(__name__)


class ChannelState(Enum):
    """Channel connection states."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass
class ChannelHealth:
    """Health status of a channel."""
    is_healthy: bool = True
    last_check: float = 0.0
    consecutive_failures: int = 0
    last_error: Optional[str] = None


@dataclass
class InvokeResult:
    """Result of a channel invoke command."""
    return_code: int
    stdout: str
    stderr: str
    duration_sec: float
    channel_used: str

    @property
    def success(self) -> bool:
        return self.return_code == 0

    def __iter__(self):
        """Allow unpacking as tuple (code, stdout, stderr) for backward compatibility."""
        return iter((self.return_code, self.stdout, self.stderr))

    def __getitem__(self, index):
        """Allow tuple-style indexing for backward compatibility."""
        return (self.return_code, self.stdout, self.stderr)[index]


class DeviceChannel(ABC):
    """
    Abstract base class for device communication channels.

    Defines the interface contract for channel implementations.
    All concrete implementations must implement these methods.

    Example:
        class MyChannel(DeviceChannel):
            def connect(self) -> bool:
                # Implementation
                pass
    """

    @abstractmethod
    def connect(self) -> bool:
        """
        Establish connection to device.

        Returns:
            bool: True if connection successful, False otherwise.
        """
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Close connection and release resources."""
        pass

    @abstractmethod
    def invoke(self, cmd: str, timeout: int = 30,background:bool = False) -> Tuple[int, str, str]:
        """
        Execute command on device.

        Args:
            cmd: Command string to execute.
            timeout: Command timeout in seconds.

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        pass

    @abstractmethod
    def push(self, local_path: str, remote_path: str) -> bool:
        """
        Upload file to device.

        Args:
            local_path: Path to local file.
            remote_path: Destination path on device.

        Returns:
            bool: True if upload successful, False otherwise.
        """
        pass

    @abstractmethod
    def pull(self, remote_path: str, local_path: str) -> bool:
        """
        Download file from device.

        Args:
            remote_path: Path to file on device.
            local_path: Destination path on local machine.

        Returns:
            bool: True if download successful, False otherwise.
        """
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """
        Check connection health.

        Returns:
            bool: True if connected and healthy, False otherwise.
        """
        pass

    @abstractmethod
    def get_type(self) -> str:
        """
        Get channel type identifier.

        Returns:
            str: Channel type (e.g., 'hdc', 'adb').
        """
        pass

    def health_check(self) -> ChannelHealth:
        """
        Perform a basic health check.

        Default implementation checks if connected.
        Override for more comprehensive checks.

        Returns:
            ChannelHealth: Current health status.
        """
        health = ChannelHealth()
        health.is_healthy = self.is_connected()
        health.last_check = time.time()
        return health


class HDCChannel(DeviceChannel):
    """
    HDC (HiSilicon Device Connector) channel implementation.

    HDC is Huawei's proprietary device communication protocol.
    Used for HarmonyOS and Kirin-based devices.
    """

    def __init__(self, serial: Optional[str] = None, timeout: int = 30):
        """
        Initialize HDC channel.

        Args:
            serial: Device serial number (optional).
            timeout: Default command timeout in seconds.
        """
        self._serial = serial
        self._timeout = timeout
        self._connected = False
        self._health = ChannelHealth()
        self._lock = threading.Lock()

        # Verify hdc binary exists
        self._hdc_path = self._find_hdc()
        if not self._hdc_path:
            logger.warning("HDC binary not found in PATH")

    def _find_hdc(self) -> Optional[str]:
        """Find hdc binary path."""
        try:
            result = subprocess.run(
                ["where", "hdc"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split('\n')[0]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def _build_hdc_cmd(self, *args) -> List[str]:
        """Build HDC command with common options."""
        cmd = [self._hdc_path] if self._hdc_path else ["hdc"]
        if self._serial:
            cmd.extend(["-t", self._serial])
        cmd.extend(args)
        return cmd

    def connect(self) -> bool:
        """Establish HDC connection."""
        with self._lock:
            try:
                # Check if hdc is available
                if not self._hdc_path:
                    logger.error("HDC binary not found")
                    self._connected = False
                    return False

                # Try to list targets to verify connection
                result = subprocess.run(
                    self._build_hdc_cmd("list", "targets"),
                    capture_output=True,
                    text=True,
                    timeout=10
                )

                if result.returncode == 0:
                    # If serial is specified, verify it's in the output
                    if self._serial:
                        if self._serial in result.stdout:
                            self._connected = True
                        else:
                            self._connected = False
                            logger.error(f"HDC connection failed: device {self._serial} not found")
                    else:
                        self._connected = True
                    
                    if self._connected:
                        logger.debug("HDC connection established")
                        self._health = ChannelHealth(is_healthy=True, last_check=time.time())
                else:
                    self._connected = False
                    logger.error(f"HDC connection failed: {result.stderr}")

                return self._connected

            except subprocess.TimeoutExpired:
                logger.error("HDC connection timeout")
                self._connected = False
                return False
            except Exception as e:
                logger.error(f"HDC connection error: {e}")
                self._connected = False
                return False

    def disconnect(self) -> None:
        """Close HDC connection."""
        with self._lock:
            self._connected = False
            logger.debug("HDC connection closed")

    def invoke(self, cmd: str, timeout: int = None,background: bool = False) -> Tuple[int, str, str]:
        """
        Execute command via HDC shell.

        Args:
            cmd: Command to execute.
            timeout: Timeout in seconds (uses default if None).

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        if timeout is None:
            timeout = self._timeout

        try:
            remote_marker = "__VMIN_REMOTE_RC__="
            wrapped_cmd = f"{cmd}; __vmin_rc=$?; echo {remote_marker}$__vmin_rc"
            result = subprocess.run(
                self._build_hdc_cmd("shell", wrapped_cmd),
                capture_output=True,
                text=True,
                timeout=timeout
            )

            if result.returncode != 0:
                return (result.returncode, result.stdout, result.stderr)
            marker_index = result.stdout.rfind(remote_marker)
            if marker_index < 0:
                return (-1, result.stdout, (result.stderr + "\nHDC shell returned no remote status").strip())
            status_text = result.stdout[marker_index + len(remote_marker):].strip().splitlines()[0]
            try:
                remote_status = int(status_text)
            except ValueError:
                return (-1, result.stdout[:marker_index], f"Invalid HDC remote status: {status_text!r}")
            return (remote_status, result.stdout[:marker_index], result.stderr)

        except subprocess.TimeoutExpired:
            if background:
                logger.debug(f"HDC background command timeout (expected) :{cmd[:50]}...")
            else:
                logger.error(f"HDC invoke timeout: {cmd[:50]}...")
            return (-1, "", f"Command timeout after {timeout}s")
        except Exception as e:
            logger.error(f"HDC invoke error: {e}")
            return (-1, "", str(e))

    def push(self, local_path: str, remote_path: str) -> bool:
        """
        Push file to device via HDC.

        Args:
            local_path: Local file path.
            remote_path: Remote destination path.

        Returns:
            bool: True if successful.
        """
        try:
            result = subprocess.run(
                self._build_hdc_cmd("file", "send", local_path, remote_path),
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                logger.debug(f"Pushed {local_path} to {remote_path}")
                return True
            else:
                logger.error(f"HDC push failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"HDC push timeout: {local_path}")
            return False
        except Exception as e:
            logger.error(f"HDC push error: {e}")
            return False

    def pull(self, remote_path: str, local_path: str) -> bool:
        """
        Pull file from device via HDC.

        Args:
            remote_path: Remote file path.
            local_path: Local destination path.

        Returns:
            bool: True if successful.
        """
        try:
            result = subprocess.run(
                self._build_hdc_cmd("file", "recv", remote_path, local_path),
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                logger.debug(f"Pulled {remote_path} to {local_path}")
                return True
            else:
                logger.error(f"HDC pull failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"HDC pull timeout: {remote_path}")
            return False
        except Exception as e:
            logger.error(f"HDC pull error: {e}")
            return False

    def is_connected(self) -> bool:
        """Check if HDC connection is healthy."""
        with self._lock:
            if not self._connected:
                return False

            try:
                result = subprocess.run(
                    self._build_hdc_cmd("list", "targets"),
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode != 0:
                    return False
                # Verify specific serial if set
                if self._serial:
                    return self._serial in result.stdout
                return True
            except (subprocess.TimeoutExpired, OSError, Exception) as e:
                logger.warning(f"HDC health check error: {e}")
                return False

    def get_type(self) -> str:
        """Get channel type."""
        return "hdc"


class ADBChannel(DeviceChannel):
    """
    ADB (Android Debug Bridge) channel implementation.

    Standard Android device communication protocol.
    Used for generic Android devices.
    """

    def __init__(self, serial: Optional[str] = None, timeout: int = 30):
        """
        Initialize ADB channel.

        Args:
            serial: Device serial number (optional).
            timeout: Default command timeout in seconds.
        """
        self._serial = serial
        self._timeout = timeout
        self._connected = False
        self._health = ChannelHealth()
        self._lock = threading.Lock()

        # Verify adb binary exists
        self._adb_path = self._find_adb()
        if not self._adb_path:
            logger.warning("ADB binary not found in PATH")

    def _find_adb(self) -> Optional[str]:
        """Find adb binary path."""
        try:
            result = subprocess.run(
                ["where", "adb"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split('\n')[0]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def _build_adb_cmd(self, *args) -> List[str]:
        """Build ADB command with common options."""
        cmd = [self._adb_path] if self._adb_path else ["adb"]
        if self._serial:
            cmd.extend(["-s", self._serial])
        cmd.extend(args)
        return cmd

    def connect(self) -> bool:
        """Establish ADB connection."""
        with self._lock:
            try:
                if not self._adb_path:
                    logger.error("ADB binary not found")
                    self._connected = False
                    return False

                # Check devices
                result = subprocess.run(
                    self._build_adb_cmd("devices"),
                    capture_output=True,
                    text=True,
                    timeout=10
                )

                if result.returncode == 0:
                    # Check if device is in list
                    lines = result.stdout.strip().split('\n')
                    for line in lines[1:]:  # Skip header
                        if line.strip() and 'device' in line:
                            # If serial is specified, verify it matches
                            if self._serial and not line.startswith(self._serial):
                                continue
                            
                            self._connected = True
                            logger.debug("ADB connection established")
                            self._health = ChannelHealth(is_healthy=True, last_check=time.time())
                            return True

                    logger.warning("No matching ADB device found")
                    self._connected = False
                    return False
                else:
                    logger.error(f"ADB connection failed: {result.stderr}")
                    self._connected = False
                    return False

            except subprocess.TimeoutExpired:
                logger.error("ADB connection timeout")
                self._connected = False
                return False
            except Exception as e:
                logger.error(f"ADB connection error: {e}")
                self._connected = False
                return False

    def disconnect(self) -> None:
        """Close ADB connection."""
        with self._lock:
            self._connected = False
            logger.debug("ADB connection closed")

    def invoke(self, cmd: str, timeout: int = None,background: bool = False) -> Tuple[int, str, str]:
        """
        Execute command via ADB shell.

        Args:
            cmd: Command to execute.
            timeout: Timeout in seconds (uses default if None).

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        if timeout is None:
            timeout = self._timeout

        try:
            result = subprocess.run(
                self._build_adb_cmd("shell", cmd),
                capture_output=True,
                text=True,
                timeout=timeout
            )

            return (result.returncode, result.stdout, result.stderr)

        except subprocess.TimeoutExpired:
            if background:
                logger.debug(f"ADB background command timeout (expected): {cmd[:50]}...")
            else:
                logger.error(f"ADB invoke timeout: {cmd[:50]}...")
            return (-1, "", f"Command timeout after {timeout}s")
        except Exception as e:
            logger.error(f"ADB invoke error: {e}")
            return (-1, "", str(e))

    def push(self, local_path: str, remote_path: str) -> bool:
        """
        Push file to device via ADB.

        Args:
            local_path: Local file path.
            remote_path: Remote destination path.

        Returns:
            bool: True if successful.
        """
        try:
            result = subprocess.run(
                self._build_adb_cmd("push", local_path, remote_path),
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                logger.debug(f"Pushed {local_path} to {remote_path}")
                return True
            else:
                logger.error(f"ADB push failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"ADB push timeout: {local_path}")
            return False
        except Exception as e:
            logger.error(f"ADB push error: {e}")
            return False

    def pull(self, remote_path: str, local_path: str) -> bool:
        """
        Pull file from device via ADB.

        Args:
            remote_path: Remote file path.
            local_path: Local destination path.

        Returns:
            bool: True if successful.
        """
        try:
            result = subprocess.run(
                self._build_adb_cmd("pull", remote_path, local_path),
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                logger.debug(f"Pulled {remote_path} to {local_path}")
                return True
            else:
                logger.error(f"ADB pull failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"ADB pull timeout: {remote_path}")
            return False
        except Exception as e:
            logger.error(f"ADB pull error: {e}")
            return False

    def is_connected(self) -> bool:
        """Check if ADB connection is healthy."""
        with self._lock:
            if not self._connected:
                return False

            try:
                result = subprocess.run(
                    self._build_adb_cmd("get-state"),
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                return 'device' in result.stdout
            except (subprocess.TimeoutExpired, OSError, Exception) as e:
                logger.warning(f"ADB health check error: {e}")
                return False

    def get_type(self) -> str:
        """Get channel type."""
        return "adb"


class ChannelManager:
    """
    High-level channel management with auto-selection and fallback.

    Manages multiple channels (HDC, ADB) with automatic selection
    and switching based on availability and health.

    Usage:
        manager = ChannelManager()
        manager.add_channel(HDCChannel(), priority=10)
        manager.add_channel(ADBChannel(), priority=5)

        if manager.connect():
            result = manager.invoke("ls /data")
    """

    def __init__(
        self,
        mode: str = "AUTO",
        health_check_interval: int = 5,
        max_retries: int = 3
    ):
        """
        Initialize ChannelManager.

        Args:
            mode: Operation mode - 'AUTO', 'MANUAL', or 'HYBRID'
            health_check_interval: Health check interval in seconds
            max_retries: Maximum retry attempts for commands
        """
        self._mode = mode.upper()
        self._health_check_interval = health_check_interval
        self._max_retries = max_retries

        self._channels: List[DeviceChannel] = []
        self._priorities: dict[DeviceChannel, int] = {}
        self._primary_channel: Optional[DeviceChannel] = None
        self._manual_channel: Optional[DeviceChannel] = None
        self._current_channel: Optional[DeviceChannel] = None

        self._connected = False
        self._lock = threading.Lock()

        self._health_monitor_thread: Optional[threading.Thread] = None
        self._stop_health_monitor = threading.Event()

        self._health_callbacks: List[Callable[[ChannelHealth], None]] = []

        # Sort channels by priority (higher = preferred)
        self._channel_order: List[DeviceChannel] = []

    @property
    def mode(self) -> str:
        """Get current operation mode."""
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        """Set operation mode."""
        self._mode = value.upper()
        logger.debug(f"ChannelManager mode set to {self._mode}")

    def add_channel(self, channel: DeviceChannel, priority: int = 0) -> None:
        """
        Add a channel with priority.

        Args:
            channel: Channel implementation to add.
            priority: Channel priority (higher = preferred).
        """
        with self._lock:
            if channel not in self._channels:
                self._channels.append(channel)
                self._priorities[channel] = priority
                self._update_channel_order()
                logger.debug(f"Added {channel.get_type()} channel with priority {priority}")

    def remove_channel(self, channel: DeviceChannel) -> None:
        """
        Remove a channel.

        Args:
            channel: Channel to remove.
        """
        with self._lock:
            if channel in self._channels:
                self._channels.remove(channel)
                del self._priorities[channel]
                self._update_channel_order()
                logger.info(f"Removed {channel.get_type()} channel")

    def _update_channel_order(self) -> None:
        """Update sorted channel order by priority."""
        self._channel_order = sorted(
            self._channels,
            key=lambda c: self._priorities.get(c, 0),
            reverse=True
        )

    def set_primary(self, channel: DeviceChannel) -> bool:
        """
        Manually set primary channel (HYBRID/MANUAL mode).

        Args:
            channel: Channel to set as primary.

        Returns:
            bool: True if channel was set successfully.
        """
        with self._lock:
            if channel not in self._channels:
                logger.error(f"Channel not registered: {channel.get_type()}")
                return False

            self._manual_channel = channel
            self._primary_channel = channel
            logger.info(f"Set primary channel to {channel.get_type()}")
            return True

    def connect(self) -> bool:
        """
        Connect to device using best available channel.

        Returns:
            bool: True if connection successful.
        """
        with self._lock:
            if self._connected:
                return True

            # In MANUAL mode, use manually selected channel
            if self._mode == "MANUAL" and self._manual_channel:
                return self._manual_channel.connect()

            # Try channels in priority order
            for channel in self._channel_order:
                if self._mode == "HYBRID" and channel != self._manual_channel:
                    # In HYBRID mode, skip non-primary channels during initial connect
                    if self._manual_channel and channel != self._manual_channel:
                        continue

                logger.debug(f"Trying {channel.get_type()} channel...")
                if channel.connect():
                    self._current_channel = channel
                    self._primary_channel = channel
                    self._connected = True
                    logger.debug(f"Connected via {channel.get_type()}")

                    # Start health monitoring
                    self._start_health_monitor()
                    return True

            logger.error("No channels available")
            return False

    def disconnect(self) -> None:
        """Disconnect all channels."""
        with self._lock:
            self._stop_health_monitor.set()
            if self._health_monitor_thread:
                self._health_monitor_thread.join(timeout=2)

            for channel in self._channels:
                try:
                    channel.disconnect()
                except Exception as e:
                    logger.warning(f"Error disconnecting {channel.get_type()}: {e}")

            self._connected = False
            self._current_channel = None
            logger.debug("All channels disconnected")

    def invoke(
        self,
        cmd: str,
        timeout: int = 30,
        retry_on_failure: bool = True,
        background:bool = False
    ) -> InvokeResult:
        """
        Execute command via current channel.

        Args:
            cmd: Command to execute.
            timeout: Timeout in seconds.
            retry_on_failure: Whether to retry on channel failure.

        Returns:
            InvokeResult with command results.
        """
        start_time = time.time()

        if not self._connected or not self._current_channel:
            logger.error("No active channel")
            return InvokeResult(
                return_code=-1,
                stdout="",
                stderr="No channel connected",
                duration_sec=time.time() - start_time,
                channel_used="none"
            )

        attempts = self._max_retries if retry_on_failure else 1

        for attempt in range(attempts):
            try:
                return_code, stdout, stderr = self._current_channel.invoke(cmd, timeout,background=background)

                # FIX: Only retry/switch if the channel itself failed (return_code == -1 indicates subprocess/timeout error).
                # If return_code is anything else (including non-zero business logic codes like 1 or 2),
                # it means the channel is healthy and the command executed successfully, so we return immediately.
                if background and return_code == -1:
                    return InvokeResult(
                        return_code=return_code, stdout=stdout, stderr=stderr,
                        duration_sec=time.time() - start_time,
                        channel_used=self._current_channel.get_type()
                    )
                if return_code != -1 or attempt == attempts - 1:
                    return InvokeResult(
                        return_code=return_code,
                        stdout=stdout,
                        stderr=stderr,
                        duration_sec=time.time() - start_time,
                        channel_used=self._current_channel.get_type()
                    )

                # Channel communication failed, try to switch
                if attempt < attempts - 1:
                    logger.warning(f"Channel {self._current_channel.get_type()} communication failed (code {return_code}, stderr: {stderr}), trying next...")
                    if not self._switch_to_next():
                        break

            except Exception as e:
                logger.error(f"Invoke error: {e}")
                if attempt < attempts - 1 and self._switch_to_next():
                    continue
                return InvokeResult(
                    return_code=-1,
                    stdout="",
                    stderr=str(e),
                    duration_sec=time.time() - start_time,
                    channel_used=self._current_channel.get_type() if self._current_channel else "none"
                )

        return InvokeResult(
            return_code=-1,
            stdout="",
            stderr="All channels failed",
            duration_sec=time.time() - start_time,
            channel_used="none"
        )

    def _switch_to_next(self) -> bool:
        """
        Switch to next available channel.

        Returns:
            bool: True if switch successful.
        """
        with self._lock:
            current_idx = -1
            if self._current_channel in self._channel_order:
                current_idx = self._channel_order.index(self._current_channel)

            # Find next channel
            for i, channel in enumerate(self._channel_order):
                if i > current_idx and channel != self._current_channel:
                    if channel.is_connected():
                        self._current_channel = channel
                        logger.info(f"Switched to {channel.get_type()} channel")
                        return True

            return False

    def push(self, local_path: str, remote_path: str) -> bool:
        """
        Push file to device.

        Args:
            local_path: Local file path.
            remote_path: Remote destination path.

        Returns:
            bool: True if successful.
        """
        if not self._connected or not self._current_channel:
            logger.error("No active channel")
            return False

        return self._current_channel.push(local_path, remote_path)

    def pull(self, remote_path: str, local_path: str) -> bool:
        """
        Pull file from device.

        Args:
            remote_path: Remote file path.
            local_path: Local destination path.

        Returns:
            bool: True if successful.
        """
        if not self._connected or not self._current_channel:
            logger.error("No active channel")
            return False

        return self._current_channel.pull(remote_path, local_path)

    def is_connected(self) -> bool:
        """Check if any channel is connected."""
        return self._connected and self._current_channel is not None

    def get_current_channel_type(self) -> Optional[str]:
        """Get type of current channel."""
        if self._current_channel:
            return self._current_channel.get_type()
        return None

    def register_health_callback(
        self,
        callback: Callable[[ChannelHealth], None]
    ) -> None:
        """
        Register callback for health status changes.

        Args:
            callback: Function to call on health status update.
        """
        self._health_callbacks.append(callback)

    def _start_health_monitor(self) -> None:
        """Start background health monitoring thread."""
        self._stop_health_monitor.clear()
        self._health_monitor_thread = threading.Thread(
            target=self._health_monitor_loop,
            daemon=True
        )
        self._health_monitor_thread.start()
        logger.debug("Health monitoring started")

    def _health_monitor_loop(self) -> None:
        """
        Background health monitoring loop.

        Note: Uses the channel's stored _health object to track consecutive failures,
        not the health object returned by health_check().
        """
        while not self._stop_health_monitor.is_set():
            try:
                # FIX: Snapshot the current channel to local variable to prevent race conditions
                # where self._current_channel is set to None by another thread (e.g., disconnect)
                # while we are trying to check its health.
                current_channel = self._current_channel
                if current_channel:
                    # Check health (this returns a new ChannelHealth object)
                    health = current_channel.health_check()

                    # Get the stored health object for tracking failures
                    stored_health = current_channel._health

                    if not health.is_healthy:
                        stored_health.consecutive_failures += 1
                        stored_health.last_error = health.last_error
                        logger.warning(
                            f"Channel {current_channel.get_type()} "
                            f"health check failed ({stored_health.consecutive_failures})"
                        )

                        if stored_health.consecutive_failures >= 3:
                            logger.error("Channel health critical, attempting switch")
                            if not self._switch_to_next():
                                logger.error("No healthy channels available")
                    else:
                        stored_health.consecutive_failures = 0
                        stored_health.last_error = None

                    stored_health.last_check = time.time()

                    # Notify callbacks with the fresh health object
                    for callback in self._health_callbacks:
                        try:
                            callback(health)
                        except Exception as e:
                            logger.warning(f"Health callback error: {e}")

            except Exception as e:
                logger.warning(f"Health monitor error: {e}")

            self._stop_health_monitor.wait(self._health_check_interval)

    def get_available_channels(self) -> List[Tuple[str, bool]]:
        """
        Get list of available channels and their status.

        Returns:
            List of (channel_type, is_connected) tuples.
        """
        return [
            (ch.get_type(), ch.is_connected())
            for ch in self._channels
        ]

    def __enter__(self) -> "ChannelManager":
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.disconnect()


def create_channel_manager(
    prefer_hdc: bool = True,
    hdc_serial: Optional[str] = None,
    adb_serial: Optional[str] = None,
    mode: str = "AUTO"
) -> ChannelManager:
    """
    Factory function to create a configured ChannelManager.

    Args:
        prefer_hdc: If True, HDC has higher priority than ADB.
        hdc_serial: HDC device serial number (optional).
        adb_serial: ADB device serial number (optional).
        mode: Operation mode.

    Returns:
        Configured ChannelManager instance.
    """
    manager = ChannelManager(mode=mode)

    # Add channels with priority
    hdc_priority = 10 if prefer_hdc else 5
    adb_priority = 5 if prefer_hdc else 10

    hdc = HDCChannel(serial=hdc_serial)
    adb = ADBChannel(serial=adb_serial)

    manager.add_channel(hdc, priority=hdc_priority)
    manager.add_channel(adb, priority=adb_priority)

    return manager

"""
Serial Port Manager - L1/L2: Serial Port Discovery & Pairing

Provides serial port discovery on both PC and device sides,
and automatic pairing of device-to-PC serial connections.

Architecture:
- PCSerialScanner: Lists available serial ports on PC
- DeviceSerialScanner: Lists serial ports on device via channel
- SerialPairingEngine: Tests device-to-PC port combinations
- SerialPortManager: High-level interface for port management

Part of the 5-layer architecture defined in ARCHITECTURE.md.

Pairing Flow (Optimized):
1. Scan PC ports (pyserial) - list all available
2. Scan Device ports (via HDC/ADB channel) - list all writable
3. Start parallel monitoring on ALL PC ports (thread per port)
4. For each device port:
   - Write marker to device port
   - Check ALL monitored PC ports for the marker
   - If ANY PC port receives it -> pairing found
5. Stop all monitors and return result

Author: Vmin Judge Tool Development
Version: 2.4 - Fixed critical wait timing bug
"""

from __future__ import annotations

import time
import logging
import re
import shlex
import threading
import queue
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any, TYPE_CHECKING, Callable
from pathlib import Path

# Configure module logger
logger = logging.getLogger(__name__)

# Conditionally import pyserial at module level for PyInstaller static analysis
# This makes pyserial detectable during packaging without breaking runtime
try:
    import serial
    import serial.tools.list_ports
    PYSERIAL_AVAILABLE = True
except ImportError:
    PYSERIAL_AVAILABLE = False
    # Create dummy references to satisfy static analysis
    serial = None


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class PCSerialPort:
    """Represents a serial port on the PC side."""
    port: str                    # e.g., "COM3" or "/dev/ttyUSB0"
    description: str = ""        # Human-readable description
    hwid: str = ""              # Hardware ID
    available: bool = True      # Is port accessible
    tested: bool = False        # Has been tested for connectivity
    last_test_success: bool = False
    last_test_latency_ms: float = 0.0


@dataclass
class DeviceSerialPort:
    """Represents a serial port on the device side."""
    port: str                    # e.g., "/dev/ttyAMA0"
    readable: bool = False       # Has read permission
    writable: bool = False       # Has write permission
    is_console: bool = False     # Is active console
    device_type: str = ""         # "ttyAMA", "ttyUSB", "ttyACM", "ttyS"


@dataclass
class PortPair:
    """Represents a paired device-to-PC serial connection."""
    device_port: str             # "/dev/ttyAMA0"
    pc_port: str                 # "COM4"
    confidence: float = 0.0        # 0.0 - 1.0 (higher = better)
    latency_ms: float = 0.0       # Response latency in milliseconds
    test_marker: str = ""         # Marker used for testing


@dataclass
class PairingDiagnostic:
    """Bounded, device-agnostic evidence for one pairing transaction."""
    code: str = "NOT_RUN"
    device_port: Optional[str] = None
    pc_port: Optional[str] = None
    baudrate: Optional[int] = None
    marker_writes: int = 0
    bytes_received: int = 0
    rx_preview_hex: str = ""
    remote_status: Optional[int] = None
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "device_port": self.device_port,
            "pc_port": self.pc_port,
            "baudrate": self.baudrate,
            "marker_writes": self.marker_writes,
            "bytes_received": self.bytes_received,
            "rx_preview_hex": self.rx_preview_hex,
            "remote_status": self.remote_status,
            "detail": self.detail,
        }


@dataclass
class PairingResult:
    """Result of a pairing attempt."""
    success: bool = False
    pair: Optional[PortPair] = None
    error: Optional[str] = None
    device_ports_found: List[DeviceSerialPort] = field(default_factory=list)
    pc_ports_found: List[PCSerialPort] = field(default_factory=list)
    attempts: int = 0
    duration_sec: float = 0.0
    failure_code: Optional[str] = None
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def device_port(self) -> Optional[str]:
        """Get device port from pair if successful."""
        return self.pair.device_port if self.pair else None

    @property
    def pc_port(self) -> Optional[str]:
        """Get PC port from pair if successful."""
        return self.pair.pc_port if self.pair else None


@dataclass
class SerialPortConfig:
    """Configuration for serial port pairing operations."""
    # Discovery options
    auto_discover: bool = True
    auto_pair: bool = True

    # Explicit port specification (bypasses discovery if set)
    explicit_device_port: Optional[str] = None
    explicit_pc_port: Optional[str] = None

    # Serial parameters
    baudrate: int = 9600
    timeout_sec: float = 2.0
    settle_time_sec: float = 0.35
    marker_retries: int = 3
    marker_retry_interval_sec: float = 0.35
    diagnostic_bytes: int = 128

    # Optional operator/platform fallbacks. Empty by default so the generic
    # engine never guesses device-specific nodes or host port numbers.
    fallback_device_ports: List[str] = field(default_factory=list)
    fallback_pc_ports: List[str] = field(default_factory=list)

    # Pairing parameters
    max_pairing_attempts: int = 20
    min_confidence_threshold: float = 0.5

    def __post_init__(self):
        """Validate generic transaction settings without injecting device data."""
        if self.baudrate <= 0:
            raise ValueError("baudrate must be positive")
        if self.timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        if self.settle_time_sec < 0:
            raise ValueError("settle_time_sec must be non-negative")
        if self.marker_retries < 1:
            raise ValueError("marker_retries must be at least 1")
        if self.marker_retry_interval_sec <= 0:
            raise ValueError("marker_retry_interval_sec must be positive")
        if self.diagnostic_bytes < 0:
            raise ValueError("diagnostic_bytes must be non-negative")
# =============================================================================
# PC Serial Scanner
# =============================================================================

class PCSerialScanner(ABC):
    """
    Abstract base class for PC serial port scanning.

    Use MockPCSerialScanner for testing without hardware.
    """

    @abstractmethod
    def list_ports(self) -> List[PCSerialPort]:
        """
        List all available serial ports on the PC.

        Returns:
            List of PCSerialPort objects with available ports.
        """
        pass

    @abstractmethod
    def is_port_available(self, port_name: str) -> bool:
        """
        Check if a specific port is available.

        Args:
            port_name: Port to check (e.g., "COM3").

        Returns:
            True if port exists and is accessible.
        """
        pass


class RealPCSerialScanner(PCSerialScanner):
    """
    Real PC serial port scanner using pyserial.

    Requires pyserial to be installed.
    """

    def __init__(self):
        self._cached_ports: Optional[List[PCSerialPort]] = None

    def list_ports(self) -> List[PCSerialPort]:
        """List all available serial ports using pyserial."""
        if not PYSERIAL_AVAILABLE:
            logger.warning("pyserial not installed, cannot list PC ports")
            return []

        ports = []
        for port_info in serial.tools.list_ports.comports():
            description = port_info.description or ""
            ignored_tokens = (
                "modem",
                "bluetooth",
                "pcui",
                "mobile connect",
                "active management technology",
            )
            if any(token in description.casefold() for token in ignored_tokens):
                logger.debug("Skipping unrelated PC port %s (%s)", port_info.device, description)
                continue
            port = PCSerialPort(
                port=port_info.device,
                description=description,
                hwid=port_info.hwid or "",
                available=True,
            )
            ports.append(port)
            logger.debug(f"Found PC port: {port.port} - {port.description}")

        self._cached_ports = ports
        return ports

    def is_port_available(self, port_name: str) -> bool:
        """Check if a specific port is available."""
        if self._cached_ports is None:
            self.list_ports()

        return any(p.port == port_name and p.available for p in self._cached_ports)


class MockPCSerialScanner(PCSerialScanner):
    """
    Mock PC serial port scanner for testing.

    Simulates PC port discovery without requiring pyserial or real ports.
    """

    def __init__(self, mock_ports: Optional[List[str]] = None):
        """
        Initialize mock scanner.

        Args:
            mock_ports: List of port names to simulate (default: ["COM3", "COM4"])
        """
        self._mock_ports = mock_ports or ["COM3", "COM4", "COM5"]
        self._available = True

    def list_ports(self) -> List[PCSerialPort]:
        """Return deterministic configured ports without touching host hardware."""
        return [PCSerialPort(port=name, description="Mock serial port", available=self._available) for name in self._mock_ports]

    def is_port_available(self, port_name: str) -> bool:
        """Check if mock port is available."""
        return self._available and port_name in self._mock_ports

    def set_available(self, available: bool) -> None:
        """Enable/disable mock port availability."""
        self._available = available


# =============================================================================
# Device Serial Scanner
# =============================================================================

class DeviceSerialScanner(ABC):
    """
    Abstract base class for device serial port scanning.

    Scans for serial ports on the embedded device via channel.
    """

    @abstractmethod
    def list_device_ports(self, channel: Optional['DeviceChannel'] = None) -> List[DeviceSerialPort]:
        """
        List available serial ports on the device.

        Args:
            channel: DeviceChannel (HDCChannel or ADBChannel) for communication.
                    Can be None for mock implementations.

        Returns:
            List of DeviceSerialPort objects found on device.
        """
        pass


class RealDeviceSerialScanner(DeviceSerialScanner):
    """
    Real device serial port scanner via HDC/ADB channel.

    Executes shell commands on device to discover serial ports.
    """

    def list_device_ports(self, channel: Optional['DeviceChannel'] = None) -> List[DeviceSerialPort]:
        """
        Discover serial ports on device via channel.

        Performs:
        1. List all tty devices: ls -la /dev/tty*
        2. Check read permissions: test -r <port>
        3. Check write permissions: test -w <port>
        4. Check if console: grep <port> /proc/consoles
        """
        if channel is None or not channel.is_connected():
            logger.warning("Channel not connected, cannot scan device ports")
            return []

        ports = []

        # Step 1: List tty devices
        code, stdout, stderr = channel.invoke(
            "ls -la /dev/tty* 2>/dev/null",
            timeout=10
        )

        if code != 0:
            logger.warning(f"Failed to list device ports: {stderr}")
            return []

        # Parse ls output to find tty devices
        tty_devices = set()
        for line in stdout.strip().split('\n'):
            if not line:
                continue
            # Parse: crw-rw---- 1 root root 204, 64 /dev/ttyAMA0
            match = re.search(r'/dev/(tty[^\s]+)', line)
            if match:
                tty_devices.add(f"/dev/{match.group(1)}")

        # Step 2-4: Check properties of each device
        for device_path in tty_devices:
            port = DeviceSerialPort(port=device_path)

            # Determine device type
            if 'AMA' in device_path:
                port.device_type = 'ttyAMA'
            elif 'ACM' in device_path:
                port.device_type = 'ttyACM'
            elif 'USB' in device_path:
                port.device_type = 'ttyUSB'
            elif 'ttyS' in device_path:
                port.device_type = 'ttyS'
            elif 'HS' in device_path:
                port.device_type = 'ttyHS'
            else:
                port.device_type = 'other'

            # Check read permission
            code, stdout, _ = channel.invoke(f"test -r {device_path} && echo READABLE", timeout=5)
            port.readable = (code == 0 and "READABLE" in stdout)

            # Check write permission
            code, stdout, _ = channel.invoke(f"test -w {device_path} && echo WRITABLE", timeout=5)
            port.writable = (code == 0 and "WRITABLE" in stdout)

            # Check if it's the console
            code, consoles, _ = channel.invoke("cat /proc/consoles 2>/dev/null", timeout=5)
            # FIX: device_path contains '/dev/', but /proc/consoles does not. Match base name.
            base_dev_name = device_path.replace('/dev/', '')
            port.is_console = base_dev_name in consoles

            if 'HwGS' in device_path:
                logger.debug(f"Skipping USB Gadget port: {device_path}")
                continue
            
            ports.append(port)
            status = "✓" if port.writable else "✗"
            logger.debug(
                f"Device port {status} {port.port}: "
                f"type={port.device_type}, readable={port.readable}, "
                f"writable={port.writable}, console={port.is_console}"
            )

        return ports


class MockDeviceSerialScanner(DeviceSerialScanner):
    """
    Mock device serial port scanner for testing.

    Returns simulated device ports without requiring real device.
    """

    def __init__(
        self,
        mock_ports: Optional[List[str]] = None,
        mock_console: Optional[str] = None
    ):
        """
        Initialize mock scanner.

        Args:
            mock_ports: List of device port names to simulate.
            mock_console: Port that should be marked as console.
        """
        self._mock_ports = mock_ports or ['/dev/ttyAMA0', '/dev/ttyUSB0']
        self._mock_console = mock_console or '/dev/ttyAMA0'

    def list_device_ports(self, channel: Optional['DeviceChannel'] = None) -> List[DeviceSerialPort]:
        """Return mock device port list."""
        ports = []
        for port_name in self._mock_ports:
            # Determine device type
            if 'AMA' in port_name:
                device_type = 'ttyAMA'
            elif 'ACM' in port_name:
                device_type = 'ttyACM'
            elif 'USB' in port_name:
                device_type = 'ttyUSB'
            elif 'ttyS' in port_name:
                device_type = 'ttyS'
            else:
                device_type = 'other'

            port = DeviceSerialPort(
                port=port_name,
                readable=True,
                writable=True,
                is_console=(port_name == self._mock_console),
                device_type=device_type
            )
            ports.append(port)
        return ports


# =============================================================================
# Serial Pairing Engine
# =============================================================================

class SerialPairingEngine(ABC):
    """
    Abstract base class for serial port pairing.

    Tests device-to-PC port combinations to find working pairs.
    """

    @abstractmethod
    def test_pair(
        self,
        device_port: str,
        pc_port: str,
        channel,
        baudrate: int = 9600,
        timeout: float = 2.0,
        *,
        settle_time_sec: float = 0.35,
        marker_retries: int = 3,
        marker_retry_interval_sec: float = 0.35,
        diagnostic_bytes: int = 128,
    ) -> Tuple[bool, float]:
        """
        Test a specific device-to-PC port pairing.

        Args:
            device_port: Device serial port (e.g., "/dev/ttyAMA0").
            pc_port: PC serial port (e.g., "COM3").
            channel: DeviceChannel for device communication.
            baudrate: Serial baud rate.
            timeout: Test timeout in seconds.

        Returns:
            Tuple of (success, latency_ms)
        """
        pass

    @abstractmethod
    def auto_pair(
        self,
        pc_scanner: PCSerialScanner,
        device_ports: List[DeviceSerialPort],
        channel,
        config: SerialPortConfig
    ) -> PairingResult:
        """
        Automatically discover and pair serial ports.

        Args:
            pc_scanner: PC serial port scanner.
            device_ports: List of device ports to test.
            channel: DeviceChannel for device communication.
            config: Pairing configuration.

        Returns:
            PairingResult with best pair found.
        """
        pass


class RealSerialPairingEngine(SerialPairingEngine):
    """
    Real serial pairing engine with echo test.

    Algorithm:
    1. Generate unique marker
    2. Write marker to device port via channel
    3. Monitor PC port for marker
    4. If received within timeout -> pairing success
    """

    def __init__(self, pc_monitor=None):
        """
        Initialize pairing engine.

        Args:
            pc_monitor: PCSerialMonitor for receiving markers on PC side.
                       If None, uses MockPCSerialMonitor.
        """
        self._pc_monitor = pc_monitor
        self._lock = threading.Lock()

    def test_pair(
        self,
        device_port: str,
        pc_port: str,
        channel,
        baudrate: int = 9600,
        timeout: float = 2.0,
        *,
        settle_time_sec: float = 0.35,
        marker_retries: int = 3,
        marker_retry_interval_sec: float = 0.35,
        diagnostic_bytes: int = 128,
    ) -> Tuple[bool, float]:
        """
        Test a device-to-PC port pairing using echo test.

        Flow:
        1. Generate unique test marker
        2. Write marker to device serial port (via HDC/ADB shell)
        3. Wait for marker on PC serial port
        4. If received -> success with latency
        """
        if not PYSERIAL_AVAILABLE:
            logger.warning("pyserial not installed, cannot test port pairing")
            return (False, 0.0)

        marker = f"PAIR_{int(time.time() * 1000)}"
        write_cmd = f"echo {marker} > {device_port}"
        
        write_start_time = time.time()

        try:
            # Step 1: Write marker to device serial port via channel
            code, _, stderr = channel.invoke(write_cmd, timeout=5)

            if code != 0:
                logger.debug(f"Failed to write to device port {device_port}: {stderr}")
                return (False, 0.0)

            # Step 2: Monitor PC serial port for marker
            try:
                with serial.Serial(pc_port, baudrate, timeout=timeout) as ser:
                    # Wait for marker to arrive
                    buffer = bytearray()
                    # FIX: Ensure we wait for full timeout duration AFTER invoke returns
                    deadline = time.time() + timeout

                    while time.time() < deadline:
                        if ser.in_waiting > 0:
                            data = ser.read(ser.in_waiting)
                            buffer.extend(data)

                            # Check if marker is in buffer
                            if marker.encode() in buffer:
                                latency_ms = (time.time() - write_start_time) * 1000
                                logger.info(
                                    f"Pair test SUCCESS: {device_port} <-> {pc_port} "
                                    f"(latency: {latency_ms:.1f}ms)"
                                )
                                return (True, latency_ms)

                        time.sleep(0.01)  # Small sleep to avoid busy loop

                    # Timeout - marker not received
                    logger.debug(f"Pair test TIMEOUT: {device_port} <-> {pc_port}")
                    return (False, 0.0)

            except serial.SerialException as e:
                logger.debug(f"PC serial error on {pc_port}: {e}")
                return (False, 0.0)

        except Exception as e:
            logger.warning(f"Pair test ERROR: {device_port} <-> {pc_port}: {e}")
            return (False, 0.0)

    def auto_pair(
        self,
        pc_scanner: PCSerialScanner,
        device_ports: List[DeviceSerialPort],
        channel,
        config: SerialPortConfig
    ) -> PairingResult:
        """
        Automatically find best device-to-PC port pair.

        Algorithm:
        1. If explicit ports configured, test them first
        2. Get available PC ports
        3. For each device port:
           - For each PC port:
             - Test connection
             - Record success and latency
        4. Return best pair by confidence/latency
        """
        start_time = time.time()
        result = PairingResult()
        result.device_ports_found = device_ports

        # Step 1: Get PC ports
        pc_ports = pc_scanner.list_ports()
        result.pc_ports_found = pc_ports

        if not pc_ports:
            result.error = "No PC serial ports found"
            logger.debug(result.error)
            return result

        # Step 2: If explicit ports configured, test them first
        if config.explicit_device_port and config.explicit_pc_port:
            logger.info(f"Testing explicit ports: {config.explicit_device_port} <-> {config.explicit_pc_port}")
            success, latency = self.test_pair(
                config.explicit_device_port,
                config.explicit_pc_port,
                channel,
                config.baudrate,
                config.timeout_sec
            )
            result.attempts = 1

            if success:
                result.success = True
                result.pair = PortPair(
                    device_port=config.explicit_device_port,
                    pc_port=config.explicit_pc_port,
                    confidence=1.0,
                    latency_ms=latency,
                    test_marker=f"PASS_{int(time.time())}"
                )
                result.duration_sec = time.time() - start_time
                return result
            else:
                logger.warning("Explicit ports failed, trying auto-discovery")

        # Step 3: Filter writable, non-console device ports
        writable_device_ports = [
            p for p in device_ports 
            if p.writable and not p.is_console
        ]

        if not writable_device_ports:
            result.error = "No writable non-console device serial ports found"
            logger.debug(result.error)
            return result

        # Step 4: Test all combinations
        candidates = []

        for device_port in writable_device_ports:
            for pc_port_obj in pc_ports:
                if not pc_port_obj.available:
                    continue

                pc_port_name = pc_port_obj.port
                result.attempts += 1

                if result.attempts > config.max_pairing_attempts:
                    logger.warning(f"Max pairing attempts ({config.max_pairing_attempts}) reached")
                    break

                logger.debug(f"Testing pair: {device_port.port} <-> {pc_port_name}")

                success, latency = self.test_pair(
                    device_port.port,
                    pc_port_name,
                    channel,
                    config.baudrate,
                    config.timeout_sec
                )

                if success:
                    # Calculate confidence based on latency
                    # Lower latency = higher confidence
                    confidence = max(0.0, 1.0 - (latency / 1000.0))

                    candidates.append(PortPair(
                        device_port=device_port.port,
                        pc_port=pc_port_name,
                        confidence=confidence,
                        latency_ms=latency,
                        test_marker=f"PAIR_{int(time.time())}"
                    ))

                    logger.info(f"Found working pair: {device_port.port} <-> {pc_port_name}")
                    break
            if candidates:
                break

        # Step 5: Select best candidate
        if candidates:
            # Sort by confidence (desc) then latency (asc)
            candidates.sort(key=lambda p: (-p.confidence, p.latency_ms))

            best = candidates[0]
            if best.confidence >= config.min_confidence_threshold:
                result.success = True
                result.pair = best
            else:
                result.error = f"No pair met confidence threshold ({config.min_confidence_threshold})"
        else:
            result.error = "No working pairs found"

        result.duration_sec = time.time() - start_time

        logger.info(
            f"Pairing complete: success={result.success}, "
            f"attempts={result.attempts}, "
            f"duration={result.duration_sec:.2f}s"
        )

        return result


# =============================================================================
# Optimized Parallel Serial Pairing Engine
# =============================================================================

class PCSerialPortMonitor:
    """
    Monitors a single PC serial port for incoming data.

    Runs in a separate thread, continuously reading from the serial port
    and storing received data in a thread-safe buffer.
    """

    def __init__(self, port_name: str, baudrate: int = 9600, timeout: float = 10.0):
        """
        Initialize PC port monitor.

        Args:
            port_name: PC serial port (e.g., "COM4")
            baudrate: Serial baud rate
            timeout: Monitor timeout in seconds
        """
        self.port_name = port_name
        self.baudrate = baudrate
        self.timeout = timeout
        self._buffer: bytearray = bytearray()
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> bool:
        """
        Start monitoring the port.

        Returns:
            True if started successfully, False otherwise.
        """
        if self._running:
            logger.debug(f"Monitor {self.port_name} already running")
            return True

        if not PYSERIAL_AVAILABLE:
            logger.warning(f"Cannot start monitor {self.port_name}: pyserial not available")
            return False

        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.debug(f"Started monitor for PC port: {self.port_name}")
        return True

    def stop(self) -> None:
        """Stop monitoring and clean up resources."""
        if not self._running:
            return

        self._running = False
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        logger.debug(f"Stopped monitor for PC port: {self.port_name}")

    def is_running(self) -> bool:
        """Check if monitor is still running."""
        return self._running

    def get_data(self) -> str:
        """
        Get all received data as string.

        Returns:
            All data received since last get_data() call, decoded as string.
        """
        with self._lock:
            data = bytes(self._buffer).decode('utf-8', errors='replace')
            self._buffer.clear()
            return data

    def peek_data(self) -> str:
        """
        Peek at received data without clearing buffer.

        Returns:
            All data in buffer as string.
        """
        with self._lock:
            return bytes(self._buffer).decode('utf-8', errors='replace')

    def contains(self, marker: str) -> bool:
        """
        Check if buffer contains a specific marker.

        Args:
            marker: String to search for in buffer.

        Returns:
            True if marker found in buffer.
        """
        with self._lock:
            data = bytes(self._buffer).decode('utf-8', errors='replace')
            found = marker in data
            if found:
                logger.debug("Monitor %s received pair marker", self.port_name)
            return found

    def snapshot(self) -> bytes:
        """Return a thread-safe copy of buffered bytes without clearing them."""
        with self._lock:
            return bytes(self._buffer)

    def _monitor_loop(self) -> None:
        """Main monitoring loop - runs in separate thread."""
        try:
            with serial.Serial(self.port_name, self.baudrate, timeout=0.1) as ser:
                logger.debug("Monitor %s opened", self.port_name)

                while self._running and not self._stop_event.is_set():
                    try:
                        if ser.in_waiting > 0:
                            data = ser.read(ser.in_waiting)
                            with self._lock:
                                self._buffer.extend(data)

                                # Log data at DEBUG level
                                data_str = data.decode('utf-8', errors='replace').strip()
                                if data_str:
                                    logger.debug(f"Monitor {self.port_name}: RX: {repr(data_str[:100])}")

                        # Small sleep to avoid busy loop
                        time.sleep(0.01)

                    except serial.SerialException as e:
                        logger.warning(f"Monitor {self.port_name}: Serial error: {e}")
                        break

        except serial.SerialException as e:
            logger.warning(f"Monitor {self.port_name}: Failed to open port: {e}")
            self._running = False
        except Exception as e:
            logger.warning(f"Monitor {self.port_name}: Unexpected error: {e}")
            self._running = False


class OptimizedSerialPairingEngine(SerialPairingEngine):
    """
    Optimized serial pairing engine with parallel PC port monitoring.

    Algorithm:
    1. Start monitors on ALL PC ports in parallel (thread per port)
    2. For each device port:
       a. Generate unique marker
       b. Write marker to device port via HDC/ADB
       c. Check ALL PC monitors for the marker
       d. If ANY monitor receives it -> pairing found!
    3. Stop all monitors and return result

    This is O(device_ports) instead of O(device_ports × pc_ports),
    and uses parallel I/O for maximum efficiency.

    Version: 2.4
    """

    def __init__(self, pc_monitor=None):
        """
        Initialize optimized pairing engine.

        Args:
            pc_monitor: Optional PCSerialMonitor (unused in this implementation)
        """
        self._monitors: Dict[str, PCSerialPortMonitor] = {}
        self._lock = threading.Lock()
        self.last_diagnostic = PairingDiagnostic()

    def test_pair(
        self,
        device_port: str,
        pc_port: str,
        channel,
        baudrate: int = 9600,
        timeout: float = 2.0,
        *,
        settle_time_sec: float = 0.35,
        marker_retries: int = 3,
        marker_retry_interval_sec: float = 0.35,
        diagnostic_bytes: int = 128,
    ) -> Tuple[bool, float]:
        """
        Test a device-to-PC port pairing.

        Note: For optimized pairing, use auto_pair() instead.
        This method is kept for backward compatibility.
        """
        # Fall back to original method
        return self._test_pair_original(
            device_port,
            pc_port,
            channel,
            baudrate,
            timeout,
            settle_time_sec=settle_time_sec,
            marker_retries=marker_retries,
            marker_retry_interval_sec=marker_retry_interval_sec,
            diagnostic_bytes=diagnostic_bytes,
        )

    def _test_pair_original(
        self,
        device_port: str,
        pc_port: str,
        channel,
        baudrate: int = 9600,
        timeout: float = 2.0,
        *,
        settle_time_sec: float = 0.35,
        marker_retries: int = 3,
        marker_retry_interval_sec: float = 0.35,
        diagnostic_bytes: int = 128,
    ) -> Tuple[bool, float]:
        """Run a bounded open-settle-send/read transaction for one port pair."""
        diagnostic = PairingDiagnostic(
            device_port=device_port,
            pc_port=pc_port,
            baudrate=baudrate,
        )
        self.last_diagnostic = diagnostic
        if not PYSERIAL_AVAILABLE:
            logger.warning("pyserial not installed, cannot test port pairing")
            diagnostic.code = "PYSERIAL_UNAVAILABLE"
            return (False, 0.0)

        try:
            try:
                with serial.Serial(pc_port, baudrate, timeout=0.1) as ser:
                    if settle_time_sec:
                        time.sleep(settle_time_sec)
                    if hasattr(ser, "reset_input_buffer"):
                        ser.reset_input_buffer()
                    marker = f"PAIR_{time.time_ns()}"
                    write_cmd = f"echo {marker} > {shlex.quote(device_port)}"
                    buffer = bytearray()
                    write_start_time = time.time()
                    deadline: Optional[float] = None
                    next_write_at = write_start_time

                    while deadline is None or time.time() < deadline:
                        now = time.time()
                        if diagnostic.marker_writes < marker_retries and now >= next_write_at:
                            code, _, stderr = channel.invoke(write_cmd, timeout=max(1.0, min(5.0, timeout)))
                            diagnostic.remote_status = code
                            if code != 0:
                                diagnostic.code = "DEVICE_ECHO_FAILED"
                                diagnostic.detail = (stderr.strip() or f"exit {code}")[:256]
                                logger.error(
                                    "Device UART write failed on %s: %s",
                                    device_port,
                                    diagnostic.detail,
                                )
                                return (False, 0.0)
                            diagnostic.marker_writes += 1
                            if deadline is None:
                                deadline = time.time() + timeout
                            next_write_at = time.time() + marker_retry_interval_sec

                        if ser.in_waiting > 0:
                            data = ser.read(ser.in_waiting)
                            buffer.extend(data)
                            diagnostic.bytes_received = len(buffer)
                            diagnostic.rx_preview_hex = bytes(buffer[:diagnostic_bytes]).hex()

                            if marker.encode() in buffer:
                                latency_ms = (time.time() - write_start_time) * 1000
                                diagnostic.code = "SUCCESS"
                                logger.debug("Pair marker received on %s", pc_port)
                                return (True, latency_ms)

                        time.sleep(0.01)

                    diagnostic.code = "NO_RX_BYTES" if not buffer else "MARKER_NOT_FOUND"
                    return (False, 0.0)

            except serial.SerialException as e:
                diagnostic.code = self._classify_pc_port_error(e)
                diagnostic.detail = str(e)[:256]
                logger.error("Cannot use PC serial port %s: %s", pc_port, e)
                return (False, 0.0)

        except Exception as e:
            diagnostic.code = "PAIR_INTERNAL_ERROR"
            diagnostic.detail = str(e)[:256]
            logger.error("Pair test failed for %s <-> %s: %s", device_port, pc_port, e)
            return (False, 0.0)

    @staticmethod
    def _classify_pc_port_error(error: Exception) -> str:
        message = str(error).lower()
        if any(token in message for token in ("access is denied", "access denied", "permission", "busy", "in use")):
            return "PC_PORT_BUSY"
        return "PC_PORT_OPEN_FAILED"

    def auto_pair(
        self,
        pc_scanner: PCSerialScanner,
        device_ports: List[DeviceSerialPort],
        channel,
        config: SerialPortConfig
    ) -> PairingResult:
        """
        Optimized auto-pairing with parallel PC port monitoring.

        Flow:
        1. Get all PC ports
        2. Start monitors on ALL PC ports in parallel
        3. For each device port:
           - Write marker to device
           - Check all monitors
           - If found -> success!
        4. Stop all monitors
        5. Return result
        """
        start_time = time.time()
        result = PairingResult()
        result.device_ports_found = device_ports

        if not device_ports:
            result.failure_code = "NO_DEVICE_PORTS"
            result.error = "No writable, non-console device serial candidates were found"
            result.duration_sec = time.time() - start_time
            return result

        if config.explicit_device_port and config.explicit_pc_port:
            result.pc_ports_found = [PCSerialPort(port=config.explicit_pc_port, description="Explicit PC port")]
            success, latency = self.test_pair(
                config.explicit_device_port,
                config.explicit_pc_port,
                channel,
                config.baudrate,
                config.timeout_sec,
                settle_time_sec=config.settle_time_sec,
                marker_retries=config.marker_retries,
                marker_retry_interval_sec=config.marker_retry_interval_sec,
                diagnostic_bytes=config.diagnostic_bytes,
            )
            diagnostic = self.last_diagnostic.to_dict()
            result.diagnostics.append(diagnostic)
            result.attempts = 1
            result.duration_sec = time.time() - start_time
            if success:
                result.success = True
                result.pair = PortPair(
                    device_port=config.explicit_device_port,
                    pc_port=config.explicit_pc_port,
                    confidence=1.0,
                    latency_ms=latency,
                )
            else:
                result.failure_code = diagnostic["code"]
                result.error = (
                    f"{result.failure_code}: {config.explicit_device_port} -> "
                    f"{config.explicit_pc_port} at {config.baudrate} baud; "
                    f"writes={diagnostic['marker_writes']}, rx_bytes={diagnostic['bytes_received']}"
                )
            return result

        # Step 1: Get PC ports
        pc_ports = (
            [PCSerialPort(port=config.explicit_pc_port, description="Explicit PC port")]
            if config.explicit_pc_port
            else pc_scanner.list_ports()
        )
        result.pc_ports_found = pc_ports

        if not pc_ports:
            result.failure_code = "NO_PC_PORTS"
            result.error = "No PC serial ports found"
            logger.warning(result.error)
            return result

        logger.debug("Pair candidates: %d device ports x %d PC ports", len(device_ports), len(pc_ports))

        # Step 2: Start monitors on ALL PC ports
        monitors_started = 0
        for pc_port_obj in pc_ports:
            if not pc_port_obj.available:
                logger.debug(f"SKIP PC port {pc_port_obj.port}: not available")
                continue

            monitor = PCSerialPortMonitor(
                pc_port_obj.port,
                baudrate=config.baudrate,
                timeout=config.timeout_sec
            )

            if monitor.start():
                self._monitors[pc_port_obj.port] = monitor
                monitors_started += 1
            else:
                logger.debug(f"SKIP PC port {pc_port_obj.port}: failed to start monitor")

        if not self._monitors:
            result.failure_code = "PC_PORT_OPEN_FAILED"
            result.error = "Failed to start any PC port monitors"
            logger.warning(result.error)
            return result

        logger.debug("Started %d/%d PC serial monitors", monitors_started, len(pc_ports))

        # Give monitors time to open and be ready
        time.sleep(max(0.05, config.settle_time_sec))

        active_monitors = {}
        failed_to_open = []
        for port, mon in self._monitors.items():
            if mon.is_running():
                active_monitors[port] = mon
            else:
                failed_to_open.append(port)
        
        self._monitors = active_monitors
        
        if failed_to_open:
            logger.debug("PC serial ports unavailable: %s", failed_to_open)
            
        if not self._monitors:
            result.failure_code = "PC_PORT_OPEN_FAILED"
            result.error = "Failed to open ANY PC serial ports. Please close other serial tools (like SecureCRT) and try again."
            logger.error(result.error)
            return result
            
        logger.debug("Active PC serial monitors: %s", list(self._monitors.keys()))
        # Step 3: Test each device port
        candidates = []
        test_attempt = 0

        try:
            for device_port in device_ports:
                logger.debug("Testing device port %s", device_port.port)
                logger.debug(f"Device port info: type={device_port.device_type}, "
                            f"readable={device_port.readable}, writable={device_port.writable}, "
                            f"console={device_port.is_console}")

                # Skip non-writable ports
                if not device_port.writable:
                    logger.debug("Skipping non-writable device port %s", device_port.port)
                    continue

                # Skip console ports correctly identified
                if device_port.is_console:
                    logger.debug("Skipping active console %s", device_port.port)
                    continue

                # Generate unique marker for this device port
                test_attempt += 1
                marker = f"PAIR_{int(time.time() * 1000)}_{test_attempt}"

                logger.debug("Sending pair marker through %s", device_port.port)

                write_cmd = f"echo {marker} > {shlex.quote(device_port.port)}"
                monitor_offsets = {
                    pc_port_name: len(monitor.snapshot())
                    for pc_port_name, monitor in self._monitors.items()
                }
                attempt_diagnostic: Dict[str, Any] = {
                    "code": "NOT_RUN",
                    "device_port": device_port.port,
                    "pc_port": None,
                    "baudrate": config.baudrate,
                    "marker_writes": 0,
                    "bytes_received": 0,
                    "rx_preview_hex": "",
                    "remote_status": None,
                    "detail": None,
                }
                write_start_time = time.time()
                found_on_port = None
                found_latency_ms = 0.0
                wait_deadline: Optional[float] = None
                next_write_at = write_start_time
                device_write_failed = False

                while wait_deadline is None or time.time() < wait_deadline:
                    now = time.time()
                    if attempt_diagnostic["marker_writes"] < config.marker_retries and now >= next_write_at:
                        code, _, stderr = channel.invoke(
                            write_cmd,
                            timeout=max(1.0, min(5.0, config.timeout_sec)),
                        )
                        attempt_diagnostic["remote_status"] = code
                        if code != 0:
                            attempt_diagnostic["code"] = "DEVICE_ECHO_FAILED"
                            attempt_diagnostic["detail"] = (stderr.strip() or f"exit {code}")[:256]
                            logger.error(
                                "Device UART write failed on %s: %s",
                                device_port.port,
                                attempt_diagnostic["detail"],
                            )
                            device_write_failed = True
                            break
                        attempt_diagnostic["marker_writes"] += 1
                        if wait_deadline is None:
                            wait_deadline = time.time() + config.timeout_sec
                        next_write_at = time.time() + config.marker_retry_interval_sec

                    for pc_port_name, monitor in self._monitors.items():
                        if monitor.contains(marker):
                            found_on_port = pc_port_name
                            found_latency_ms = (time.time() - write_start_time) * 1000
                            break
                    if found_on_port:
                        break
                    time.sleep(0.02)

                received_by_port: Dict[str, bytes] = {}
                for pc_port_name, monitor in self._monitors.items():
                    snapshot = monitor.snapshot()
                    received_by_port[pc_port_name] = snapshot[monitor_offsets[pc_port_name]:]
                received_total = sum(len(data) for data in received_by_port.values())
                preview = b"".join(received_by_port.values())[:config.diagnostic_bytes]
                attempt_diagnostic["bytes_received"] = received_total
                attempt_diagnostic["rx_preview_hex"] = preview.hex()

                if found_on_port:
                    # Calculate confidence based on actual latency
                    confidence = max(0.0, 1.0 - (found_latency_ms / 1000.0))
                    confidence = max(confidence, 0.6) 

                    pair = PortPair(
                        device_port=device_port.port,
                        pc_port=found_on_port,
                        confidence=confidence,
                        latency_ms=found_latency_ms,
                        test_marker=marker
                    )
                    candidates.append(pair)
                    attempt_diagnostic["code"] = "SUCCESS"
                    attempt_diagnostic["pc_port"] = found_on_port
                    result.diagnostics.append(attempt_diagnostic)

                    logger.debug("Pair marker matched %s to %s", device_port.port, found_on_port)

                    # Early exit since we found a working pair
                    result.attempts += 1
                    result.success = True
                    result.pair = pair
                    result.duration_sec = time.time() - start_time
                    return result

                else:
                    if not device_write_failed:
                        attempt_diagnostic["code"] = "MARKER_NOT_FOUND" if received_total else "NO_RX_BYTES"
                    result.failure_code = attempt_diagnostic["code"]
                    result.diagnostics.append(attempt_diagnostic)
                    logger.debug("Pair marker not found for device port %s", device_port.port)

                result.attempts += 1

                # Check max attempts
                if result.attempts >= config.max_pairing_attempts:
                    logger.warning(f"Max pairing attempts ({config.max_pairing_attempts}) reached")
                    break

        finally:
            # Step 4: Stop all monitors
            logger.debug("Stopping PC serial monitors")
            for pc_port_name, monitor in self._monitors.items():
                monitor.stop()
            self._monitors.clear()

        # Step 5: Select best pair
        if candidates:
            # Sort by confidence (desc) then latency (asc)
            candidates.sort(key=lambda p: (-p.confidence, p.latency_ms))

            best = candidates[0]
            if best.confidence >= config.min_confidence_threshold:
                result.success = True
                result.pair = best
                logger.debug("Selected pair %s <-> %s", best.device_port, best.pc_port)
            else:
                result.error = f"No pair met confidence threshold ({config.min_confidence_threshold})"
        else:
            result.failure_code = result.failure_code or "NO_WORKING_PAIR"
            result.error = f"{result.failure_code}: no working pairs found"
            logger.debug(result.error)

        result.duration_sec = time.time() - start_time

        logger.debug(
            "Pairing complete: success=%s attempts=%d duration=%.2fs",
            result.success,
            result.attempts,
            result.duration_sec,
        )

        return result


class MockSerialPairingEngine(SerialPairingEngine):
    """
    Mock serial pairing engine for testing.

    Simulates pairing without requiring real serial ports.
    """

    def __init__(
        self,
        working_pairs: Optional[List[Tuple[str, str]]] = None,
        default_latency_ms: float = 10.0
    ):
        """
        Initialize mock pairing engine.

        Args:
            working_pairs: List of (device_port, pc_port) tuples that should succeed.
            default_latency_ms: Default latency for successful pairs.
        """
        self._working_pairs = set(working_pairs or [('/dev/ttyAMA0', 'COM4')])
        self._default_latency = default_latency_ms
        self._call_count = 0

    def test_pair(
        self,
        device_port: str,
        pc_port: str,
        channel=None,
        baudrate: int = 9600,
        timeout: float = 2.0,
        *,
        settle_time_sec: float = 0.35,
        marker_retries: int = 3,
        marker_retry_interval_sec: float = 0.35,
        diagnostic_bytes: int = 128,
    ) -> Tuple[bool, float]:
        """Simulate pair test."""
        self._call_count += 1

        if (device_port, pc_port) in self._working_pairs:
            return (True, self._default_latency)
        return (False, 0.0)

    def auto_pair(
        self,
        pc_scanner: PCSerialScanner,
        device_ports: List[DeviceSerialPort],
        channel=None,
        config: SerialPortConfig = None
    ) -> PairingResult:
        """Simulate auto-pairing."""
        config = config or SerialPortConfig()

        result = PairingResult()
        result.device_ports_found = device_ports
        result.pc_ports_found = pc_scanner.list_ports()

        # Find first working pair, respecting max_attempts
        for device_port in device_ports:
            for pc_port_obj in result.pc_ports_found:
                result.attempts += 1

                # Check max attempts limit
                if result.attempts > config.max_pairing_attempts:
                    result.error = f"Max pairing attempts ({config.max_pairing_attempts}) reached"
                    return result

                if (device_port.port, pc_port_obj.port) in self._working_pairs:
                    result.success = True
                    result.pair = PortPair(
                        device_port=device_port.port,
                        pc_port=pc_port_obj.port,
                        confidence=1.0,
                        latency_ms=self._default_latency,
                        test_marker=f"MOCK_{int(time.time())}"
                    )
                    return result

        result.error = "No working pairs found"
        return result


# =============================================================================
# Serial Port Manager (High-Level Interface)
# =============================================================================

class SerialPortManager:
    """
    High-level serial port management with auto-discovery.

    Provides a simple interface for:
    - Automatic discovery and pairing
    - Explicit port configuration
    - Connection verification
    - Fallback handling

    Usage:
        # Auto-discovery mode
        manager = SerialPortManager(channel=hdс_channel)
        result = manager.discover()
        if result.success:
            print(f"Device: {result.device_port}, PC: {result.pc_port}")

        # Explicit ports mode
        config = SerialPortConfig(
            explicit_device_port="/dev/ttyAMA0",
            explicit_pc_port="COM4"
        )
        manager = SerialPortManager(config=config)
        result = manager.discover()
    """

    def __init__(
        self,
        channel=None,
        config: Optional[SerialPortConfig] = None,
        pc_scanner: Optional[PCSerialScanner] = None,
        device_scanner: Optional[DeviceSerialScanner] = None,
        pairing_engine: Optional[SerialPairingEngine] = None
    ):
        """
        Initialize SerialPortManager.

        Args:
            channel: DeviceChannel (HDCChannel or ADBChannel) for device communication.
            config: SerialPortConfig for pairing configuration.
            pc_scanner: PCSerialScanner (uses MockPCSerialScanner if None).
            device_scanner: DeviceSerialScanner (uses MockDeviceSerialScanner if None).
            pairing_engine: SerialPairingEngine (uses Mock if None).
        """
        self._channel = channel
        self._config = config or SerialPortConfig()

        # Use provided scanners/engines or create mocks
        self._pc_scanner = pc_scanner or MockPCSerialScanner()
        self._device_scanner = device_scanner or MockDeviceSerialScanner()
        self._pairing_engine = pairing_engine or MockSerialPairingEngine()

        # Current paired connection
        self._current_pair: Optional[PortPair] = None
        self._verified = False

    @property
    def channel(self):
        """Get device channel."""
        return self._channel

    @property
    def config(self) -> SerialPortConfig:
        """Get configuration."""
        return self._config

    @property
    def is_connected(self) -> bool:
        """Check if currently paired and verified."""
        return self._current_pair is not None and self._verified

    def discover(self) -> PairingResult:
        """
        Discover and pair serial ports.

        Returns:
            PairingResult with pairing outcome.

        Note:
            If config.auto_discover is False and explicit ports are set,
            only tests the explicit ports.
        """
        if self._config.explicit_device_port:
            device_ports = [
                DeviceSerialPort(
                    port=self._config.explicit_device_port,
                    writable=True,
                    device_type="explicit",
                )
            ]
        else:
            device_ports = self._device_scanner.list_device_ports(self._channel)
        logger.debug("Found %d device serial candidates", len(device_ports))

        if not device_ports and self._config.fallback_device_ports:
            logger.warning("No device ports discovered; using configured fallback candidates")
            device_ports = [
                DeviceSerialPort(port=p, writable=True)
                for p in self._config.fallback_device_ports
            ]

        # Attempt pairing
        result = self._pairing_engine.auto_pair(
            self._pc_scanner,
            device_ports,
            self._channel,
            self._config
        )

        if result.success:
            self._current_pair = result.pair
            self._verified = False  # Needs verification
            logger.debug(f"Paired: {result.device_port} <-> {result.pc_port}")
        else:
            logger.debug(f"Pairing failed: {result.error}")

        return result

    def verify_connection(self) -> bool:
        """
        Verify the current pairing is working.

        Returns:
            True if connection is verified, False otherwise.
        """
        if not self._current_pair:
            logger.warning("No pair to verify")
            return False

        logger.debug(f"Verifying connection: {self._current_pair.device_port} <-> {self._current_pair.pc_port}")

        # Test the pair again
        success, latency = self._pairing_engine.test_pair(
            self._current_pair.device_port,
            self._current_pair.pc_port,
            self._channel,
            self._config.baudrate,
            self._config.timeout_sec,
            settle_time_sec=self._config.settle_time_sec,
            marker_retries=self._config.marker_retries,
            marker_retry_interval_sec=self._config.marker_retry_interval_sec,
            diagnostic_bytes=self._config.diagnostic_bytes,
        )

        self._verified = success

        if success:
            logger.debug(f"Verification SUCCESS (latency: {latency:.1f}ms)")
        else:
            logger.warning("Verification FAILED")

        return success

    def get_device_port(self) -> Optional[str]:
        """Get paired device port."""
        return self._current_pair.device_port if self._current_pair else None

    def get_pc_port(self) -> Optional[str]:
        """Get paired PC port."""
        return self._current_pair.pc_port if self._current_pair else None

    def get_pair(self) -> Optional[PortPair]:
        """Get current port pair."""
        return self._current_pair

    def get_last_diagnostic(self) -> Optional[Dict[str, Any]]:
        """Return bounded evidence from the most recent direct/verification attempt."""
        diagnostic = getattr(self._pairing_engine, "last_diagnostic", None)
        return diagnostic.to_dict() if isinstance(diagnostic, PairingDiagnostic) else None

    def reset(self) -> None:
        """Reset pairing state."""
        self._current_pair = None
        self._verified = False
        logger.info("SerialPortManager reset")


def create_serial_port_manager(
    channel,
    prefer_hdc: bool = True,
    config: Optional[SerialPortConfig] = None,
    use_real_impl: bool = False
) -> SerialPortManager:
    """
    Factory function to create a configured SerialPortManager.

    Args:
        channel: DeviceChannel for device communication.
        prefer_hdc: If True, uses HDC-specific port detection patterns.
        config: Optional configuration.
        use_real_impl: If True, uses Real* implementations (requires pyserial).
                       If False (default), uses Mock* implementations for testing.

    Returns:
        Configured SerialPortManager instance.

    Note:
        By default, this factory returns mock implementations suitable for
        testing. For production use with real hardware, set use_real_impl=True
        or construct SerialPortManager directly with Real* scanner/engine.
    """
    config = config or SerialPortConfig()

    if use_real_impl:
        # Use real implementations (requires pyserial)
        # Use OptimizedSerialPairingEngine for parallel monitoring of all PC ports
        pc_scanner = RealPCSerialScanner()
        device_scanner = RealDeviceSerialScanner()
        pairing_engine = OptimizedSerialPairingEngine()  # v2.4: fixed wait timing
    else:
        # Use mock implementations for testing (default)
        pc_scanner = MockPCSerialScanner()
        device_scanner = MockDeviceSerialScanner()
        pairing_engine = MockSerialPairingEngine()

    return SerialPortManager(
        channel=channel,
        config=config,
        pc_scanner=pc_scanner,
        device_scanner=device_scanner,
        pairing_engine=pairing_engine
    )

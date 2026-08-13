"""
Serial Monitor - L2: Ingestion Layer

Provides non-blocking serial port reading with line buffering.
Used to capture log output from embedded devices.

Part of the 5-layer architecture defined in ARCHITECTURE.md.

Author: Vmin Judge Tool Development
Version: 1.0
"""

from __future__ import annotations

import threading
import logging
import time
from typing import List, Optional, Callable, TYPE_CHECKING
from dataclasses import dataclass, field
from pathlib import Path

# Conditionally import pyserial at module level for PyInstaller static analysis
# This makes pyserial detectable during packaging without breaking runtime
try:
    import serial
    import serial.tools.list_ports
    PYSERIAL_AVAILABLE = True
except ImportError:
    PYSERIAL_AVAILABLE = False
    serial = None

# Configure module logger
logger = logging.getLogger(__name__)


class SerialMonitorError(Exception):
    """Base exception for SerialMonitor errors."""
    pass


class SerialPortNotFoundError(SerialMonitorError):
    """Raised when serial port cannot be opened."""
    pass


class SerialReadError(SerialMonitorError):
    """Raised when serial read fails."""
    pass


# Serial port constants (pyserial 3.x compatible)
# These are commonly used values that work across versions
class SerialBytesize:
    """Serial bytesize constants."""
    FIVEBITS = 5
    SIXBITS = 6
    SEVENBITS = 7
    EIGHTBITS = 8


class SerialParity:
    """Serial parity constants."""
    NONE = 'N'
    EVEN = 'E'
    ODD = 'O'
    MARK = 'M'
    SPACE = 'S'


class SerialStopbits:
    """Serial stopbits constants."""
    ONE = 1
    ONEPOINTFIVE = 1.5
    TWO = 2


@dataclass
class SerialConfig:
    """Serial port configuration."""
    port: str = "/dev/ttyUSB0"
    baudrate: int = 115200
    bytesize: int = SerialBytesize.EIGHTBITS
    parity: str = SerialParity.NONE
    stopbits: float = SerialStopbits.ONE
    timeout: float = 0.001  # 1ms timeout for non-blocking read
    xonxoff: bool = False
    rtscts: bool = False
    dsrdtr: bool = False
    write_timeout: Optional[float] = None

    @classmethod
    def from_defaults(cls) -> "SerialConfig":
        """Create config with sensible defaults."""
        return cls()


class SerialMonitor:
    """
    Non-blocking serial port monitor.

    Reads data from serial port and returns complete lines.
    Handles incomplete lines by buffering them between reads.

    Usage:
        monitor = SerialMonitor("/dev/ttyUSB0", 115200)
        with monitor:
            while True:
                lines = monitor.read_lines()
                for line in lines:
                    process(line)
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        config: Optional[SerialConfig] = None,
        on_error: Optional[Callable[[Exception], None]] = None
    ):
        """
        Initialize SerialMonitor.

        Args:
            port: Serial port path (e.g., "COM3" or "/dev/ttyUSB0")
            baudrate: Baud rate (default 115200)
            config: SerialConfig object for detailed configuration
            on_error: Optional callback for error handling
        """
        self._port = port
        self._baudrate = baudrate
        self._config = config or SerialConfig(port=port, baudrate=baudrate)
        self._on_error = on_error

        self._ser: Optional[serial.Serial] = None
        self._buffer = bytearray()
        self._running = False
        self._lock = threading.Lock()

        self._bytes_read = 0
        self._lines_read = 0
        self._errors_count = 0

    @property
    def port(self) -> str:
        """Get configured port."""
        return self._port

    @property
    def baudrate(self) -> int:
        """Get configured baud rate."""
        return self._baudrate

    @property
    def is_open(self) -> bool:
        """Check if serial port is open."""
        return self._ser is not None and self._ser.is_open

    @property
    def stats(self) -> dict:
        """Get monitor statistics."""
        return {
            "bytes_read": self._bytes_read,
            "lines_read": self._lines_read,
            "errors": self._errors_count,
            "buffer_size": len(self._buffer)
        }

    def open(self) -> bool:
        """
        Open serial port.

        Returns:
            bool: True if port opened successfully.

        Raises:
            SerialPortNotFoundError: If port cannot be opened.
        """
        try:
            if self._ser and self._ser.is_open:
                logger.warning(f"Port {self._port} already open")
                return True

            self._ser = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=self._config.bytesize,
                parity=self._config.parity,
                stopbits=self._config.stopbits,
                timeout=self._config.timeout,
                xonxoff=self._config.xonxoff,
                rtscts=self._config.rtscts,
                dsrdtr=self._config.dsrdtr,
                write_timeout=self._config.write_timeout
            )

            logger.info(f"Opened serial port {self._port} at {self._baudrate} baud")
            return True

        except serial.SerialException as e:
            error_msg = f"Failed to open port {self._port}: {e}"
            logger.error(error_msg)
            self._errors_count += 1
            raise SerialPortNotFoundError(error_msg)

    def close(self) -> None:
        """Close serial port."""
        with self._lock:
            if self._ser:
                try:
                    if self._ser.is_open:
                        self._ser.close()
                    logger.info(f"Closed serial port {self._port}")
                except serial.SerialException as e:
                    logger.warning(f"Error closing port: {e}")
                finally:
                    self._ser = None
            self._buffer.clear()

    def read_lines(self) -> List[bytes]:
        """
        Read available complete lines from serial port.

        Non-blocking read - returns immediately with available lines.
        Incomplete lines are buffered for the next read.

        Returns:
            List of complete lines (without newline characters).
            Each line is bytes (not decoded).
        """
        lines = []

        if not self.is_open:
            return lines

        try:
            # Read available bytes
            if self._ser.in_waiting > 0:
                data = self._ser.read(self._ser.in_waiting)
                self._buffer.extend(data)
                self._bytes_read += len(data)

            # Extract complete lines
            while b'\n' in self._buffer:
                line, self._buffer = self._buffer.split(b'\n', 1)

                # Strip carriage return if present
                if line.endswith(b'\r'):
                    line = line[:-1]

                # Skip empty lines
                if line:
                    lines.append(bytes(line))
                    self._lines_read += 1

        except serial.SerialException as e:
            logger.warning(f"Serial read error: {e}")
            self._errors_count += 1
            if self._on_error:
                try:
                    self._on_error(e)
                except Exception:
                    pass
        except OSError as e:
            logger.warning(f"Serial device error: {e}")
            self._errors_count += 1
            if self._on_error:
                try:
                    self._on_error(e)
                except Exception:
                    pass

        return lines

    def read_line(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """
        Blocking read - wait for a single line.

        Args:
            timeout: Maximum time to wait in seconds. None = wait forever
                     until a line is received or stop() is called.

        Returns:
            Single line as bytes, or None if timeout reached or stop() called.

        Note:
            - Call start() before using this method if you want to control
              when to stop via stop() calls from another thread.
            - If timeout is None and stop() is not called, this waits forever.
        """
        start_time = time.time()
        has_timeout = timeout is not None

        while True:
            lines = self.read_lines()
            if lines:
                return lines[0]

            # Check timeout
            if has_timeout and (time.time() - start_time) > timeout:
                return None

            # Check stop flag (only if start() was called)
            if not self._running:
                return None

            time.sleep(0.001)  # Small sleep to avoid busy loop

    def write(self, data: bytes) -> int:
        """
        Write data to serial port.

        Args:
            data: Bytes to write.

        Returns:
            Number of bytes written.
        """
        if not self.is_open:
            raise SerialReadError("Port not open")

        try:
            return self._ser.write(data)
        except serial.SerialException as e:
            logger.error(f"Serial write error: {e}")
            raise SerialReadError(f"Write failed: {e}")

    def flush(self) -> None:
        """Flush input and output buffers."""
        if self.is_open:
            try:
                self._ser.reset_input_buffer()
                self._ser.reset_output_buffer()
            except serial.SerialException as e:
                logger.warning(f"Error flushing buffers: {e}")

    def clear_buffer(self) -> None:
        """Clear the line buffer."""
        with self._lock:
            self._buffer.clear()

    def start(self) -> None:
        """
        Start the monitor (set running flag).

        Note: This is only used by read_line() for blocking reads.
        read_lines() is non-blocking and does not check _running.
        Call this before using read_line() to control when it should stop.
        """
        self._running = True
        logger.debug("SerialMonitor started")

    def stop(self) -> None:
        """
        Stop the monitor (clear running flag).

        Note: This is only used by read_line() for blocking reads.
        read_lines() is non-blocking and does not check _running.
        Call this to signal read_line() should return.
        """
        self._running = False
        logger.debug("SerialMonitor stopped")

    def reset_stats(self) -> None:
        """Reset statistics counters."""
        self._bytes_read = 0
        self._lines_read = 0
        self._errors_count = 0

    def __enter__(self) -> "SerialMonitor":
        """Context manager entry - open port."""
        self.open()
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - close port."""
        self.stop()
        self.close()

    def __repr__(self) -> str:
        return f"SerialMonitor(port={self._port}, baudrate={self._baudrate}, open={self.is_open})"


class MockSerialMonitor(SerialMonitor):
    """
    Mock SerialMonitor for testing without hardware.

    Accepts data via inject() method instead of reading from port.
    Properly simulates serial behavior including in_waiting and read().
    """

    def __init__(
        self,
        port: str = "/dev/mock",
        baudrate: int = 115200,
        config: Optional[SerialConfig] = None
    ):
        super().__init__(port, baudrate, config)
        self._mock_buffer: List[bytes] = []
        self._open = False
        # Track total bytes read for proper in_waiting calculation
        self._bytes_read_total = 0

    def open(self) -> bool:
        """Mock open always succeeds."""
        self._open = True
        if self._ser is None:
            self._ser = MockSerial(self)
        return True

    def close(self) -> None:
        """Mock close."""
        self._open = False

    @property
    def is_open(self) -> bool:
        """Mock is_open."""
        return self._open

    def inject(self, data: str) -> None:
        """
        Inject test data as if received from serial port.

        Args:
            data: String to inject (will be encoded to bytes).
        """
        self._buffer.extend(data.encode('utf-8'))
        self._bytes_read += len(data)
        # Update mock serial's in_waiting
        if self._ser:
            self._ser.in_waiting = len(self._buffer)

    def inject_bytes(self, data: bytes) -> None:
        """Inject raw bytes."""
        self._buffer.extend(data)
        self._bytes_read += len(data)
        # Update mock serial's in_waiting
        if self._ser:
            self._ser.in_waiting = len(self._buffer)

    def read_lines(self) -> List[bytes]:
        """
        Read available complete lines from mock serial port.

        For MockSerialMonitor, this reads from the internal buffer
        that was populated by inject() calls, properly simulating
        serial port behavior.
        """
        lines = []

        if not self.is_open:
            return lines

        # Read all available data from buffer (simulates serial read)
        if self._buffer:
            # Find complete lines
            while b'\n' in self._buffer:
                line, self._buffer = self._buffer.split(b'\n', 1)

                # Strip carriage return if present
                if line.endswith(b'\r'):
                    line = line[:-1]

                # Skip empty lines
                if line:
                    lines.append(bytes(line))
                    self._lines_read += 1

            # Update mock serial's in_waiting
            if self._ser:
                self._ser.in_waiting = len(self._buffer)

        return lines


class MockSerial:
    """
    Mock serial object for testing.

    Can be paired with a MockSerialMonitor to properly track in_waiting.
    """

    def __init__(self, monitor: "MockSerialMonitor" = None):
        """
        Initialize mock serial.

        Args:
            monitor: Optional MockSerialMonitor reference for in_waiting tracking.
        """
        self.is_open = True
        self._monitor = monitor
        self.in_waiting = 0

    def read(self, size: int) -> bytes:
        """Read bytes from the monitor's buffer."""
        if self._monitor and self._monitor._buffer:
            # Read up to 'size' bytes from the buffer
            data = bytes(self._monitor._buffer[:size])
            # Remove read data from buffer
            self._monitor._buffer = self._monitor._buffer[size:]
            self.in_waiting = len(self._monitor._buffer)
            return data
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        self.is_open = False

    def reset_input_buffer(self) -> None:
        if self._monitor:
            self._monitor._buffer.clear()
            self.in_waiting = 0

    def reset_output_buffer(self) -> None:
        pass


def list_available_ports() -> List[str]:
    """
    List available serial ports.

    Returns:
        List of port paths (e.g., ["COM1", "COM2", "/dev/ttyUSB0"]).
    """
    if not PYSERIAL_AVAILABLE:
        logger.warning("pyserial not installed, cannot list ports")
        return []

    try:
        return [p.device for p in serial.tools.list_ports.comports()]
    except Exception as e:
        logger.warning(f"Error listing ports: {e}")
        return []

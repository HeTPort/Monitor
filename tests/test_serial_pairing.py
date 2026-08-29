from __future__ import annotations

import types
import unittest
from unittest.mock import patch

import src.serial_port_manager as pairing


class _NoScanPC(pairing.PCSerialScanner):
    def list_ports(self):
        raise AssertionError("explicit pairing must not scan PC ports")

    def is_port_available(self, port_name: str) -> bool:
        raise AssertionError("explicit pairing must not query PC ports")


class _NoScanDevice(pairing.DeviceSerialScanner):
    def list_device_ports(self, channel=None):
        raise AssertionError("explicit pairing must not scan device ports")


class _PCScanner(pairing.PCSerialScanner):
    def __init__(self, *ports):
        self._ports = list(ports)

    def list_ports(self):
        return [pairing.PCSerialPort(port=port, description="test port") for port in self._ports]

    def is_port_available(self, port_name: str) -> bool:
        return port_name in self._ports


class _DirectEngine(pairing.OptimizedSerialPairingEngine):
    def __init__(self):
        super().__init__()
        self.calls = []

    def test_pair(self, device_port, pc_port, channel, baudrate=9600, timeout=2.0, **options):
        self.calls.append((device_port, pc_port, baudrate, timeout, options))
        return True, 12.5


class _FakeChannel:
    def __init__(self, *, drop_writes=0, fragment=False, return_code=0):
        self.serial = None
        self.commands = []
        self.drop_writes = drop_writes
        self.fragment = fragment
        self.return_code = return_code

    def invoke(self, command, timeout=5):
        self.commands.append(command)
        if self.return_code:
            return self.return_code, "", "remote write failed"
        if len(self.commands) <= self.drop_writes:
            return 0, "", ""
        if self.serial is not None:
            marker = command.split("echo ", 1)[1].split(" >", 1)[0]
            encoded = marker.encode("ascii")
            if self.fragment and len(self.commands) == self.drop_writes + 1:
                self.serial.buffer.extend(encoded[: len(encoded) // 2])
            elif self.fragment and len(self.commands) == self.drop_writes + 2:
                self.serial.buffer.extend(encoded[len(encoded) // 2 :])
            else:
                self.serial.buffer.extend(encoded)
        return 0, "", ""


class _FakeSerialPort:
    opened = []

    def __init__(self, port, baudrate, timeout):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.buffer = bytearray()
        self.reset_called = False
        self.__class__.opened.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def reset_input_buffer(self):
        self.reset_called = True

    @property
    def in_waiting(self):
        return len(self.buffer)

    def read(self, size):
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value


class SerialPairingTests(unittest.TestCase):
    def test_real_pc_scanner_filters_modem_and_management_ports(self) -> None:
        ports = [
            types.SimpleNamespace(device="COM5", description="USB UART ChA (COM5)", hwid="USB"),
            types.SimpleNamespace(device="COM66", description="HUAWEI Mobile Connect - Modem Interface", hwid="MODEM"),
            types.SimpleNamespace(device="COM3", description="Intel(R) Active Management Technology - SOL", hwid="AMT"),
        ]
        fake_serial_module = types.SimpleNamespace(
            tools=types.SimpleNamespace(list_ports=types.SimpleNamespace(comports=lambda: ports))
        )
        with patch.object(pairing, "PYSERIAL_AVAILABLE", True), patch.object(pairing, "serial", fake_serial_module):
            discovered = pairing.RealPCSerialScanner().list_ports()
        self.assertEqual([port.port for port in discovered], ["COM5"])

    def test_explicit_pair_bypasses_both_scanners_and_uses_9600(self) -> None:
        engine = _DirectEngine()
        config = pairing.SerialPortConfig(
            explicit_device_port="/dev/ttyHW0",
            explicit_pc_port="COM5",
            timeout_sec=4.0,
        )
        manager = pairing.SerialPortManager(
            channel=object(),
            config=config,
            pc_scanner=_NoScanPC(),
            device_scanner=_NoScanDevice(),
            pairing_engine=engine,
        )

        result = manager.discover()

        self.assertTrue(result.success)
        self.assertEqual(engine.calls[0][:4], ("/dev/ttyHW0", "COM5", 9600, 4.0))
        self.assertEqual(engine.calls[0][4]["marker_retries"], 3)
        self.assertEqual([port.port for port in result.device_ports_found], ["/dev/ttyHW0"])
        self.assertEqual([port.port for port in result.pc_ports_found], ["COM5"])

    def test_direct_test_opens_pc_before_device_echo_and_does_not_use_printf(self) -> None:
        _FakeSerialPort.opened.clear()
        channel = _FakeChannel()

        class SerialBoundToChannel(_FakeSerialPort):
            def __enter__(self):
                channel.serial = self
                return super().__enter__()

        fake_serial_module = types.SimpleNamespace(
            Serial=SerialBoundToChannel,
            SerialException=RuntimeError,
        )
        engine = pairing.OptimizedSerialPairingEngine()
        with patch.object(pairing, "PYSERIAL_AVAILABLE", True), patch.object(pairing, "serial", fake_serial_module):
            success, _ = engine.test_pair(
                "/dev/ttyHW0",
                "COM5",
                channel,
                baudrate=9600,
                timeout=0.1,
                settle_time_sec=0,
            )

        self.assertTrue(success)
        self.assertEqual(_FakeSerialPort.opened[0].baudrate, 9600)
        self.assertTrue(_FakeSerialPort.opened[0].reset_called)
        self.assertEqual(len(channel.commands), 1)
        self.assertTrue(channel.commands[0].startswith("echo PAIR_"))
        self.assertNotIn("printf", channel.commands[0])

    def test_direct_pair_retries_after_dropped_first_marker(self) -> None:
        channel = _FakeChannel(drop_writes=1)

        class SerialBoundToChannel(_FakeSerialPort):
            def __enter__(self):
                channel.serial = self
                return super().__enter__()

        fake_serial_module = types.SimpleNamespace(Serial=SerialBoundToChannel, SerialException=RuntimeError)
        engine = pairing.OptimizedSerialPairingEngine()
        with patch.object(pairing, "PYSERIAL_AVAILABLE", True), patch.object(pairing, "serial", fake_serial_module):
            success, _ = engine.test_pair(
                "/dev/ttyUSB7",
                "COM42",
                channel,
                baudrate=115200,
                timeout=0.1,
                settle_time_sec=0,
                marker_retries=3,
                marker_retry_interval_sec=0.01,
            )

        self.assertTrue(success)
        self.assertEqual(len(channel.commands), 2)
        self.assertEqual(engine.last_diagnostic.code, "SUCCESS")
        self.assertEqual(engine.last_diagnostic.marker_writes, 2)
        self.assertEqual(engine.last_diagnostic.baudrate, 115200)

    def test_direct_pair_accepts_marker_split_across_reads(self) -> None:
        channel = _FakeChannel(fragment=True)

        class SerialBoundToChannel(_FakeSerialPort):
            def __enter__(self):
                channel.serial = self
                return super().__enter__()

        fake_serial_module = types.SimpleNamespace(Serial=SerialBoundToChannel, SerialException=RuntimeError)
        engine = pairing.OptimizedSerialPairingEngine()
        with patch.object(pairing, "PYSERIAL_AVAILABLE", True), patch.object(pairing, "serial", fake_serial_module):
            success, _ = engine.test_pair(
                "/dev/ttyACM9",
                "/dev/ttyUSB3",
                channel,
                baudrate=57600,
                timeout=0.1,
                settle_time_sec=0,
                marker_retry_interval_sec=0.01,
            )

        self.assertTrue(success)
        self.assertEqual(engine.last_diagnostic.code, "SUCCESS")
        self.assertGreater(engine.last_diagnostic.bytes_received, 0)

    def test_direct_pair_classifies_zero_rx_and_remote_failure(self) -> None:
        for channel, expected in (
            (_FakeChannel(drop_writes=10), "NO_RX_BYTES"),
            (_FakeChannel(return_code=7), "DEVICE_ECHO_FAILED"),
        ):
            class SerialBoundToChannel(_FakeSerialPort):
                def __enter__(self):
                    channel.serial = self
                    return super().__enter__()

            fake_serial_module = types.SimpleNamespace(Serial=SerialBoundToChannel, SerialException=RuntimeError)
            engine = pairing.OptimizedSerialPairingEngine()
            with patch.object(pairing, "PYSERIAL_AVAILABLE", True), patch.object(pairing, "serial", fake_serial_module):
                success, _ = engine.test_pair(
                    "/dev/ttyS2",
                    "COM7",
                    channel,
                    timeout=0.04,
                    settle_time_sec=0,
                    marker_retries=2,
                    marker_retry_interval_sec=0.01,
                )
            self.assertFalse(success)
            self.assertEqual(engine.last_diagnostic.code, expected)

    def test_direct_pair_classifies_busy_pc_port(self) -> None:
        def busy_serial(*args, **kwargs):
            raise RuntimeError("Access is denied: port is busy")

        fake_serial_module = types.SimpleNamespace(Serial=busy_serial, SerialException=RuntimeError)
        engine = pairing.OptimizedSerialPairingEngine()
        with patch.object(pairing, "PYSERIAL_AVAILABLE", True), patch.object(pairing, "serial", fake_serial_module):
            success, _ = engine.test_pair("/dev/ttyS0", "COM9", _FakeChannel(), settle_time_sec=0)

        self.assertFalse(success)
        self.assertEqual(engine.last_diagnostic.code, "PC_PORT_BUSY")

    def test_auto_pair_retries_marker_on_arbitrary_candidates(self) -> None:
        class FakeMonitor:
            instances = []

            def __init__(self, port_name, baudrate=9600, timeout=10.0):
                self.port_name = port_name
                self.baudrate = baudrate
                self.buffer = bytearray()
                self.running = False
                self.__class__.instances.append(self)

            def start(self):
                self.running = True
                return True

            def stop(self):
                self.running = False

            def is_running(self):
                return self.running

            def contains(self, marker):
                return marker.encode("ascii") in self.buffer

            def snapshot(self):
                return bytes(self.buffer)

        class RetryChannel:
            def __init__(self):
                self.commands = []

            def invoke(self, command, timeout=5):
                self.commands.append(command)
                if len(self.commands) == 2:
                    marker = command.split("echo ", 1)[1].split(" >", 1)[0]
                    FakeMonitor.instances[0].buffer.extend(marker.encode("ascii"))
                return 0, "", ""

        channel = RetryChannel()
        engine = pairing.OptimizedSerialPairingEngine()
        config = pairing.SerialPortConfig(
            baudrate=38400,
            timeout_sec=0.1,
            settle_time_sec=0,
            marker_retries=3,
            marker_retry_interval_sec=0.01,
        )
        candidates = [pairing.DeviceSerialPort(port="/dev/ttyVendor9", writable=True)]

        with patch.object(pairing, "PCSerialPortMonitor", FakeMonitor):
            result = engine.auto_pair(_PCScanner("COM77"), candidates, channel, config)

        self.assertTrue(result.success)
        self.assertEqual((result.device_port, result.pc_port), ("/dev/ttyVendor9", "COM77"))
        self.assertEqual(len(channel.commands), 2)
        self.assertEqual(result.diagnostics[0]["marker_writes"], 2)
        self.assertEqual(result.diagnostics[0]["baudrate"], 38400)

    def test_empty_fallbacks_do_not_guess_device_specific_ports(self) -> None:
        config = pairing.SerialPortConfig()
        self.assertEqual(config.fallback_device_ports, [])
        self.assertEqual(config.fallback_pc_ports, [])

        result = pairing.OptimizedSerialPairingEngine().auto_pair(
            _PCScanner("COM1"), [], object(), config
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_code, "NO_DEVICE_PORTS")


if __name__ == "__main__":
    unittest.main()

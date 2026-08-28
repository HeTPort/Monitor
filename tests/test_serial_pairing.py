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


class _DirectEngine(pairing.OptimizedSerialPairingEngine):
    def __init__(self):
        super().__init__()
        self.calls = []

    def test_pair(self, device_port, pc_port, channel, baudrate=9600, timeout=2.0):
        self.calls.append((device_port, pc_port, baudrate, timeout))
        return True, 12.5


class _FakeChannel:
    def __init__(self):
        self.serial = None
        self.commands = []

    def invoke(self, command, timeout=5):
        self.commands.append(command)
        if self.serial is not None:
            marker = command.split("echo ", 1)[1].split(" >", 1)[0]
            self.serial.buffer.extend(marker.encode("ascii"))
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
        self.assertEqual(engine.calls, [("/dev/ttyHW0", "COM5", 9600, 4.0)])
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
            success, _ = engine.test_pair("/dev/ttyHW0", "COM5", channel, baudrate=9600, timeout=0.1)

        self.assertTrue(success)
        self.assertEqual(_FakeSerialPort.opened[0].baudrate, 9600)
        self.assertTrue(_FakeSerialPort.opened[0].reset_called)
        self.assertEqual(len(channel.commands), 1)
        self.assertTrue(channel.commands[0].startswith("echo PAIR_"))
        self.assertNotIn("printf", channel.commands[0])


if __name__ == "__main__":
    unittest.main()

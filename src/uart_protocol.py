"""UART-v2 framing and session-aware decoding.

Wire format: NUL + COBS(UTF-8 compact JSON + CRC32 little-endian) + NUL.
The relay adds/removes no event semantics; JSON event schema remains version 1.
"""

from __future__ import annotations

import json
import zlib
from typing import Any, Mapping

from .events import EventEnvelope, EventProtocolError


UART_PROTOCOL = "uart-v2"


def cobs_encode(data: bytes) -> bytes:
    output = bytearray((0,))
    code_index = 0
    code = 1
    for value in data:
        if value == 0:
            output[code_index] = code
            code_index = len(output)
            output.append(0)
            code = 1
        else:
            output.append(value)
            code += 1
            if code == 0xFF:
                output[code_index] = code
                code_index = len(output)
                output.append(0)
                code = 1
    output[code_index] = code
    return bytes(output)


def cobs_decode(data: bytes) -> bytes:
    if not data:
        raise EventProtocolError("empty_frame", "UART-v2 frame is empty")
    output = bytearray()
    index = 0
    while index < len(data):
        code = data[index]
        if code == 0:
            raise EventProtocolError("invalid_cobs", "COBS frame contains a zero byte")
        index += 1
        end = index + code - 1
        if end > len(data):
            raise EventProtocolError("invalid_cobs", "COBS code exceeds frame length")
        output.extend(data[index:end])
        index = end
        if code != 0xFF and index < len(data):
            output.append(0)
    return bytes(output)


def encode_uart_frame(record: Mapping[str, Any]) -> bytes:
    payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    checksum = zlib.crc32(payload) & 0xFFFFFFFF
    return b"\x00" + cobs_encode(payload + checksum.to_bytes(4, "little")) + b"\x00"


def frame_wire_seconds(frame_bytes: int, baudrate: int, *, bits_per_byte: int = 10) -> float:
    if frame_bytes < 0 or baudrate <= 0 or bits_per_byte <= 0:
        raise ValueError("frame size, baudrate, and bits per byte must be positive")
    return frame_bytes * bits_per_byte / baudrate


class UartV2Decoder:
    """Discard stale preamble, then fail closed for the active attempt."""

    def __init__(self, run_id: str, test_id: str, *, max_frame_bytes: int = 512) -> None:
        if not run_id or not test_id:
            raise ValueError("run_id and test_id are required")
        if max_frame_bytes < 64:
            raise ValueError("max_frame_bytes must be at least 64")
        self.run_id = run_id
        self.test_id = test_id
        self.max_frame_bytes = max_frame_bytes
        self.expected_seq = 1
        self.active = False
        self.final_seen = False
        self.discarded_frames = 0
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list[EventEnvelope]:
        if not isinstance(data, bytes):
            raise TypeError("UART-v2 decoder accepts bytes")
        self._buffer.extend(data)
        events: list[EventEnvelope] = []
        while True:
            delimiter = self._buffer.find(b"\x00")
            if delimiter < 0:
                if len(self._buffer) > self.max_frame_bytes:
                    if self.active:
                        raise EventProtocolError("frame_too_long", "active UART-v2 frame exceeded the configured limit")
                    self._buffer.clear()
                    self.discarded_frames += 1
                break
            encoded = bytes(self._buffer[:delimiter])
            del self._buffer[: delimiter + 1]
            if not encoded:
                continue
            if len(encoded) > self.max_frame_bytes:
                error = EventProtocolError("frame_too_long", "UART-v2 frame exceeded the configured limit")
                if self.active:
                    raise error
                self.discarded_frames += 1
                continue
            try:
                record = self._decode_record(encoded)
            except EventProtocolError:
                if self.active:
                    raise
                self.discarded_frames += 1
                continue
            matching = record.get("run_id") == self.run_id and record.get("test_id") == self.test_id
            if not self.active:
                if not matching or record.get("type") != "agent_start" or record.get("seq") != 1:
                    self.discarded_frames += 1
                    continue
                self.active = True
            elif not matching:
                raise EventProtocolError("wrong_session", "UART-v2 event changed run_id or test_id after START")
            event = EventEnvelope.from_mapping(
                record,
                expected_run_id=self.run_id,
                expected_seq=self.expected_seq,
                verify_crc=False,
            )
            self.expected_seq += 1
            events.append(event)
            if event.type == "agent_final":
                self.final_seen = True
                # FINAL closes the current session. Bytes already buffered after
                # it belong to later UART activity and must not poison this run.
                self._buffer.clear()
                return events
        return events

    def finish(self) -> None:
        if self.active and any(self._buffer):
            raise EventProtocolError("truncated_frame", "UART-v2 stream ended inside an active frame")

    @staticmethod
    def _decode_record(encoded: bytes) -> dict[str, Any]:
        decoded = cobs_decode(encoded)
        if len(decoded) < 5:
            raise EventProtocolError("short_frame", "UART-v2 frame has no JSON payload and CRC")
        payload, supplied = decoded[:-4], int.from_bytes(decoded[-4:], "little")
        actual = zlib.crc32(payload) & 0xFFFFFFFF
        if supplied != actual:
            raise EventProtocolError("transport_crc_mismatch", "UART-v2 transport CRC does not match")
        try:
            record = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventProtocolError("invalid_frame_json", f"UART-v2 payload is not UTF-8 JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise EventProtocolError("invalid_frame_json", "UART-v2 JSON payload must be an object")
        return record

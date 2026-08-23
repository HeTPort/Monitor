"""Versioned UART JSONL event encoding and incremental decoding."""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass, field
from typing import Any, Mapping


EVENT_SCHEMA_VERSION = 1
EVENT_SOURCES = {
    "agent",
    "cpu-workload",
    "gpu-workload",
    "cpu-telemetry",
    "gpu-telemetry",
    "kernel",
}
EVENT_TYPES = {
    "agent_start",
    "capability",
    "environment",
    "start",
    "heartbeat",
    "batch",
    "verify",
    "golden",
    "telemetry",
    "kernel",
    "error",
    "summary",
    "violation",
    "agent_final",
}


class EventProtocolError(ValueError):
    """A framing/integrity problem that makes the run evidence unreliable."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _canonical_bytes(record: Mapping[str, Any]) -> bytes:
    without_crc = {key: value for key, value in record.items() if key != "crc32"}
    return json.dumps(without_crc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def event_crc32(record: Mapping[str, Any]) -> str:
    return f"{zlib.crc32(_canonical_bytes(record)) & 0xFFFFFFFF:08x}"


@dataclass(frozen=True)
class EventEnvelope:
    schema_version: int
    run_id: str
    seq: int
    timestamp_ms: int
    source: str
    type: str
    payload: dict[str, Any]
    crc32: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_mapping(
        cls,
        record: Mapping[str, Any],
        *,
        expected_run_id: str | None = None,
        expected_seq: int | None = None,
        verify_crc: bool = True,
    ) -> "EventEnvelope":
        required = {
            "schema_version": int,
            "run_id": str,
            "seq": int,
            "timestamp_ms": int,
            "source": str,
            "type": str,
            "payload": dict,
        }
        for key, expected_type in required.items():
            if key not in record:
                raise EventProtocolError("missing_field", f"event missing required field: {key}")
            if not isinstance(record[key], expected_type) or isinstance(record[key], bool):
                raise EventProtocolError("invalid_field", f"event field {key} has invalid type")
        if record["schema_version"] != EVENT_SCHEMA_VERSION:
            raise EventProtocolError(
                "unsupported_schema",
                f"event schema {record['schema_version']} is unsupported; expected {EVENT_SCHEMA_VERSION}",
            )
        if expected_run_id is not None and record["run_id"] != expected_run_id:
            raise EventProtocolError(
                "wrong_run_id",
                f"event run_id {record['run_id']!r} does not match {expected_run_id!r}",
            )
        if expected_seq is not None and record["seq"] != expected_seq:
            relation = "duplicate_or_reordered" if record["seq"] < expected_seq else "sequence_gap"
            raise EventProtocolError(relation, f"event seq {record['seq']} does not match expected {expected_seq}")
        if record["source"] not in EVENT_SOURCES:
            raise EventProtocolError("invalid_source", f"unsupported event source: {record['source']}")
        if record["type"] not in EVENT_TYPES:
            raise EventProtocolError("invalid_type", f"unsupported event type: {record['type']}")
        supplied_crc = record.get("crc32")
        if supplied_crc is not None:
            if not isinstance(supplied_crc, str):
                raise EventProtocolError("invalid_crc", "event crc32 must be a hexadecimal string")
            normalized_crc = supplied_crc.lower().removeprefix("0x")
            if verify_crc and normalized_crc != event_crc32(record):
                raise EventProtocolError("crc_mismatch", "event crc32 does not match its canonical content")
            supplied_crc = normalized_crc
        return cls(
            schema_version=record["schema_version"],
            run_id=record["run_id"],
            seq=record["seq"],
            timestamp_ms=record["timestamp_ms"],
            source=record["source"],
            type=record["type"],
            payload=dict(record["payload"]),
            crc32=supplied_crc,
            raw=dict(record),
        )

    def to_dict(self, *, include_crc: bool = True) -> dict[str, Any]:
        record = dict(self.raw) if self.raw else {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "seq": self.seq,
            "timestamp_ms": self.timestamp_ms,
            "source": self.source,
            "type": self.type,
            "payload": self.payload,
        }
        if include_crc:
            record["crc32"] = self.crc32 or event_crc32(record)
        else:
            record.pop("crc32", None)
        return record


class EventDecoder:
    """Decode arbitrarily fragmented UART bytes into verified event envelopes."""

    def __init__(
        self,
        run_id: str,
        *,
        first_seq: int = 1,
        verify_crc: bool = True,
        max_line_bytes: int = 1024 * 1024,
    ) -> None:
        if not run_id:
            raise ValueError("run_id must not be empty")
        self.run_id = run_id
        self.expected_seq = first_seq
        self.verify_crc = verify_crc
        self.max_line_bytes = max_line_bytes
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list[EventEnvelope]:
        if not isinstance(data, bytes):
            raise TypeError("event decoder accepts bytes")
        self._buffer.extend(data)
        if len(self._buffer) > self.max_line_bytes and b"\n" not in self._buffer:
            raise EventProtocolError("line_too_long", f"UART line exceeds {self.max_line_bytes} bytes")
        events: list[EventEnvelope] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if not line:
                continue
            events.append(self._decode_line(line))
        return events

    def finish(self) -> None:
        if self._buffer:
            raise EventProtocolError("truncated_line", "UART stream ended with an incomplete JSONL record")

    def _decode_line(self, line: bytes) -> EventEnvelope:
        if len(line) > self.max_line_bytes:
            raise EventProtocolError("line_too_long", f"UART line exceeds {self.max_line_bytes} bytes")
        try:
            text = line.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise EventProtocolError("invalid_utf8", f"invalid UTF-8 at byte {exc.start}") from exc
        try:
            record = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EventProtocolError("malformed_json", f"invalid JSON event: {exc.msg}") from exc
        if not isinstance(record, dict):
            raise EventProtocolError("invalid_envelope", "event JSON root must be an object")
        event = EventEnvelope.from_mapping(
            record,
            expected_run_id=self.run_id,
            expected_seq=self.expected_seq,
            verify_crc=self.verify_crc,
        )
        self.expected_seq += 1
        return event


def build_event(
    *,
    run_id: str,
    seq: int,
    timestamp_ms: int,
    source: str,
    event_type: str,
    payload: Mapping[str, Any],
    include_crc: bool = True,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "run_id": run_id,
        "seq": seq,
        "timestamp_ms": timestamp_ms,
        "source": source,
        "type": event_type,
        "payload": dict(payload),
    }
    EventEnvelope.from_mapping(record, expected_run_id=run_id, expected_seq=seq, verify_crc=False)
    if include_crc:
        record["crc32"] = event_crc32(record)
    return record


def encode_event(record: Mapping[str, Any]) -> bytes:
    """Encode one envelope as compact UTF-8 JSON followed by LF."""
    return json.dumps(dict(record), separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def wrap_workload_event(
    workload_record: Mapping[str, Any],
    *,
    run_id: str,
    seq: int,
    timestamp_ms: int,
    target: str,
) -> dict[str, Any]:
    """Wrap a native CPU/GPU JSONL record without changing or dropping fields."""
    event_type = workload_record.get("type")
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise EventProtocolError("invalid_workload_event", f"unsupported workload event type: {event_type!r}")
    if target not in {"cpu", "gpu"}:
        raise EventProtocolError("invalid_workload_target", f"unsupported workload target: {target!r}")
    return build_event(
        run_id=run_id,
        seq=seq,
        timestamp_ms=timestamp_ms,
        source=f"{target}-workload",
        event_type=event_type,
        payload=dict(workload_record),
    )

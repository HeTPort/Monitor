from __future__ import annotations

import unittest

from src.events import (
    EventDecoder,
    EventEnvelope,
    EventProtocolError,
    build_event,
    encode_event,
    wrap_workload_event,
)


class EventDecoderTests(unittest.TestCase):
    def event(self, seq: int, event_type: str = "heartbeat", payload: dict | None = None) -> dict:
        return build_event(
            run_id="run-1",
            seq=seq,
            timestamp_ms=seq * 10,
            source="cpu-workload",
            event_type=event_type,
            payload=payload or {},
        )

    def test_fragmentation_and_concatenation(self) -> None:
        wire = encode_event(self.event(1)) + encode_event(self.event(2, "summary", {"result": "PASS", "exit_code": 0}))
        decoder = EventDecoder("run-1")
        events = decoder.feed(wire[:17]) + decoder.feed(wire[17:53]) + decoder.feed(wire[53:])
        decoder.finish()
        self.assertEqual([event.seq for event in events], [1, 2])

    def test_unknown_additive_fields_are_preserved(self) -> None:
        record = self.event(1)
        record["future_field"] = {"value": 7}
        # Recompute CRC after adding the field.
        record.pop("crc32")
        from src.events import event_crc32

        record["crc32"] = event_crc32(record)
        event = EventDecoder("run-1").feed(encode_event(record))[0]
        self.assertEqual(event.raw["future_field"], {"value": 7})

    def test_sequence_gap_is_protocol_failure(self) -> None:
        decoder = EventDecoder("run-1")
        decoder.feed(encode_event(self.event(1)))
        with self.assertRaisesRegex(EventProtocolError, "expected 2") as raised:
            decoder.feed(encode_event(self.event(3)))
        self.assertEqual(raised.exception.kind, "sequence_gap")

    def test_wrong_run_crc_utf8_and_truncation_fail(self) -> None:
        wrong = build_event(
            run_id="other",
            seq=1,
            timestamp_ms=1,
            source="agent",
            event_type="agent_start",
            payload={},
        )
        with self.assertRaises(EventProtocolError):
            EventDecoder("run-1").feed(encode_event(wrong))

        bad_crc = self.event(1)
        bad_crc["crc32"] = "00000000"
        with self.assertRaisesRegex(EventProtocolError, "crc32"):
            EventDecoder("run-1").feed(encode_event(bad_crc))

        with self.assertRaisesRegex(EventProtocolError, "UTF-8"):
            EventDecoder("run-1").feed(b"\xff\n")

        decoder = EventDecoder("run-1")
        decoder.feed(b'{"schema_version":1')
        with self.assertRaisesRegex(EventProtocolError, "incomplete"):
            decoder.finish()

    def test_native_workload_payload_is_not_reduced(self) -> None:
        native = {"type": "summary", "result": "PERFORMANCE_FAIL", "exit_code": 7, "custom": 9}
        wrapped = wrap_workload_event(native, run_id="run-1", seq=1, timestamp_ms=99, target="cpu")
        event = EventEnvelope.from_mapping(wrapped, expected_run_id="run-1", expected_seq=1)
        self.assertEqual(event.payload, native)


if __name__ == "__main__":
    unittest.main()

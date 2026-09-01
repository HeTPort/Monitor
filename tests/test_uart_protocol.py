import unittest

from src.events import EventProtocolError
from src.uart_protocol import UartV2Decoder, cobs_decode, cobs_encode, encode_uart_frame, frame_wire_seconds


def event(run_id, test_id, seq, event_type, payload=None):
    return {
        "schema_version": 1,
        "test_id": test_id,
        "run_id": run_id,
        "seq": seq,
        "timestamp_ms": seq,
        "source": "agent" if event_type.startswith("agent_") else "cpu-workload",
        "type": event_type,
        "payload": payload or {},
    }


class UartProtocolTests(unittest.TestCase):
    def test_cobs_round_trip_including_zeroes(self):
        raw = b"\x00abc\x00\x01\xff\x00"
        encoded = cobs_encode(raw)
        self.assertNotIn(b"\x00", encoded)
        self.assertEqual(cobs_decode(encoded), raw)

    def test_fragmented_frames_decode(self):
        wire = encode_uart_frame(event("r", "t", 1, "agent_start")) + encode_uart_frame(
            event("r", "t", 2, "agent_final", {"workload_exit_code": 0})
        )
        decoder = UartV2Decoder("r", "t")
        events = []
        for part in (wire[:3], wire[3:17], wire[17:]):
            events.extend(decoder.feed(part))
        self.assertEqual([item.type for item in events], ["agent_start", "agent_final"])
        self.assertTrue(decoder.final_seen)

    def test_stale_and_corrupt_preamble_is_discarded(self):
        stale = encode_uart_frame(event("old", "old-test", 1, "agent_start"))
        corrupt = b"\x00\x02x\x00"
        current = encode_uart_frame(event("new", "test", 1, "agent_start"))
        decoder = UartV2Decoder("new", "test")
        decoded = decoder.feed(b"old text" + b"\x00" + stale + corrupt + current)
        self.assertEqual([item.run_id for item in decoded], ["new"])
        self.assertGreaterEqual(decoder.discarded_frames, 2)

    def test_active_crc_failure_is_fail_closed(self):
        decoder = UartV2Decoder("r", "t")
        decoder.feed(encode_uart_frame(event("r", "t", 1, "agent_start")))
        damaged = bytearray(encode_uart_frame(event("r", "t", 2, "agent_final")))
        damaged[-3] ^= 1
        with self.assertRaises(EventProtocolError) as caught:
            decoder.feed(bytes(damaged))
        self.assertEqual(caught.exception.kind, "transport_crc_mismatch")

    def test_bytes_after_final_do_not_poison_completed_session(self):
        wire = (
            encode_uart_frame(event("r", "t", 1, "agent_start"))
            + encode_uart_frame(event("r", "t", 2, "agent_final", {"workload_exit_code": 0}))
            + encode_uart_frame(event("next", "next-test", 1, "agent_start"))
        )
        decoder = UartV2Decoder("r", "t")
        decoded = decoder.feed(wire)
        self.assertEqual([item.type for item in decoded], ["agent_start", "agent_final"])

    def test_wire_time_scales_with_baud(self):
        self.assertAlmostEqual(frame_wire_seconds(512, 9600), 5120 / 9600)
        self.assertGreater(frame_wire_seconds(512, 1200), frame_wire_seconds(512, 115200))


if __name__ == "__main__":
    unittest.main()

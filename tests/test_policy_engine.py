from __future__ import annotations

import unittest

from src.events import EventEnvelope, EventProtocolError, build_event
from src.policy_engine import PolicyEngine, PolicyLimits


def event(seq: int, event_type: str, payload: dict, source: str = "cpu-workload") -> EventEnvelope:
    record = build_event(
        run_id="run-1",
        seq=seq,
        timestamp_ms=seq * 100,
        source=source,
        event_type=event_type,
        payload=payload,
    )
    return EventEnvelope.from_mapping(record, expected_run_id="run-1", expected_seq=seq)


class PolicyEngineTests(unittest.TestCase):
    def test_pass_requires_pass_summary_and_agent_final(self) -> None:
        engine = PolicyEngine()
        engine.process(event(1, "summary", {"result": "PASS", "exit_code": 0}))
        engine.process(event(2, "agent_final", {"restoration_ok": True, "spool_complete": True}, "agent"))
        result = engine.finalize()
        self.assertEqual(result.verdict, "PASS")
        self.assertEqual(result.workload_exit_code, 0)

    def test_every_nonpass_workload_result_is_dut_failure(self) -> None:
        for native_result, native_exit in (
            ("CHECKSUM_FAIL", 1),
            ("API_ERROR", 2),
            ("TIMEOUT", 3),
            ("DEVICE_LOST", 4),
            ("ALLOCATION_FAIL", 5),
            ("UNKNOWN_ERROR", 6),
            ("PERFORMANCE_FAIL", 7),
        ):
            with self.subTest(native_result=native_result):
                engine = PolicyEngine()
                engine.process(event(1, "summary", {"result": native_result, "exit_code": native_exit}))
                engine.process(event(2, "agent_final", {"restoration_ok": True, "spool_complete": True}, "agent"))
                result = engine.finalize()
                self.assertEqual(result.verdict, "DUT_FAIL")
                self.assertEqual(result.workload_result, native_result)
                self.assertEqual(result.workload_exit_code, native_exit)

    def test_protocol_failure_outweighs_dut_but_preserves_both(self) -> None:
        engine = PolicyEngine()
        engine.process(event(1, "summary", {"result": "CHECKSUM_FAIL", "exit_code": 1}))
        engine.process(event(2, "agent_final", {"restoration_ok": True, "spool_complete": True}, "agent"))
        engine.protocol_failure(EventProtocolError("crc_mismatch", "bad crc"))
        result = engine.finalize()
        self.assertEqual(result.verdict, "INFRA_ERROR")
        self.assertTrue(result.dut_reasons)
        self.assertTrue(result.infrastructure_reasons)

    def test_limits_and_required_telemetry(self) -> None:
        limits = PolicyLimits.from_mapping(
            {
                "performance": {"operations_per_sec_avg": {"min": 100.0}},
                "telemetry": {"cpu.temperature": {"max": 60.0}},
                "required_telemetry": ["cpu.temperature", "cpu.frequency"],
            }
        )
        engine = PolicyEngine(limits)
        engine.process(
            event(
                1,
                "telemetry",
                {"metrics": {"cpu.temperature": 65.0, "cpu.frequency": 2500000}},
                "cpu-telemetry",
            )
        )
        engine.process(event(2, "summary", {"result": "PASS", "exit_code": 0, "operations_per_sec_avg": 90.0}))
        engine.process(event(3, "agent_final", {"restoration_ok": True, "spool_complete": True}, "agent"))
        result = engine.finalize()
        self.assertEqual(result.verdict, "DUT_FAIL")
        self.assertGreaterEqual(len(result.dut_reasons), 2)

    def test_missing_agent_final_is_infrastructure_error(self) -> None:
        engine = PolicyEngine()
        engine.process(event(1, "summary", {"result": "PASS", "exit_code": 0}))
        self.assertEqual(engine.finalize().verdict, "INFRA_ERROR")

    def test_nonzero_agent_exit_without_summary_is_preserved_as_dut_failure(self) -> None:
        engine = PolicyEngine()
        engine.process(
            event(
                1,
                "agent_final",
                {"workload_exit_code": 4, "summary_seen": False, "restoration_ok": True, "spool_complete": True},
                "agent",
            )
        )
        result = engine.finalize()
        self.assertEqual(result.verdict, "DUT_FAIL")
        self.assertTrue(any(reason["code"] == "WORKLOAD_EXIT_NONZERO" for reason in result.dut_reasons))

    def test_required_telemetry_accepts_per_core_metric_instances(self) -> None:
        engine = PolicyEngine(PolicyLimits(required_telemetry=("cpu.frequency",)))
        engine.process(
            event(1, "telemetry", {"metrics": {"cpu.frequency.4": 2200000, "cpu.frequency.5": 2200000}}, "cpu-telemetry")
        )
        engine.process(event(2, "summary", {"result": "PASS", "exit_code": 0}))
        engine.process(event(3, "agent_final", {"workload_exit_code": 0, "restoration_ok": True, "spool_complete": True}, "agent"))
        self.assertEqual(engine.finalize().verdict, "PASS")


if __name__ == "__main__":
    unittest.main()

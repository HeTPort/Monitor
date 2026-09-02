"""Typed policy evaluation for workload, telemetry, kernel, liveness, and infrastructure evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Mapping

from .events import EventEnvelope, EventProtocolError


class RunExitCode(IntEnum):
    PASS = 0
    DUT_FAIL = 1
    SILENT_FAILURE = 2
    INFRA_ERROR = 3
    INVALID_CONFIGURATION = 4
    UNSUPPORTED = 5
    USER_ABORT = 6


VERDICT_BY_EXIT = {
    RunExitCode.PASS: "PASS",
    RunExitCode.DUT_FAIL: "DUT_FAIL",
    RunExitCode.SILENT_FAILURE: "SILENT_FAILURE",
    RunExitCode.INFRA_ERROR: "INFRA_ERROR",
    RunExitCode.INVALID_CONFIGURATION: "INVALID_CONFIGURATION",
    RunExitCode.UNSUPPORTED: "UNSUPPORTED",
    RunExitCode.USER_ABORT: "USER_ABORT",
}


@dataclass(frozen=True)
class PolicyLimits:
    performance: dict[str, dict[str, float]] = field(default_factory=dict)
    telemetry: dict[str, dict[str, float]] = field(default_factory=dict)
    required_telemetry: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "PolicyLimits":
        raw = data or {}
        performance = raw.get("performance", {})
        telemetry = raw.get("telemetry", {})
        required = raw.get("required_telemetry", [])
        if not isinstance(performance, dict) or not isinstance(telemetry, dict):
            raise ValueError("performance and telemetry limits must be mappings")
        if not isinstance(required, (list, tuple)) or not all(isinstance(item, str) for item in required):
            raise ValueError("required_telemetry must be a list of strings")
        return cls(
            performance={key: dict(value) for key, value in performance.items()},
            telemetry={key: dict(value) for key, value in telemetry.items()},
            required_telemetry=tuple(required),
        )


@dataclass
class PolicyResult:
    verdict: str
    exit_code: int
    dut_reasons: list[dict[str, Any]]
    infrastructure_reasons: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    workload_result: str | None
    workload_exit_code: int | None
    workload_summary: dict[str, Any] | None
    telemetry_seen: list[str]
    terminal_summary_seen: bool
    agent_final_seen: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "exit_code": self.exit_code,
            "dut_reasons": self.dut_reasons,
            "infrastructure_reasons": self.infrastructure_reasons,
            "warnings": self.warnings,
            "workload_result": self.workload_result,
            "workload_exit_code": self.workload_exit_code,
            "workload_summary": self.workload_summary,
            "telemetry_seen": self.telemetry_seen,
            "terminal_summary_seen": self.terminal_summary_seen,
            "agent_final_seen": self.agent_final_seen,
        }


class PolicyEngine:
    """Accumulate evidence while retaining DUT and infrastructure reasons separately."""

    def __init__(self, limits: PolicyLimits | None = None) -> None:
        self.limits = limits or PolicyLimits()
        self.dut_reasons: list[dict[str, Any]] = []
        self.infrastructure_reasons: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.workload_summary: dict[str, Any] | None = None
        self.workload_result: str | None = None
        self.workload_exit_code: int | None = None
        self.telemetry_seen: set[str] = set()
        self.agent_final_seen = False
        self.agent_workload_exit_code: int | None = None
        self._pre_run_exit: RunExitCode | None = None
        self._user_aborted = False

    def process(self, event: EventEnvelope) -> None:
        payload = event.payload
        if event.type == "summary":
            self._process_summary(payload, event)
        elif event.type == "verify" and payload.get("pass") is False:
            self._dut("correctness", "VERIFY_FAIL", payload, event)
        elif event.type == "telemetry":
            self._process_telemetry(payload, event)
        elif event.type == "kernel":
            severity = str(payload.get("severity", "warning")).lower()
            if severity in {"critical", "fail", "fatal"}:
                self._dut("kernel", str(payload.get("rule_id", "KERNEL_CRITICAL")), payload, event)
            else:
                self._warn("kernel", str(payload.get("rule_id", "KERNEL_WARNING")), payload, event)
        elif event.type == "violation":
            scope = str(payload.get("scope", "policy"))
            self._dut(scope, str(payload.get("code", "LIMIT_VIOLATION")), payload, event)
        elif event.type == "error":
            origin = str(payload.get("origin", event.source))
            if origin in {"agent", "transport", "artifact", "restoration"} or event.source == "agent":
                self._infra("agent", str(payload.get("error_code", "AGENT_ERROR")), payload, event)
            else:
                self._dut("workload", str(payload.get("error_code", payload.get("error_type", "WORKLOAD_ERROR"))), payload, event)
        elif event.type == "agent_final":
            self.agent_final_seen = True
            agent_exit = payload.get("workload_exit_code")
            if isinstance(agent_exit, int) and not isinstance(agent_exit, bool):
                self.agent_workload_exit_code = agent_exit
                if agent_exit != 0:
                    self._dut("workload", "WORKLOAD_EXIT_NONZERO", {"exit_code": agent_exit}, event)
                if self.workload_exit_code is not None and agent_exit != self.workload_exit_code:
                    self._infra(
                        "workload",
                        "WORKLOAD_EXIT_MISMATCH",
                        {"summary_exit_code": self.workload_exit_code, "agent_exit_code": agent_exit},
                        event,
                    )
            if "restoration_ok" in payload and payload.get("restoration_ok") is not True:
                self._infra("restoration", "RESTORATION_FAILED", payload, event)
            if payload.get("spool_complete") is False:
                self._infra("artifact", "DEVICE_SPOOL_INCOMPLETE", payload, event)

    def protocol_failure(self, error: EventProtocolError) -> None:
        self.infrastructure_reasons.append({"scope": "protocol", "code": error.kind, "message": str(error)})

    def infrastructure_failure(self, scope: str, code: str, message: str) -> None:
        reason = {"scope": scope, "code": code, "message": message}
        if reason not in self.infrastructure_reasons:
            self.infrastructure_reasons.append(reason)

    def invalid_configuration(self, message: str) -> None:
        self._pre_run_exit = RunExitCode.INVALID_CONFIGURATION
        self.infrastructure_reasons.append({"scope": "configuration", "code": "INVALID_CONFIGURATION", "message": message})

    def unsupported(self, message: str) -> None:
        self._pre_run_exit = RunExitCode.UNSUPPORTED
        self.infrastructure_reasons.append({"scope": "capability", "code": "UNSUPPORTED", "message": message})

    def user_abort(self, message: str = "operator requested abort") -> None:
        self._user_aborted = True
        self.infrastructure_reasons.append({"scope": "operator", "code": "USER_ABORT", "message": message})

    def finalize(self, *, timed_out: bool = False, require_agent_final: bool = True) -> PolicyResult:
        if timed_out and self.workload_summary is None:
            self.dut_reasons.append({"scope": "liveness", "code": "HEARTBEAT_OR_SUMMARY_TIMEOUT"})
        for metric in self.limits.required_telemetry:
            if not any(seen == metric or seen.startswith(f"{metric}.") for seen in self.telemetry_seen):
                self.infrastructure_reasons.append(
                    {"scope": "telemetry", "code": "REQUIRED_TELEMETRY_MISSING", "metric": metric}
                )
        if require_agent_final and not self.agent_final_seen:
            self.infrastructure_reasons.append({"scope": "agent", "code": "AGENT_FINAL_MISSING"})

        if self._user_aborted:
            exit_code = RunExitCode.USER_ABORT
        elif self._pre_run_exit is not None:
            exit_code = self._pre_run_exit
        elif self.infrastructure_reasons:
            exit_code = RunExitCode.INFRA_ERROR
        elif self.workload_summary is None and timed_out:
            exit_code = RunExitCode.SILENT_FAILURE
        elif self.workload_summary is None and self.agent_workload_exit_code not in (None, 0):
            exit_code = RunExitCode.DUT_FAIL
        elif self.workload_summary is None:
            exit_code = RunExitCode.INFRA_ERROR
            self.infrastructure_reasons.append({"scope": "workload", "code": "SUMMARY_MISSING"})
        elif self.dut_reasons:
            exit_code = RunExitCode.DUT_FAIL
        else:
            exit_code = RunExitCode.PASS
        return PolicyResult(
            verdict=VERDICT_BY_EXIT[exit_code],
            exit_code=int(exit_code),
            dut_reasons=list(self.dut_reasons),
            infrastructure_reasons=list(self.infrastructure_reasons),
            warnings=list(self.warnings),
            workload_result=self.workload_result,
            workload_exit_code=(
                self.workload_exit_code
                if self.workload_exit_code is not None
                else self.agent_workload_exit_code
            ),
            workload_summary=dict(self.workload_summary) if self.workload_summary else None,
            telemetry_seen=sorted(self.telemetry_seen),
            terminal_summary_seen=self.workload_summary is not None,
            agent_final_seen=self.agent_final_seen,
        )

    def _process_summary(self, payload: Mapping[str, Any], event: EventEnvelope) -> None:
        if self.workload_summary is not None:
            self._infra("protocol", "DUPLICATE_SUMMARY", payload, event)
            return
        self.workload_summary = dict(payload)
        result = payload.get("result")
        exit_code = payload.get("exit_code")
        self.workload_result = str(result) if result is not None else None
        self.workload_exit_code = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None
        if self.workload_result != "PASS" or self.workload_exit_code != 0:
            self._dut(
                "workload",
                self.workload_result or "INVALID_WORKLOAD_RESULT",
                {"result": result, "exit_code": exit_code},
                event,
            )
        if payload.get("verify_pass") is False or int(payload.get("verify_fail_count", 0) or 0) > 0:
            self._dut("correctness", "SUMMARY_VERIFY_FAIL", payload, event)
        for metric, bounds in self.limits.performance.items():
            self._check_bound(metric, payload.get(metric), bounds, "performance", event)

    def _process_telemetry(self, payload: Mapping[str, Any], event: EventEnvelope) -> None:
        metric = payload.get("metric")
        if isinstance(metric, str):
            self.telemetry_seen.add(metric)
            bounds = self.limits.telemetry.get(metric)
            if bounds is not None:
                self._check_bound(metric, payload.get("value"), bounds, "telemetry", event)
        metrics = payload.get("metrics")
        if isinstance(metrics, dict):
            for name, value in metrics.items():
                if isinstance(name, str):
                    self.telemetry_seen.add(name)
                    bounds = self.limits.telemetry.get(name)
                    if bounds is None:
                        bounds = next(
                            (
                                candidate_bounds
                                for candidate, candidate_bounds in self.limits.telemetry.items()
                                if name.startswith(f"{candidate}.")
                            ),
                            None,
                        )
                    if bounds is not None:
                        self._check_bound(name, value, bounds, "telemetry", event)

    def _check_bound(
        self,
        metric: str,
        value: Any,
        bounds: Mapping[str, Any],
        scope: str,
        event: EventEnvelope,
    ) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            self._infra(scope, "INVALID_METRIC_VALUE", {"metric": metric, "value": value}, event)
            return
        minimum = bounds.get("min")
        maximum = bounds.get("max")
        if isinstance(minimum, (int, float)) and value < minimum:
            self._dut(scope, "BELOW_MINIMUM", {"metric": metric, "value": value, "min": minimum}, event)
        if isinstance(maximum, (int, float)) and value > maximum:
            self._dut(scope, "ABOVE_MAXIMUM", {"metric": metric, "value": value, "max": maximum}, event)

    def _dut(self, scope: str, code: str, payload: Mapping[str, Any], event: EventEnvelope) -> None:
        self.dut_reasons.append({"scope": scope, "code": code, "seq": event.seq, "evidence": dict(payload)})

    def _infra(self, scope: str, code: str, payload: Mapping[str, Any], event: EventEnvelope) -> None:
        self.infrastructure_reasons.append(
            {"scope": scope, "code": code, "seq": event.seq, "evidence": dict(payload)}
        )

    def _warn(self, scope: str, code: str, payload: Mapping[str, Any], event: EventEnvelope) -> None:
        self.warnings.append({"scope": scope, "code": code, "seq": event.seq, "evidence": dict(payload)})

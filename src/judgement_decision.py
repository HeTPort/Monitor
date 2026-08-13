"""
JudgmentDecision - L4: Decision Layer

Determines test verdict based on all inputs from L1-L3 layers.
Combines PatternProcessor (fail/warn detection) and HeartbeatWatchdog
(silent failure detection) to produce final verdicts.

Part of the 5-layer architecture defined in ARCHITECTURE.md.

Verdict Logic:
    PASS        = dmesg_clean AND workload_result='PASS'
    FAIL        = dmesg_error OR workload_result='FAIL'
    SILENT_FAILURE = heartbeat timeout without result

Exit Codes:
    0 = PASS
    1 = FAIL
    2 = SILENT_FAILURE
    3 = ERROR (or RUNNING)

Author: Vmin Judge Tool Development
Version: 1.0
"""

from __future__ import annotations

import sys
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Callable
from threading import RLock
from pathlib import Path

# Add src directory to path for imports
_src_path = str(Path(__file__).parent)
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

# Import L3 components
from pattern_processor import PatternProcessor, MatchResult
from heartbeat_watchdog import HeartbeatWatchdog
from log_parser import ParsedLine

# Import centralized constants
from verdict_constants import (
    Verdict,
    ExitCode,
    LogSource,
    PatternType,
    get_verdict_description,
    # Legacy constants for backward compatibility
    VERDICT_RUNNING as _LOCAL_VERDICT_RUNNING,
    VERDICT_PASS as _LOCAL_VERDICT_PASS,
    VERDICT_FAIL as _LOCAL_VERDICT_FAIL,
    VERDICT_SILENT_FAILURE as _LOCAL_VERDICT_SILENT_FAILURE,
    EXIT_CODE_PASS as _LOCAL_EXIT_CODE_PASS,
    EXIT_CODE_FAIL as _LOCAL_EXIT_CODE_FAIL,
    EXIT_CODE_SILENT_FAILURE as _LOCAL_EXIT_CODE_SILENT_FAILURE,
    EXIT_CODE_ERROR as _LOCAL_EXIT_CODE_ERROR,
)

# Configure module logger
logger = logging.getLogger(__name__)


# =============================================================================
# Path Resolution Helper
# =============================================================================

def _resolve_rules_path(path: str) -> str:
    """
    Resolve rule configuration file path with fallback search.

    Search order:
    1. Exact path as provided
    2. configs/ subdirectory (for projects using 'configs' naming)
    3. config/ subdirectory (default)

    Args:
        path: Original path provided by user

    Returns:
        Resolved path that exists, or original path if not found
    """
    import os

    # 1. Try exact path first
    if os.path.exists(path):
        return path

    # 2. Try configs/ prefix (common alternative naming)
    if not path.startswith('configs/'):
        alt_path = 'configs/' + os.path.basename(path)
        if os.path.exists(alt_path):
            logger.debug(f"Resolved config path: {path} -> {alt_path}")
            return alt_path

    # 3. Try config/ prefix
    if not path.startswith('config/'):
        alt_path = 'config/' + os.path.basename(path)
        if os.path.exists(alt_path):
            logger.debug(f"Resolved config path: {path} -> {alt_path}")
            return alt_path

    return path  # Return original if nothing found


# =============================================================================
# Verdict Constants (re-exported from verdict_constants for backward compatibility)
# =============================================================================

VERDICT_RUNNING = _LOCAL_VERDICT_RUNNING
VERDICT_PASS = _LOCAL_VERDICT_PASS
VERDICT_FAIL = _LOCAL_VERDICT_FAIL
VERDICT_SILENT_FAILURE = _LOCAL_VERDICT_SILENT_FAILURE


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class TestState:
    """L4 State: Current test state.

    Tracks all decision-relevant state across the test lifecycle.
    """
    __test__ = False  # Exclude from pytest test collection

    dmesg_error: bool = False
    dmesg_warn_count: int = 0
    workload_result: Optional[str] = None  # 'PASS', 'FAIL', or None
    heartbeat_count: int = 0
    last_heartbeat: Optional[float] = None
    start_time: float = 0
    end_time: Optional[float] = None
    lines_processed: int = 0

    # Track what caused the verdict
    verdict_reason: str = ""

    def reset(self):
        """Reset state to initial values."""
        self.dmesg_error = False
        self.dmesg_warn_count = 0
        self.workload_result = None
        self.heartbeat_count = 0
        self.last_heartbeat = None
        self.start_time = 0
        self.end_time = None
        self.lines_processed = 0
        self.verdict_reason = ""


# =============================================================================
# JudgmentDecision Class
# =============================================================================

class JudgmentDecision:
    """
    Determines test verdict based on all inputs.

    This is the L4 (Decision Layer) component that:
    - Maintains test state (TestState)
    - Integrates PatternProcessor (L3) for fail/warn detection
    - Integrates HeartbeatWatchdog (L3) for silent failure detection
    - Produces final verdicts (PASS/FAIL/SILENT_FAILURE/RUNNING)

    Combined Judgment Logic:
        PASS  = dmesg_clean AND workload_result='PASS'
        FAIL  = dmesg_error OR workload_result='FAIL'
        FAIL  = dmesg_error AND workload_result='PASS' (dmesg error wins)
        SILENT_FAILURE = heartbeat timeout

    Usage:
        judge = JudgmentDecision(rules_path='config/rules.conf')
        judge.start()

        for line in lines:
            judge.process_line(line)

            if judge.is_complete():
                break

        verdict = judge.get_verdict()
        exit_code = judge.get_exit_code()

        judge.stop()
    """

    # Class-level constants for exit codes (use centralized constants)
    EXIT_CODE_PASS = int(ExitCode.PASS)
    EXIT_CODE_FAIL = int(ExitCode.FAIL)
    EXIT_CODE_SILENT_FAILURE = int(ExitCode.SILENT_FAILURE)
    EXIT_CODE_ERROR = int(ExitCode.ERROR)

    DEFAULT_TIMEOUT_SEC = 60

    def __init__(self, timeout_sec: int = None, rules_path: str = None):
        """
        Initialize JudgmentDecision.

        Args:
            timeout_sec: Overall test timeout in seconds (default 60).
            rules_path: Optional path to rule configuration file.
        """
        self.timeout_sec = timeout_sec if timeout_sec is not None else self.DEFAULT_TIMEOUT_SEC
        self.start_time = time.time()
        self.state = TestState()
        self._is_running = False
        self._lock = RLock()

        # Resolve config path with fallback search
        resolved_path = _resolve_rules_path(rules_path) if rules_path else None

        # Initialize L3 components
        self.pattern_processor = PatternProcessor(resolved_path) if resolved_path else PatternProcessor()
        self.heartbeat_watchdog = HeartbeatWatchdog()

        # Register callback for heartbeat timeout
        self.heartbeat_watchdog.register_timeout_callback(self._on_heartbeat_timeout)

        # Statistics
        self._lines_by_source: Dict[str, int] = {
            'dmesg': 0,
            'workload': 0,
            'hilog': 0,
            'logcat': 0,
            'unknown': 0
        }
        self._pattern_matches: Dict[str, int] = {}

        # Heartbeat pattern detection
        self._heartbeat_patterns = ['HEARTBEAT', 'heartbeat', 'KEEPALIVE']

        logger.debug(f"JudgmentDecision initialized with timeout={self.timeout_sec}s")

    # =========================================================================
    # Lifecycle Methods
    # =========================================================================

    def start(self):
        """Start the judgment process."""
        with self._lock:
            if not self._is_running:
                self._is_running = True
                self.state.start_time = time.time()
                self.heartbeat_watchdog.start()
                logger.info("JudgmentDecision started")

    def stop(self):
        """Stop the judgment process."""
        with self._lock:
            if self._is_running:
                self._is_running = False
                self.state.end_time = time.time()
                self.heartbeat_watchdog.stop()
                logger.info(f"JudgmentDecision stopped (verdict: {self.get_verdict()})")

    def reset(self):
        """Reset state for a new test session."""
        with self._lock:
            self.state.reset()
            self.start_time = time.time()
            self._lines_by_source = {k: 0 for k in self._lines_by_source}
            self._pattern_matches.clear()
            self.pattern_processor.reset_counts()
            self.heartbeat_watchdog.reset_state()
            logger.debug("JudgmentDecision reset")

    # =========================================================================
    # Line Processing
    # =========================================================================

    def process_line(self, line: ParsedLine):
        """
        Process a parsed log line.

        This method:
        1. Routes to PatternProcessor for fail/warn detection
        2. Detects heartbeat patterns and resets watchdog
        3. Detects result patterns (RESULT: PASS/FAIL)
        4. Updates state accordingly

        Args:
            line: ParsedLine from LogParser (L2)
        """
        with self._lock:
            self.state.lines_processed += 1

            # Update source statistics
            if hasattr(line, 'source'):
                self._lines_by_source[line.source] = self._lines_by_source.get(line.source, 0) + 1

            # Extract content
            content = line.content if hasattr(line, 'content') else str(line)
            timestamp = line.timestamp if hasattr(line, 'timestamp') else time.time()

            # Check for heartbeat first (before pattern matching)
            if self._is_heartbeat_line(content):
                self._handle_heartbeat(content, timestamp)
                return

            # Check for result patterns
            result_type = self._check_result_pattern(content)
            if result_type:
                self._handle_result(result_type, content)
                return

            # Pattern matching via PatternProcessor
            match_result = self.pattern_processor.evaluate(line)
            if match_result:
                self._update_stats(match_result.matched_rule_id)
                self.update(match_result)

    def process_heartbeat(self, raw_line: bytes, timestamp: float = None):
        """
        Process a heartbeat line directly.

        Args:
            raw_line: Raw heartbeat bytes.
            timestamp: Unix timestamp (uses current time if None).
        """
        with self._lock:
            if timestamp is None:
                timestamp = time.time()
            self._handle_heartbeat(raw_line.decode() if isinstance(raw_line, bytes) else raw_line, timestamp)

    def update(self, match_result: MatchResult):
        """
        Update state based on a match result from PatternProcessor.

        Args:
            match_result: MatchResult from PatternProcessor.evaluate()
        """
        with self._lock:
            if not match_result or not match_result.reached:
                return

            pattern_type = match_result.pattern_type

            if pattern_type == 'fail':
                self.state.dmesg_error = True
                self.state.verdict_reason = match_result.content
                logger.warning(f"Dmesg error detected: {match_result.content[:100]}")

            elif pattern_type == 'warn':
                self.state.dmesg_warn_count += 1
                logger.debug(f"Dmesg warning: {match_result.content[:100]}")

            elif pattern_type == 'result':
                # Check if content indicates PASS or FAIL
                content_upper = match_result.content.upper()
                if 'FAIL' in content_upper:
                    self.state.workload_result = 'FAIL'
                    self.state.verdict_reason = match_result.content
                    logger.info(f"Workload result: FAIL - {match_result.content[:100]}")
                elif 'PASS' in content_upper:
                    self.state.workload_result = 'PASS'
                    self.state.verdict_reason = match_result.content
                    logger.info(f"Workload result: PASS - {match_result.content[:100]}")

    # =========================================================================
    # Verdict Methods
    # =========================================================================

    def get_verdict(self) -> str:
        """
        Get current verdict.

        Verdict Logic:
            PASS        = workload_result='PASS' AND NOT dmesg_error
            FAIL        = dmesg_error OR workload_result='FAIL' (dmesg error wins over workload PASS)
            SILENT_FAILURE = heartbeat timeout detected
            RUNNING     = test not yet complete

        Note: PASS does not require heartbeat - a workload can report PASS
              immediately without any heartbeat (valid if workload completes quickly).

        Returns:
            One of: VERDICT_RUNNING, VERDICT_PASS, VERDICT_FAIL, VERDICT_SILENT_FAILURE
        """
        with self._lock:
            # FAIL conditions (highest priority)
            if self.state.dmesg_error:
                return VERDICT_FAIL

            if self.state.workload_result == 'FAIL':
                return VERDICT_FAIL

            # PASS conditions
            if self.state.workload_result == 'PASS' and not self.state.dmesg_error:
                return VERDICT_PASS

            # SILENT_FAILURE: heartbeat timeout
            if self.heartbeat_watchdog.was_timeout():
                return VERDICT_SILENT_FAILURE

            # Overall timeout (no result received)
            if self._is_running:
                elapsed = time.time() - self.state.start_time
                if elapsed > self.timeout_sec:
                    if self.state.heartbeat_count > 0:
                        # Had heartbeats but no result = silent failure
                        return VERDICT_SILENT_FAILURE
                    else:
                        # Never got any heartbeat = fail
                        return VERDICT_FAIL

            return VERDICT_RUNNING

    def is_complete(self) -> bool:
        """
        Check if test has reached a terminal state.

        Returns:
            True if verdict is no longer RUNNING.
        """
        return self.get_verdict() != VERDICT_RUNNING

    def get_exit_code(self) -> int:
        """
        Get exit code for current verdict.

        Returns:
            0 = PASS, 1 = FAIL, 2 = SILENT_FAILURE, 3 = ERROR/RUNNING
        """
        verdict = self.get_verdict()

        verdict_to_code = {
            VERDICT_PASS: self.EXIT_CODE_PASS,
            VERDICT_FAIL: self.EXIT_CODE_FAIL,
            VERDICT_SILENT_FAILURE: self.EXIT_CODE_SILENT_FAILURE,
            VERDICT_RUNNING: self.EXIT_CODE_ERROR,
        }

        return verdict_to_code.get(verdict, self.EXIT_CODE_ERROR)

    # =========================================================================
    # State Management
    # =========================================================================

    def set_workload_result(self, result: str):
        """
        Set workload result directly.

        Args:
            result: 'PASS' or 'FAIL'
        """
        with self._lock:
            self.state.workload_result = result
            logger.info(f"Workload result set to: {result}")

    def get_state(self) -> TestState:
        """Get current test state."""
        with self._lock:
            return TestState(
                dmesg_error=self.state.dmesg_error,
                dmesg_warn_count=self.state.dmesg_warn_count,
                workload_result=self.state.workload_result,
                heartbeat_count=self.state.heartbeat_count,
                last_heartbeat=self.state.last_heartbeat,
                start_time=self.state.start_time,
                end_time=self.state.end_time,
                lines_processed=self.state.lines_processed,
                verdict_reason=self.state.verdict_reason
            )

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """
        Get comprehensive statistics.

        Returns:
            Dictionary with all statistics.
        """
        with self._lock:
            duration = (self.state.end_time or time.time()) - self.state.start_time

            return {
                'verdict': self.get_verdict(),
                'exit_code': self.get_exit_code(),
                'is_running': self._is_running,
                'is_complete': self.is_complete(),
                'duration_sec': duration,
                'lines_processed': self.state.lines_processed,
                'source_counts': dict(self._lines_by_source),
                'pattern_matches': dict(self._pattern_matches),
                'heartbeat_count': self.state.heartbeat_count,
                'dmesg_error': self.state.dmesg_error,
                'dmesg_warn_count': self.state.dmesg_warn_count,
                'workload_result': self.state.workload_result,
                'verdict_reason': self.state.verdict_reason,
            }

    # =========================================================================
    # Private Methods
    # =========================================================================

    def _is_heartbeat_line(self, content: str) -> bool:
        """Check if line contains a heartbeat pattern."""
        content_upper = content.upper()
        for pattern in self._heartbeat_patterns:
            if pattern.upper() in content_upper:
                return True
        return False

    def _check_result_pattern(self, content: str) -> Optional[str]:
        """Check if line contains a result pattern."""
        content_upper = content.upper()

        if 'RESULT:' in content_upper:
            if 'PASS' in content_upper:
                return 'PASS'
            if 'FAIL' in content_upper:
                return 'FAIL'

        return None

    def _handle_heartbeat(self, content: str, timestamp: float):
        """Handle a heartbeat line."""
        self.state.heartbeat_count += 1
        self.state.last_heartbeat = timestamp
        self.heartbeat_watchdog.reset(timestamp)
        logger.debug(f"Heartbeat #{self.state.heartbeat_count}: {content[:50]}")

    def _handle_result(self, result: str, content: str):
        """Handle a result line."""
        self.state.workload_result = result
        self.state.verdict_reason = content
        logger.info(f"Workload result: {result} - {content[:100]}")

    def _on_heartbeat_timeout(self, elapsed: float):
        """Callback for heartbeat timeout."""
        logger.warning(f"Heartbeat timeout: {elapsed:.1f}s since last heartbeat")
        self.state.verdict_reason = f"No heartbeat for {elapsed:.1f}s"

    def _update_stats(self, rule_id: str):
        """Update pattern match statistics."""
        if rule_id:
            self._pattern_matches[rule_id] = self._pattern_matches.get(rule_id, 0) + 1

    # =========================================================================
    # Representation
    # =========================================================================

    def __repr__(self) -> str:
        stats = self.get_stats()
        return (f"JudgmentDecision(verdict={stats['verdict']}, "
                f"heartbeats={stats['heartbeat_count']}, "
                f"lines={stats['lines_processed']}, "
                f"running={stats['is_running']})")


# =============================================================================
# Run Tests
# =============================================================================

if __name__ == '__main__':
    # Simple self-test
    print("JudgmentDecision module loaded successfully")
    print(f"Verdict constants: PASS={VERDICT_PASS}, FAIL={VERDICT_FAIL}, "
          f"SILENT_FAILURE={VERDICT_SILENT_FAILURE}, RUNNING={VERDICT_RUNNING}")

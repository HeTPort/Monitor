"""
Result Formatter - L5: Output Layer

Formats test results for output (text/JSON/file).
Integrates with JudgmentDecision to produce final reports.

Part of the 5-layer architecture defined in ARCHITECTURE.md.

Verdict Logic:
    PASS        = dmesg_clean AND workload_result='PASS'
    FAIL        = dmesg_error OR workload_result='FAIL'
    SILENT_FAILURE = heartbeat timeout
    ERROR       = tool configuration/execution error

Exit Codes:
    0 = PASS
    1 = FAIL
    2 = SILENT_FAILURE
    3 = ERROR

Author: Vmin Judge Tool Development
Version: 1.0
"""

from __future__ import annotations

import sys
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional
from datetime import datetime
from pathlib import Path

# Add src directory to path for imports
_src_path = str(Path(__file__).parent)
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

# Import centralized constants
from .verdict_constants import (
    Verdict,
    ExitCode,
    get_exit_code_description,
    # Legacy constants for backward compatibility
    VERDICT_RUNNING as _LOCAL_VERDICT_RUNNING,
    VERDICT_PASS as _LOCAL_VERDICT_PASS,
    VERDICT_FAIL as _LOCAL_VERDICT_FAIL,
    VERDICT_SILENT_FAILURE as _LOCAL_VERDICT_SILENT_FAILURE,
    VERDICT_ERROR as _LOCAL_VERDICT_ERROR,
)

# Configure module logger
logger = logging.getLogger(__name__)


# =============================================================================
# Verdict Constants (re-exported from verdict_constants for backward compatibility)
# =============================================================================

VERDICT_RUNNING = _LOCAL_VERDICT_RUNNING
VERDICT_PASS = _LOCAL_VERDICT_PASS
VERDICT_FAIL = _LOCAL_VERDICT_FAIL
VERDICT_SILENT_FAILURE = _LOCAL_VERDICT_SILENT_FAILURE
VERDICT_ERROR = _LOCAL_VERDICT_ERROR


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class FormattedResult:
    """
    L5 Output: Formatted test result.

    Attributes:
        verdict: Final verdict (PASS/FAIL/SILENT_FAILURE/ERROR).
        exit_code: Unix exit code (0=PASS, 1=FAIL, 2=SILENT_FAILURE, 3=ERROR).
        duration_sec: Test duration in seconds.
        heartbeat_count: Number of heartbeats received.
        dmesg_warn_count: Number of dmesg warnings detected.
        patterns_matched: Dictionary of matched patterns and their counts.
        timestamp: Formatted timestamp when result was created.
    """
    verdict: str
    exit_code: int
    duration_sec: float
    heartbeat_count: int
    dmesg_warn_count: int
    patterns_matched: Dict[str, int] = field(default_factory=dict)
    timestamp: str = ""

    def __post_init__(self):
        """Generate timestamp if not provided."""
        if not self.timestamp:
            self.timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


# =============================================================================
# ResultFormatter Class
# =============================================================================

class ResultFormatter:
    """
    Formats test results for output.

    Responsibilities:
    - Format verdict as text (human-readable) or JSON
    - Write to stdout, file, or JSON file
    - Create FormattedResult from JudgmentDecision stats

    Usage:
        formatter = ResultFormatter()

        # Create result from JudgmentDecision stats
        stats = judgment_decision.get_stats()
        result = formatter.create_result(stats)

        # Format and output
        print(formatter.format(result, 'text'))
        formatter.write(result, 'file:output.txt')

        # Or use convenience methods
        formatter.format_and_write(stats, 'json:result.json')
    """

    # Exit code mapping (uses centralized constants)
    EXIT_CODE_MAP = {
        Verdict.PASS: int(ExitCode.PASS),
        Verdict.FAIL: int(ExitCode.FAIL),
        Verdict.SILENT_FAILURE: int(ExitCode.SILENT_FAILURE),
        Verdict.ERROR: int(ExitCode.ERROR),
        Verdict.RUNNING: int(ExitCode.ERROR),
    }

    def __init__(self):
        """Initialize the formatter."""
        pass

    def create_result(self, stats: Dict) -> FormattedResult:
        """
        Create FormattedResult from JudgmentDecision.get_stats().

        Args:
            stats: Dictionary from JudgmentDecision.get_stats() with keys:
                - verdict: str
                - exit_code: int
                - duration_sec: float
                - heartbeat_count: int
                - dmesg_warn_count: int
                - pattern_matches: Dict[str, int]
                - verdict_reason: str (optional)
                - lines_processed: int (optional)

        Returns:
            FormattedResult ready for output

        Example:
            stats = {
                'verdict': 'PASS',
                'exit_code': 0,
                'duration_sec': 45.5,
                'heartbeat_count': 10,
                'dmesg_warn_count': 2,
                'pattern_matches': {}
            }
            result = formatter.create_result(stats)
        """
        return FormattedResult(
            verdict=stats.get('verdict', 'UNKNOWN'),
            exit_code=stats.get('exit_code', 3),
            duration_sec=stats.get('duration_sec', 0.0),
            heartbeat_count=stats.get('heartbeat_count', 0),
            dmesg_warn_count=stats.get('dmesg_warn_count', 0),
            patterns_matched=stats.get('pattern_matches', {}),
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )

    def create_from_verdict(
        self,
        verdict: str,
        duration_sec: float,
        heartbeat_count: int,
        dmesg_warn_count: int = 0,
        patterns_matched: Optional[Dict[str, int]] = None
    ) -> FormattedResult:
        """
        Create FormattedResult from individual parameters.

        Args:
            verdict: Final verdict (PASS/FAIL/SILENT_FAILURE/ERROR).
            duration_sec: Test duration in seconds.
            heartbeat_count: Number of heartbeats received.
            dmesg_warn_count: Number of dmesg warnings (default 0).
            patterns_matched: Matched patterns dictionary (default empty).

        Returns:
            FormattedResult ready for output
        """
        # Convert string verdict to Verdict enum for lookup
        try:
            verdict_enum = Verdict(verdict)
        except ValueError:
            verdict_enum = Verdict.ERROR

        exit_code = self.EXIT_CODE_MAP.get(verdict_enum, int(ExitCode.ERROR))

        return FormattedResult(
            verdict=verdict,
            exit_code=exit_code,
            duration_sec=duration_sec,
            heartbeat_count=heartbeat_count,
            dmesg_warn_count=dmesg_warn_count,
            patterns_matched=patterns_matched or {},
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )

    def format(self, result: FormattedResult, format_type: str = 'text') -> str:
        """
        Format result for output.

        Args:
            result: FormattedResult to format.
            format_type: 'text' for human-readable, 'json' for JSON.

        Returns:
            Formatted string

        Raises:
            ValueError: If format_type is not 'text' or 'json'
        """
        if format_type == 'json':
            return self._format_json(result)
        elif format_type == 'text':
            return self._format_text(result)
        else:
            raise ValueError(f"Unknown format type: {format_type}. Use 'text' or 'json'.")

    def format_and_write(
        self,
        stats: Dict,
        destination: str = 'stdout',
        format_type: str = 'text'
    ) -> FormattedResult:
        """
        Convenience method: create result, format, and write in one call.

        Args:
            stats: Statistics from JudgmentDecision.get_stats().
            destination: 'stdout', 'file:<path>', or 'json:<path>'.
            format_type: 'text' or 'json'.

        Returns:
            FormattedResult that was written
        """
        result = self.create_result(stats)
        formatted = self.format(result, format_type)
        self.write(formatted, destination)
        return result

    def _format_text(self, result: FormattedResult) -> str:
        """
        Format as human-readable text.

        Output format:
            [JUDGE] PASS
            Duration: 45.5s
            Heartbeats: 10
            Warnings: 2
            Exit Code: 0
            Timestamp: 2026-08-11 14:30:00

        If patterns were matched:
            [JUDGE] FAIL
            Duration: 30.2s
            Heartbeats: 5
            Warnings: 0
            Exit Code: 1
            Timestamp: 2026-08-11 14:30:00
            Patterns Matched:
              - fail|icon|CPU hang: 2
        """
        lines = [
            f"[JUDGE] {result.verdict}",
            f"Duration: {result.duration_sec:.1f}s",
            f"Heartbeats: {result.heartbeat_count}",
            f"Warnings: {result.dmesg_warn_count}",
            f"Exit Code: {result.exit_code}",
            f"Timestamp: {result.timestamp}",
        ]

        # Add pattern matches if any
        if result.patterns_matched:
            lines.append("Patterns Matched:")
            for pattern, count in sorted(result.patterns_matched.items()):
                lines.append(f"  - {pattern}: {count}")

        return '\n'.join(lines)

    def _format_json(self, result: FormattedResult) -> str:
        """
        Format as JSON string.

        Output format:
            {
              "verdict": "PASS",
              "exit_code": 0,
              "duration_sec": 45.5,
              "heartbeat_count": 10,
              "dmesg_warn_count": 2,
              "patterns_matched": {},
              "timestamp": "2026-08-11 14:30:00"
            }
        """
        return json.dumps(result.to_dict(), indent=2)

    def _format_minimal(self, result: FormattedResult) -> str:
        """
        Format as minimal output (verdict only).

        Output format:
            PASS
        """
        return result.verdict

    def write(self, result_str: str, destination: str = 'stdout'):
        """
        Write formatted result to destination.

        Args:
            result_str: Already formatted string.
            destination: 'stdout' (print to console) or 'file:<path>' or 'json:<path>'.

        Raises:
            IOError: If file cannot be written
        """
        if destination == 'stdout':
            print(result_str)
        elif destination.startswith('file:'):
            path = destination[5:]  # Remove 'file:' prefix
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(result_str)
                    f.write('\n')  # Ensure trailing newline
                logger.info(f"Result written to {path}")
            except IOError as e:
                logger.error(f"Failed to write to {path}: {e}")
                raise
        elif destination.startswith('json:'):
            path = destination[5:]  # Remove 'json:' prefix
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(result_str)
                    f.write('\n')  # Ensure trailing newline
                logger.info(f"JSON result written to {path}")
            except IOError as e:
                logger.error(f"Failed to write JSON to {path}: {e}")
                raise
        else:
            logger.warning(f"Unknown destination: {destination}")
            print(result_str)  # Fallback to stdout

    def get_exit_code_description(self, exit_code: int) -> str:
        """
        Get human-readable description of exit code.

        Args:
            exit_code: Exit code (0, 1, 2, or 3).

        Returns:
            Human-readable description.
        """
        # Use centralized function for consistent descriptions
        return get_exit_code_description(exit_code)

    def format_summary(self, result: FormattedResult) -> str:
        """
        Format a one-line summary.

        Output format:
            PASS (0) - 45.5s, 10 heartbeats
        """
        return (
            f"{result.verdict} ({result.exit_code}) - "
            f"{result.duration_sec:.1f}s, "
            f"{result.heartbeat_count} heartbeats"
        )


# =============================================================================
# Module Test
# =============================================================================

if __name__ == '__main__':
    print("ResultFormatter module loaded successfully")
    print(f"Verdict constants: PASS={VERDICT_PASS}, FAIL={VERDICT_FAIL}, "
          f"SILENT_FAILURE={VERDICT_SILENT_FAILURE}, ERROR={VERDICT_ERROR}")

    # Example usage
    formatter = ResultFormatter()

    # Create sample stats (as if from JudgmentDecision)
    sample_stats = {
        'verdict': 'PASS',
        'exit_code': 0,
        'duration_sec': 45.5,
        'heartbeat_count': 10,
        'dmesg_warn_count': 2,
        'pattern_matches': {'warn|icon|cpu frequency': 1}
    }

    result = formatter.create_result(sample_stats)

    print("\n--- Text Format ---")
    print(formatter.format(result, 'text'))

    print("\n--- JSON Format ---")
    print(formatter.format(result, 'json'))

    print("\n--- Minimal Format ---")
    print(result.verdict)

    print("\n--- Summary ---")
    print(formatter.format_summary(result))

    print("\n--- Exit Code Description ---")
    print(formatter.get_exit_code_description(0))
    print(formatter.get_exit_code_description(2))

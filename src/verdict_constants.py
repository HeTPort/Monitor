"""
Verdict Constants and Enums - Centralized Definitions

This module provides centralized constants and enums for all verdict-related
values used across the 5-layer architecture.

Usage:
    from verdict_constants import Verdict, ExitCode, LogSource

    if result.verdict == Verdict.PASS:
        print("Test passed!")

    exit_code = ExitCode.from_verdict(Verdict.PASS)

Author: Vmin Judge Tool Development
Version: 1.0
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Dict


# =============================================================================
# Verdict Enum
# =============================================================================

class Verdict(str, Enum):
    """
    Test verdict enumeration.

    Values:
        RUNNING: Test is still in progress
        PASS: Test completed successfully with no failures
        FAIL: Test failed (dmesg error or workload FAIL)
        SILENT_FAILURE: Process hung/crashed without explicit error
        ERROR: Tool configuration or execution error
    """
    RUNNING = 'RUNNING'
    PASS = 'PASS'
    FAIL = 'FAIL'
    SILENT_FAILURE = 'SILENT_FAILURE'
    ERROR = 'ERROR'

    @classmethod
    def is_terminal(cls, verdict: "Verdict") -> bool:
        """Check if verdict is a terminal (non-RUNNING) state."""
        return verdict != cls.RUNNING

    @classmethod
    def is_success(cls, verdict: "Verdict") -> bool:
        """Check if verdict indicates success."""
        return verdict == cls.PASS

    @classmethod
    def is_failure(cls, verdict: "Verdict") -> bool:
        """Check if verdict indicates failure (including SILENT_FAILURE)."""
        return verdict in (cls.FAIL, cls.SILENT_FAILURE)


# =============================================================================
# Exit Code Enum
# =============================================================================

class ExitCode(int, Enum):
    """
    Unix exit code enumeration.

    Exit codes are used to communicate test results to the shell/OS.
    These follow standard conventions for test frameworks.
    """
    PASS = 0              # Test completed successfully
    FAIL = 1              # Failure pattern detected
    SILENT_FAILURE = 2    # Heartbeat timeout
    ERROR = 3             # Tool configuration or execution error

    @classmethod
    def from_verdict(cls, verdict: Verdict) -> "ExitCode":
        """Convert a Verdict to its corresponding exit code."""
        mapping: Dict[Verdict, ExitCode] = {
            Verdict.PASS: cls.PASS,
            Verdict.FAIL: cls.FAIL,
            Verdict.SILENT_FAILURE: cls.SILENT_FAILURE,
            Verdict.ERROR: cls.ERROR,
            Verdict.RUNNING: cls.ERROR,  # RUNNING treated as ERROR for exit code
        }
        return mapping.get(verdict, cls.ERROR)

    @property
    def description(self) -> str:
        """Get human-readable description of exit code."""
        descriptions = {
            0: "PASS - Test completed successfully",
            1: "FAIL - Failure pattern detected",
            2: "SILENT_FAILURE - Heartbeat timeout",
            3: "ERROR - Tool configuration or execution error",
        }
        return descriptions.get(self.value, f"Unknown exit code: {self.value}")


# =============================================================================
# Log Source Enum
# =============================================================================

class LogSource(str, Enum):
    """
    Log source enumeration for categorizing log lines.

    These represent the different sources of log data captured from
    the serial stream.
    """
    DMESG = 'dmesg'           # Kernel log with timestamp format [123.456]
    WORKLOAD = 'workload'     # Workload output (HEARTBEAT, RESULT)
    HILOG = 'hilog'           # HarmonyOS log (YYYY-MM-DD format)
    LOGCAT = 'logcat'         # Android log (MM-DD HH:MM:SS format)
    UNKNOWN = 'unknown'       # Unidentified source

    @classmethod
    def is_critical_source(cls, source: "LogSource") -> bool:
        """Check if source is one where errors trigger FAIL verdict."""
        return source in (cls.DMESG,)


# =============================================================================
# Pattern Type Enum
# =============================================================================

class PatternType(str, Enum):
    """
    Pattern type enumeration for rule categorization.

    Patterns are categorized as ignore, warn, or fail based on
    their severity and impact on the verdict.
    """
    IGNORE = 'ignore'     # Pattern to ignore (suppress logging)
    WARN = 'warn'         # Warning pattern (logged but doesn't fail)
    FAIL = 'fail'         # Failure pattern (triggers FAIL verdict)
    HEARTBEAT = 'heartbeat'  # Heartbeat pattern (resets watchdog)
    RESULT = 'result'     # Result pattern (RESULT: PASS/FAIL)


# =============================================================================
# Match Type Enum (for pattern matching)
# =============================================================================

class MatchType(str, Enum):
    """
    Pattern match type enumeration.

    These represent the different ways patterns can be matched
    against log line content.
    """
    CONTAINS = 'contains'     # Substring match (case-sensitive)
    EXACT = 'exact'           # Full string match (case-sensitive)
    REGEX = 'regex'           # Regular expression match
    ICONTAINS = 'icontains'   # Substring match (case-insensitive)
    IEXACT = 'iexact'         # Full string match (case-insensitive)
    IREGEX = 'iregex'         # Regex match (case-insensitive)

    @classmethod
    def from_string(cls, type_str: str) -> "MatchType":
        """Convert string to MatchType (for config file parsing)."""
        mapping = {
            'contains': cls.CONTAINS,
            'exact': cls.EXACT,
            'regex': cls.REGEX,
            'icontains': cls.ICONTAINS,
            'iexact': cls.IEXACT,
            'iregex': cls.IREGEX,
        }
        return mapping.get(type_str.lower(), cls.CONTAINS)


# =============================================================================
# Channel Type Enum
# =============================================================================

class ChannelType(str, Enum):
    """Device channel type enumeration."""
    HDC = 'hdc'   # HiSilicon Device Connector (Huawei)
    ADB = 'adb'   # Android Debug Bridge (Generic Android)


# =============================================================================
# Test Status Enum
# =============================================================================

class TestStatus(str, Enum):
    """Test execution status enumeration."""
    PENDING = 'pending'       # Workload not yet started
    RUNNING = 'running'       # Workload currently executing
    COMPLETED = 'completed'   # Workload finished (check verdict for result)
    TIMEOUT = 'timeout'       # Overall test timeout exceeded


# =============================================================================
# Re-export for convenience
# =============================================================================

# Legacy constants for backward compatibility
# These allow existing code to import from verdict_constants
VERDICT_RUNNING = Verdict.RUNNING
VERDICT_PASS = Verdict.PASS
VERDICT_FAIL = Verdict.FAIL
VERDICT_SILENT_FAILURE = Verdict.SILENT_FAILURE
VERDICT_ERROR = Verdict.ERROR

EXIT_CODE_PASS = ExitCode.PASS
EXIT_CODE_FAIL = ExitCode.FAIL
EXIT_CODE_SILENT_FAILURE = ExitCode.SILENT_FAILURE
EXIT_CODE_ERROR = ExitCode.ERROR

# Source constants
SOURCE_DMESG = LogSource.DMESG
SOURCE_WORKLOAD = LogSource.WORKLOAD
SOURCE_HILOG = LogSource.HILOG
SOURCE_LOGCAT = LogSource.LOGCAT
SOURCE_UNKNOWN = LogSource.UNKNOWN


def get_verdict_description(verdict: Verdict) -> str:
    """
    Get human-readable description of a verdict.

    Args:
        verdict: The verdict to describe.

    Returns:
        Human-readable description string.
    """
    descriptions = {
        Verdict.RUNNING: "Test is still in progress",
        Verdict.PASS: "Test completed successfully",
        Verdict.FAIL: "Failure pattern detected (dmesg error or workload FAIL)",
        Verdict.SILENT_FAILURE: "Process hung/crashed without explicit error (heartbeat timeout)",
        Verdict.ERROR: "Tool configuration or execution error",
    }
    return descriptions.get(verdict, "Unknown verdict")


def get_exit_code_description(exit_code: int) -> str:
    """
    Get human-readable description of an exit code.

    Args:
        exit_code: The exit code to describe.

    Returns:
        Human-readable description string, or a generic error message
        if the exit code is not recognized.
    """
    try:
        return ExitCode(exit_code).description
    except ValueError:
        return f"Unknown exit code: {exit_code}"

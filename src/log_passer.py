"""
Log Parser - L2: Ingestion Layer

Parses raw log lines into structured data.
Identifies source (dmesg vs workload) and adds metadata.

Part of the 5-layer architecture defined in ARCHITECTURE.md.

Author: Vmin Judge Tool Development
Version: 1.0
"""

from __future__ import annotations

import re
import time
import logging
from typing import Optional, List, Dict, Callable
from dataclasses import dataclass, field
from pathlib import Path

# Configure module logger
logger = logging.getLogger(__name__)


# Source identification patterns
DMESG_TIMESTAMP_PATTERN = re.compile(rb'^\[\s*\d+\.\d+\]')
WORKLOAD_PATTERNS = [
    re.compile(rb'HEARTBEAT'),
    re.compile(rb'RESULT:'),
    re.compile(rb'Processing'),
    re.compile(rb'GPU|CPU'),
]
HILOG_PATTERNS = [
    re.compile(rb'^\d{4}-\d{2}-\d{2}'),
    re.compile(rb'\[\s*\d{4}/\d{2}/\d{2}'),
]
LOGCAT_PATTERNS = [
    re.compile(rb'^\w{2}-\d{2}\s+\d{2}:\d{2}:\d{2}'),
    re.compile(rb'/Android:'),
]


@dataclass
class ParsedLine:
    """
    L2 Output: Parsed log line with metadata.

    Attributes:
        raw: Raw bytes from serial port.
        source: Source identifier ('dmesg', 'workload', 'hilog', 'logcat', 'unknown').
        content: Decoded string content.
        timestamp: Unix timestamp when parsed.
        line_number: Sequential line number for debugging.
        metadata: Additional metadata extracted from line.
    """
    raw: bytes
    source: str
    content: str
    timestamp: float
    line_number: int
    metadata: Dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.source}] {self.content}"

    def is_dmesg(self) -> bool:
        """Check if source is dmesg."""
        return self.source == 'dmesg'

    def is_workload(self) -> bool:
        """Check if source is workload."""
        return self.source == 'workload'

    def is_hilog(self) -> bool:
        """Check if source is hilog."""
        return self.source == 'hilog'

    def is_logcat(self) -> bool:
        """Check if source is logcat."""
        return self.source == 'logcat'

    def is_unknown(self) -> bool:
        """Check if source is unknown."""
        return self.source == 'unknown'


@dataclass
class ParseConfig:
    """Configuration for LogParser behavior."""
    # Source identification
    default_unknown_to_workload: bool = True  # Conservative for PASS
    case_sensitive_source_id: bool = False

    # Decoding
    encoding: str = 'utf-8'
    fallback_encodings: List[str] = field(default_factory=lambda: ['latin-1', 'gbk', 'cp1252'])
    errors: str = 'replace'  # 'replace', 'ignore', 'strict'

    # Line limits
    max_line_length: int = 10000  # Truncate lines longer than this
    strip_whitespace: bool = True

    # Special patterns for result detection
    result_patterns: Dict[str, re.Pattern] = field(default_factory=lambda: {
        'pass': re.compile(rb'RESULT:\s*PASS', re.IGNORECASE),
        'fail': re.compile(rb'RESULT:\s*FAIL', re.IGNORECASE),
        'heartbeat': re.compile(rb'HEARTBEAT', re.IGNORECASE),
    })


class LogParser:
    """
    Parses raw lines into structured ParsedLine objects.

    Identifies log source (dmesg, workload, hilog, logcat) based on patterns.
    Adds timestamp and line number for each parsed line.

    Thread Safety: The parse() method is thread-safe. Line numbers are
    protected by a lock to prevent race conditions in concurrent use.

    Usage:
        parser = LogParser()
        parsed = parser.parse(b'[123.456] kernel: test message')
        print(parsed.source)  # 'dmesg'
        print(parsed.content)  # 'kernel: test message'
    """

    def __init__(self, config: Optional[ParseConfig] = None):
        """
        Initialize LogParser.

        Thread Safety: Creates a lock for thread-safe parsing.

        Args:
            config: ParseConfig for parser behavior. Uses defaults if None.
        """
        self._config = config or ParseConfig()
        self._line_number = 0
        self._lock = __import__('threading').Lock()
        self._source_stats: Dict[str, int] = {
            'dmesg': 0,
            'workload': 0,
            'hilog': 0,
            'logcat': 0,
            'unknown': 0,
        }
        self._parse_errors = 0

    @property
    def line_number(self) -> int:
        """Get current line number."""
        return self._line_number

    @property
    def source_stats(self) -> Dict[str, int]:
        """Get source statistics."""
        return dict(self._source_stats)

    @property
    def parse_errors(self) -> int:
        """Get number of parse errors."""
        return self._parse_errors

    def parse(self, raw_line: bytes, timestamp: Optional[float] = None) -> ParsedLine:
        """
        Parse a raw line into a ParsedLine.

        Thread-safe: Uses lock to protect shared state (line_number, stats).
        Multiple threads can call parse() concurrently without race conditions.

        Args:
            raw_line: Raw bytes from serial port.
            timestamp: Unix timestamp (uses current time if None).

        Returns:
            ParsedLine with source, content, and metadata.
        """
        if timestamp is None:
            timestamp = time.time()

        # Thread-safe line number increment
        with self._lock:
            self._line_number += 1
            line_num = self._line_number

        # Decode the line (not thread-sensitive, no lock needed)
        content = self._decode_line(raw_line)

        # Strip whitespace if configured
        if self._config.strip_whitespace:
            content = content.strip()

        # Truncate if too long
        if len(content) > self._config.max_line_length:
            content = content[:self._config.max_line_length] + "... [TRUNCATED]"

        # Identify source
        source = self._identify_source(raw_line)

        # Extract metadata
        metadata = self._extract_metadata(raw_line, content)

        # Update stats (thread-safe: use lock)
        with self._lock:
            self._source_stats[source] = self._source_stats.get(source, 0) + 1

        return ParsedLine(
            raw=raw_line,
            source=source,
            content=content,
            timestamp=timestamp,
            line_number=line_num,
            metadata=metadata
        )

    def parse_many(self, raw_lines: List[bytes], timestamp: Optional[float] = None) -> List[ParsedLine]:
        """
        Parse multiple lines.

        Args:
            raw_lines: List of raw bytes.
            timestamp: Base timestamp (current time used if None).

        Returns:
            List of ParsedLine objects.
        """
        if timestamp is None:
            timestamp = time.time()

        results = []
        for raw in raw_lines:
            parsed = self.parse(raw, timestamp)
            results.append(parsed)

        return results

    def _decode_line(self, raw_line: bytes) -> str:
        """Decode bytes to string using configured encodings."""
        for encoding in [self._config.encoding] + self._config.fallback_encodings:
            try:
                return raw_line.decode(encoding, errors=self._config.errors)
            except (UnicodeDecodeError, LookupError):
                continue

        # Fallback: decode with errors='replace'
        self._parse_errors += 1
        return raw_line.decode('utf-8', errors='replace')

    def _identify_source(self, raw_line: bytes) -> str:
        """
        Identify the source of the log line.

        Priority (order matters!):
        1. dmesg - kernel timestamp format [123.456] (most specific, check first)
        2. hilog - HarmonyOS timestamp format (YYYY-MM-DD or [timestamp])
        3. logcat - Android timestamp format (MM-DD HH:MM:SS)
        4. workload - explicit patterns (HEARTBEAT, RESULT, Processing, GPU/CPU)
        5. unknown - default based on config

        IMPORTANT: Pattern matching order is critical. Lines matching multiple
        sources should be classified according to this priority order.

        Examples:
        - "[ 1.234] message" -> dmesg (kernel timestamp)
        - "2026-01-01 12:00:00 message" -> hilog (full date)
        - "01-01 12:00:00 message" -> logcat (short date)
        - "HEARTBEAT: iteration=1" -> workload (explicit pattern)
        - "[ 1.234] GPU frequency changed" -> dmesg (has timestamp prefix)
        """
        # Check for dmesg FIRST (most specific pattern - requires timestamp prefix)
        if DMESG_TIMESTAMP_PATTERN.search(raw_line):
            return 'dmesg'

        # Check for hilog (HarmonyOS format with full date)
        for pattern in HILOG_PATTERNS:
            if pattern.search(raw_line):
                return 'hilog'

        # Check for logcat (Android format with short date)
        for pattern in LOGCAT_PATTERNS:
            if pattern.search(raw_line):
                return 'logcat'

        # Check for workload patterns (explicit markers)
        for pattern in WORKLOAD_PATTERNS:
            if pattern.search(raw_line):
                return 'workload'

        # Default: workload (conservative, assumes workload output is safe)
        if self._config.default_unknown_to_workload:
            return 'workload'

        return 'unknown'

    def _extract_metadata(self, raw_line: bytes, content: str) -> Dict[str, str]:
        """
        Extract metadata from the line.

        Extracts:
        - timestamp (if present)
        - result type (pass/fail)
        - heartbeat iteration (if present)
        """
        metadata = {}

        # Extract dmesg timestamp
        timestamp_match = re.search(rb'\[(\d+\.\d+)\]', raw_line)
        if timestamp_match:
            metadata['dmesg_timestamp'] = timestamp_match.group(1).decode('ascii')

        # Check for result patterns
        for result_type, pattern in self._config.result_patterns.items():
            if pattern.search(raw_line):
                metadata['result'] = result_type
                break

        # Extract heartbeat iteration
        hb_match = re.search(rb'iteration[=:](\d+)', raw_line, re.IGNORECASE)
        if hb_match:
            metadata['heartbeat_iteration'] = hb_match.group(1).decode('ascii')

        # Extract GPU/CPU frequency if present
        freq_match = re.search(rb'(\d+)\s*(MHz|GHz)', raw_line, re.IGNORECASE)
        if freq_match:
            metadata['frequency'] = freq_match.group(0).decode('ascii')

        return metadata

    def check_result(self, line: ParsedLine) -> Optional[str]:
        """
        Check if line contains a result indicator.

        Args:
            line: ParsedLine to check.

        Returns:
            'pass', 'fail', 'heartbeat', or None.
        """
        result = line.metadata.get('result')
        if result:
            return result

        # Check content directly
        content_lower = line.content.lower()
        if 'result:' in content_lower:
            if 'pass' in content_lower:
                return 'pass'
            if 'fail' in content_lower:
                return 'fail'

        if 'heartbeat' in content_lower:
            return 'heartbeat'

        return None

    def is_pass(self, line: ParsedLine) -> bool:
        """Check if line indicates PASS result."""
        return self.check_result(line) == 'pass'

    def is_fail(self, line: ParsedLine) -> bool:
        """Check if line indicates FAIL result."""
        return self.check_result(line) == 'fail'

    def is_heartbeat(self, line: ParsedLine) -> bool:
        """Check if line is a heartbeat."""
        return self.check_result(line) == 'heartbeat'

    def reset(self) -> None:
        """Reset parser state."""
        self._line_number = 0
        self._source_stats = {
            'dmesg': 0,
            'workload': 0,
            'hilog': 0,
            'logcat': 0,
            'unknown': 0,
        }
        self._parse_errors = 0

    def get_stats(self) -> dict:
        """Get parser statistics."""
        return {
            'lines_parsed': self._line_number,
            'source_stats': dict(self._source_stats),
            'parse_errors': self._parse_errors,
            'dominant_source': max(self._source_stats, key=self._source_stats.get) if self._source_stats else 'none'
        }

    def __repr__(self) -> str:
        return f"LogParser(line={self._line_number}, sources={self._source_stats})"


class StreamingLogParser(LogParser):
    """
    LogParser with streaming support for continuous parsing.

    Maintains state between parses for context-aware parsing.
    Tracks heartbeat counts, consecutive unknown lines, and last dmesg time.

    Thread Safety: Inherits thread-safety from LogParser. ALL streaming state
    modifications (_heartbeat_count, _consecutive_unknown, _last_dmesg_time) are
    protected by the parent class lock to prevent race conditions.
    """

    def __init__(self, config: Optional[ParseConfig] = None):
        super().__init__(config)
        self._last_dmesg_time: Optional[float] = None
        self._consecutive_unknown = 0
        self._heartbeat_count = 0  # Initialize to 0

    def parse(self, raw_line: bytes, timestamp: Optional[float] = None) -> ParsedLine:
        """
        Parse with streaming context awareness.

        Thread-safe: Uses parent lock to protect streaming state updates.
        """
        # First, call parent parse (handles line number and source stats with lock)
        parsed = super().parse(raw_line, timestamp)

        # Update streaming state WITH the parent's lock to prevent race conditions
        with self._lock:
            if parsed.source == 'dmesg':
                self._last_dmesg_time = parsed.timestamp
                self._consecutive_unknown = 0
            elif parsed.source == 'unknown':
                self._consecutive_unknown += 1
            else:
                self._consecutive_unknown = 0

            # Count heartbeats
            if self.is_heartbeat(parsed):
                self._heartbeat_count += 1

            # Add streaming metadata (read current values under lock)
            parsed.metadata['consecutive_unknown'] = self._consecutive_unknown
            parsed.metadata['heartbeat_count'] = self._heartbeat_count

        return parsed

    def get_streaming_stats(self) -> dict:
        """Get streaming-specific statistics."""
        # Get base stats (includes line number and source stats)
        base = self.get_stats()

        # Get streaming stats (need lock for consistency)
        with self._lock:
            base.update({
                'heartbeat_count': self._heartbeat_count,
                'consecutive_unknown': self._consecutive_unknown,
                'last_dmesg_time': self._last_dmesg_time,
            })

        return base

    def reset_streaming(self) -> None:
        """Reset streaming state (thread-safe)."""
        with self._lock:
            self._last_dmesg_time = None
            self._consecutive_unknown = 0
            self._heartbeat_count = 0

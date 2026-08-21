"""
Pattern Processor - L3: Processing Layer

Evaluates parsed log lines against rule patterns.
Implements 6 match types (contains, exact, regex, icontains, iexact, iregex)
with threshold-based triggering.

Part of the 5-layer architecture defined in ARCHITECTURE.md.

Author: Vmin Judge Tool Development
Version: 1.0
"""

from __future__ import annotations

import re
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Set
from threading import RLock

# Configure module logger
logger = logging.getLogger(__name__)


class MatchType(Enum):
    """Pattern matching types - ported from C++ types.h"""
    CONTAINS = 0     # Substring match (case-sensitive)
    EXACT = 1        # Full string match (case-sensitive)
    REGEX = 2        # Regular expression
    ICONTAINS = 3    # Substring match (case-insensitive)
    IEXACT = 4       # Full string match (case-insensitive)
    IREGEX = 5       # Regex (case-insensitive)

    @classmethod
    def from_string(cls, type_str: str) -> 'MatchType':
        """Convert string to MatchType."""
        type_map = {
            'contains': cls.CONTAINS,
            'exact': cls.EXACT,
            'regex': cls.REGEX,
            'icontains': cls.ICONTAINS,
            'iexact': cls.IEXACT,
            'iregex': cls.IREGEX,
        }
        return type_map.get(type_str.lower(), cls.CONTAINS)


@dataclass
class Rule:
    """
    Single rule definition - ported from C++ types.h Rule struct.

    Config file format: type|pattern|threshold=N
    Examples:
        contains|Kernel panic
        regex|.*watchdog.*BUG.*|threshold=3
        icontains|cpu hang
    """
    type: MatchType
    pattern: str
    threshold: int = 1  # Trigger only after N matches
    id: str = ""        # Unique identifier for hit tracking
    name: str = ""      # Human-readable name (extracted from pattern)

    # Runtime state (not in config)
    current_hit_count: int = field(default=0, repr=False)
    _compiled_regex: Optional[re.Pattern] = field(default=None, repr=False)

    def __post_init__(self):
        """Compile regex patterns for performance."""
        if self.type in (MatchType.REGEX, MatchType.IREGEX):
            flags = re.IGNORECASE if self.type == MatchType.IREGEX else 0
            try:
                self._compiled_regex = re.compile(self.pattern, flags)
            except re.error as e:
                logger.warning(f"Invalid regex pattern '{self.pattern}': {e}")
                self._compiled_regex = None

    def match(self, line: str) -> bool:
        """
        Execute pattern matching against a line.

        Args:
            line: Line content to match against.

        Returns:
            True if pattern matches, False otherwise.
        """
        if not line:
            return False

        try:
            if self.type == MatchType.CONTAINS:
                return self.pattern in line
            elif self.type == MatchType.EXACT:
                return line == self.pattern
            elif self.type == MatchType.REGEX:
                if self._compiled_regex:
                    return bool(self._compiled_regex.search(line))
                return bool(re.search(self.pattern, line))
            elif self.type == MatchType.ICONTAINS:
                return self.pattern.lower() in line.lower()
            elif self.type == MatchType.IEXACT:
                return line.lower() == self.pattern.lower()
            elif self.type == MatchType.IREGEX:
                if self._compiled_regex:
                    return bool(self._compiled_regex.search(line))
                return bool(re.search(self.pattern, line, re.IGNORECASE))
        except re.error as e:
            logger.warning(f"Regex error in pattern '{self.pattern}': {e}")
            return False

        return False

    def reset(self):
        """Reset hit counter."""
        self.current_hit_count = 0


@dataclass
class MatchResult:
    """
    Result of pattern evaluation - ported from C++ types.h MatchResult.

    Attributes:
        matched: Whether any pattern matched.
        reached: Whether threshold was reached (for triggering).
        matched_rule_id: Which rule matched (empty if no match).
        pattern_name: Human-readable rule name.
        count: Hit count when threshold reached.
        pattern_type: 'fail', 'warn', 'ignore', 'heartbeat', 'result'.
        content: The matched line content.
    """
    matched: bool = False
    reached: bool = False
    matched_rule_id: str = ""
    pattern_name: str = ""
    count: int = 0
    pattern_type: str = ""  # 'fail', 'warn', 'ignore', 'heartbeat', 'result'
    content: str = ""


@dataclass
class RuleSet:
    """
    Collection of rules by category - ported from C++ types.h RuleSet.

    Single ruleset for PC-side (unlike C++ which has dmesg/hilog/logcat RuleSets).
    """
    ignore_rules: List[Rule] = field(default_factory=list)
    warn_rules: List[Rule] = field(default_factory=list)
    fail_rules: List[Rule] = field(default_factory=list)

    def get_all_rules(self) -> List[Rule]:
        """Get all rules in a flat list."""
        return self.ignore_rules + self.warn_rules + self.fail_rules

    def reset_all_counts(self):
        """Reset hit counts for all rules."""
        for rule in self.get_all_rules():
            rule.reset()


class PatternProcessor:
    """
    Evaluates parsed lines against rule patterns.

    Responsibilities:
    - Load and parse rule configuration (.conf format)
    - Match patterns with 6 match types (contains, exact, regex, icontains, iexact, iregex)
    - Track hit counts for threshold-based triggering
    - Thread-safe rule management with RLock (Python equivalent of C++ shared_mutex)

    Config file format (from C++ rules.cpp):
        [section_name]
        type|pattern|threshold=N

    Example:
        [dmesg_fail]
        contains|Kernel panic
        regex|.*watchdog.*BUG.*soft lockup.*|threshold=3
        icontains|CPU hang

    Usage:
        processor = PatternProcessor()
        processor.load_rules('config/cpu_judge.conf')

        parsed_line = parser.parse(raw_line)
        result = processor.evaluate(parsed_line)
        if result and result.reached:
            print(f"Pattern matched: {result.pattern_name}")
    """

    def __init__(self, config_path: str = None):
        """
        Initialize PatternProcessor.

        Args:
            config_path: Optional path to rule configuration file.
        """
        self._lock = RLock()  # Thread-safe, supports multiple readers
        self._rule_set = RuleSet()
        self._version = 0

        # Statistics
        self._match_stats: Dict[str, int] = {}

        if config_path:
            self.load_rules(config_path)

    @property
    def version(self) -> int:
        """Get current rule configuration version."""
        return self._version

    @property
    def match_stats(self) -> Dict[str, int]:
        """Get pattern match statistics."""
        return dict(self._match_stats)

    def load_rules(self, config_path: str) -> bool:
        """
        Load rules from config file - ported from C++ rules.cpp loadRulesFromFileInternal().

        Returns True on success, False on failure.
        Uses fallback rules if file cannot be opened.

        Args:
            config_path: Path to rule configuration file.

        Returns:
            True if loaded successfully, False otherwise.
        """
        with self._lock:
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    return self._parse_config(f, config_path)
            except FileNotFoundError:
                logger.warning(f"Rule file not found: {config_path}")
                return False
            except IOError as e:
                logger.error(f"Error reading rule file {config_path}: {e}")
                return False

    def _parse_config(self, file_obj, source_path: str = "") -> bool:
        """
        Parse config file - ported from C++ rules.cpp buildRule() and addToLocal().

        Args:
            file_obj: File-like object containing config.
            source_path: Original file path for error messages.

        Returns:
            True if parsing succeeded.
        """
        current_section = None
        line_number = 0

        for line in file_obj:
            line_number += 1
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith('#') or line.startswith(';'):
                continue

            # Parse section header
            if line.startswith('[') and line.endswith(']'):
                current_section = line[1:-1].strip()
                continue

            if not current_section:
                logger.warning(f"Rule without section at line {line_number} in {source_path}")
                continue

            # Parse rule: type|pattern|threshold=N
            rule = self._parse_rule(line, current_section, line_number)
            if rule:
                self._add_rule(rule, current_section)

        self._version += 1
        logger.info(f"Loaded rules: {len(self._rule_set.fail_rules)} fail, "
                   f"{len(self._rule_set.warn_rules)} warn, "
                   f"{len(self._rule_set.ignore_rules)} ignore rules")
        return True

    def _parse_rule(self, raw_line: str, section: str, line_number: int = 0) -> Optional[Rule]:
        """
        Parse single rule line - ported from C++ rules.cpp buildRule().

        Args:
            raw_line: Raw line from config file.
            section: Current section name.
            line_number: Line number for error reporting.

        Returns:
            Rule object if parsing succeeded, None otherwise.
        """
        parts = raw_line.split('|')
        type_str = parts[0].strip()
        
        threshold = 1
        pattern = ""
        
        if len(parts) >= 3 and parts[-1].strip().startswith('threshold='):
           
            try:
                threshold = int(parts[-1].strip().split('=')[1])
            except (ValueError, IndexError):
                logger.warning(f"Invalid threshold in section {section}, line {line_number}")
           
            pattern = '|'.join(parts[1:-1]).strip()
        elif len(parts) >= 2:
            
            pattern = '|'.join(parts[1:]).strip()
        else:
            
            type_str = 'contains'
            pattern = parts[0].strip()
            
        match_type = MatchType.from_string(type_str)

        # Extract name from pattern (for display)
        name = pattern[:50] + "..." if len(pattern) > 50 else pattern

        # Create unique rule ID
        rule_id = f"{section}|{match_type.name}|{pattern[:30]}|t={threshold}"

        return Rule(
            type=match_type,
            pattern=pattern,
            threshold=threshold,
            id=rule_id,
            name=name
        )

    def _add_rule(self, rule: Rule, section: str):
        """
        Add rule to appropriate list - ported from C++ rules.cpp addToLocal().

        Args:
            rule: Rule to add.
            section: Section name determines rule type.
        """
        section_lower = section.lower()

        if section_lower in ('dmesg_ignore', 'ignore', 'hilog_ignore', 'logcat_ignore'):
            self._rule_set.ignore_rules.append(rule)
        elif section_lower in ('dmesg_fail', 'fail', 'hilog_fail', 'logcat_fail'):
            self._rule_set.fail_rules.append(rule)
        elif section_lower in ('dmesg_warn', 'warn', 'hilog_warn', 'logcat_warn'):
            self._rule_set.warn_rules.append(rule)
        # Note: Other section names are logged but ignored

    def evaluate(self, line) -> Optional[MatchResult]:
        """
        Evaluate line against all rules - ported from C++ rules.cpp matchRulesWithThreshold().

        Processing order (matches C++ processLineInternal()):
        1. Check ignore rules first → return None if matched (suppress)
        2. Check fail rules → trigger FAIL if threshold reached
        3. Check warn rules → log warning if threshold reached

        Returns MatchResult with reached=True when threshold is met.

        Args:
            line: Either a ParsedLine object or a string.

        Returns:
            MatchResult if pattern matched and threshold reached, None if suppressed or no match.
        """
        # Extract content from line
        if hasattr(line, 'content'):
            content = line.content
        else:
            content = str(line)

        with self._lock:  # Read lock (compatible with hot-reload)
            # 1. Check ignore rules
            ignore_result = self._match_rules(content, self._rule_set.ignore_rules, 'ignore')
            if ignore_result and ignore_result.reached:
                return None  # Suppressed by ignore rule

            # 2. Check fail rules (any fail triggers FAIL)
            fail_result = self._match_rules(content, self._rule_set.fail_rules, 'fail')
            if fail_result:
                fail_result.pattern_type = 'fail'
                self._update_stats(fail_result.matched_rule_id)
                return fail_result

            # 3. Check warn rules
            warn_result = self._match_rules(content, self._rule_set.warn_rules, 'warn')
            if warn_result:
                warn_result.pattern_type = 'warn'
                self._update_stats(warn_result.matched_rule_id)
                return warn_result

            return None

    def _match_rules(self, content: str, rules: List[Rule], pattern_type: str) -> Optional[MatchResult]:
        """
        Match against rule list - ported from C++ rules.cpp matchRulesWithThreshold().
        Implements threshold counting with atomic increment.

        Args:
            content: Line content to match.
            rules: List of rules to check.
            pattern_type: Type of pattern for result.

        Returns:
            MatchResult if threshold reached, None otherwise.
        """
        for rule in rules:
            if rule.match(content):
                # Increment hit count atomically
                rule.current_hit_count += 1
                count = rule.current_hit_count

                if count >= rule.threshold:
                    return MatchResult(
                        matched=True,
                        reached=True,
                        matched_rule_id=rule.id,
                        pattern_name=rule.name,
                        count=count,
                        pattern_type=pattern_type,
                        content=content
                    )
        return None

    def _update_stats(self, rule_id: str):
        """Update match statistics for a rule."""
        if rule_id:
            self._match_stats[rule_id] = self._match_stats.get(rule_id, 0) + 1

    def evaluate_simple(self, content: str) -> Optional[MatchResult]:
        """
        Evaluate string content directly against rules.

        This is a convenience method that doesn't require a ParsedLine object.

        Args:
            content: Line content to evaluate.

        Returns:
            MatchResult if pattern matched, None otherwise.
        """
        return self.evaluate(content)

    def reset_counts(self):
        """Reset all hit counters - useful for new test session."""
        with self._lock:
            self._rule_set.reset_all_counts()
            self._match_stats.clear()

    def get_rule_counts(self) -> Dict[str, Dict[str, int]]:
        """
        Get current hit counts for all rules.

        Returns:
            Dictionary with rule counts by category.
        """
        with self._lock:
            return {
                'fail': {r.id: r.current_hit_count for r in self._rule_set.fail_rules},
                'warn': {r.id: r.current_hit_count for r in self._rule_set.warn_rules},
                'ignore': {r.id: r.current_hit_count for r in self._rule_set.ignore_rules},
            }

    def get_rule_summary(self) -> Dict[str, int]:
        """Get summary of rule counts."""
        with self._lock:
            return {
                'fail_rules': len(self._rule_set.fail_rules),
                'warn_rules': len(self._rule_set.warn_rules),
                'ignore_rules': len(self._rule_set.ignore_rules),
            }

    def add_rule(self, match_type: MatchType, pattern: str,
                 category: str = 'fail', threshold: int = 1, name: str = ""):
        """
        Add a rule programmatically.

        Args:
            match_type: Type of pattern matching.
            pattern: Pattern string.
            category: Rule category ('fail', 'warn', 'ignore').
            threshold: Hit threshold before triggering.
            name: Human-readable name for the rule.
        """
        rule = Rule(
            type=match_type,
            pattern=pattern,
            threshold=threshold,
            name=name or pattern[:50],
            id=f"dynamic|{match_type.name}|{pattern[:30]}|t={threshold}"
        )

        with self._lock:
            self._add_rule(rule, category)
            self._version += 1

    def __repr__(self) -> str:
        summary = self.get_rule_summary()
        return (f"PatternProcessor(version={self._version}, "
                f"fail={summary['fail_rules']}, "
                f"warn={summary['warn_rules']}, "
                f"ignore={summary['ignore_rules']})")

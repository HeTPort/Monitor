from __future__ import annotations

import unittest


class LegacyImportTests(unittest.TestCase):
    def test_correctly_spelled_compatibility_modules(self) -> None:
        from src.judgment_decision import JudgmentDecision
        from src.log_parser import LogParser
        from src.pattern_processor import PatternProcessor

        self.assertIsNotNone(JudgmentDecision)
        self.assertIsNotNone(LogParser)
        self.assertIsNotNone(PatternProcessor)


if __name__ == "__main__":
    unittest.main()

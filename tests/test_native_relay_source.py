from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "native" / "uart_relay" / "avs_uart_relay.c"


class NativeRelaySourceTests(unittest.TestCase):
    def test_relay_has_portable_transport_primitives_and_no_extra_runtime(self):
        text = SOURCE.read_text(encoding="utf-8")
        for required in (
            "termios.h",
            "tcdrain",
            "write_all",
            "write_nul_guard",
            "DEFAULT_TAIL_GUARD",
            "EINTR",
            "crc32_bytes",
            "cobs_encode",
            "cobs_decode",
        ):
            self.assertIn(required, text)
        for forbidden in ("std::", "#include <iostream>", "pthread_", "system(", "popen("):
            self.assertNotIn(forbidden, text)

    def test_relay_exposes_separate_safe_probes(self):
        text = SOURCE.read_text(encoding="utf-8")
        for option in ("--version", "--self-test", "--check-uart", "--baud", "--max-frame", "--tail-guard"):
            self.assertIn(option, text)

    def test_eof_guard_is_written_before_the_final_drain(self):
        text = SOURCE.read_text(encoding="utf-8")
        guard = text.index("write_nul_guard(fd, tail_guard)")
        final_drain = text.index("tcdrain(fd)", guard)
        self.assertLess(guard, final_drain)


if __name__ == "__main__":
    unittest.main()

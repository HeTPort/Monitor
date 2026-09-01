from __future__ import annotations

import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from src.channel_manager import HDCChannel
from src.transport import CommandResult, HDCTransport


class HDCRemoteStatusTests(unittest.TestCase):
    def test_typed_transport_uses_remote_status_instead_of_host_status(self) -> None:
        transport = HDCTransport(Path("hdc"), serial="DEVICE-1")
        host_result = CommandResult(
            ("hdc", "shell"),
            0,
            "/bin/sh: python3: inaccessible or not found\n__VMIN_REMOTE_RC__=127\n",
            "",
            0.1,
        )
        with patch.object(transport, "_run", return_value=host_result) as run:
            result = transport.invoke(("python3", "--version"))

        self.assertEqual(result.return_code, 127)
        self.assertFalse(result.success)
        self.assertNotIn("__VMIN_REMOTE_RC__", result.stdout)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], [str(transport.tool), "-t", "DEVICE-1", "shell"])
        self.assertIn("python3 --version", command[-1])

    def test_legacy_channel_reports_remote_echo_failure(self) -> None:
        channel = object.__new__(HDCChannel)
        channel._hdc_path = "hdc"
        channel._serial = "DEVICE-1"
        channel._timeout = 30
        completed = types.SimpleNamespace(
            returncode=0,
            stdout="/bin/sh: cannot create /dev/ttyHW0\n__VMIN_REMOTE_RC__=1\n",
            stderr="",
        )
        with patch("src.channel_manager.subprocess.run", return_value=completed) as run:
            code, stdout, stderr = channel.invoke("echo PAIR_1 > /dev/ttyHW0")

        self.assertEqual(code, 1)
        self.assertIn("cannot create", stdout)
        self.assertNotIn("__VMIN_REMOTE_RC__", stdout)
        self.assertEqual(stderr, "")
        command = run.call_args.args[0]
        self.assertIn("echo PAIR_1 > /dev/ttyHW0", command[-1])
        self.assertNotIn("printf", command[-1])

    def test_cancel_active_terminates_the_host_transport_process(self) -> None:
        transport = HDCTransport(Path(sys.executable))
        results = []
        worker = threading.Thread(
            target=lambda: results.append(
                transport._run([sys.executable, "-c", "import time; time.sleep(30)"], 30)
            )
        )
        worker.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with transport._process_lock:
                if transport._active_processes:
                    break
            time.sleep(0.01)
        self.assertEqual(transport.cancel_active(), 1)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()

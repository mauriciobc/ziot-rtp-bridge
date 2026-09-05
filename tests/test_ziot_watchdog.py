"""Watchdog regressions: Docker launch/timeout failures stay critical."""
import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ziot_watchdog as watchdog


class RestartFrigateTests(unittest.TestCase):
    def run_main(self, run_effect):
        bridge = {"status": "ok", "cameras": []}
        go2rtc = {"cat_cam1": {"producers": [], "consumers": []}}
        out = io.StringIO()
        with mock.patch.object(watchdog, "check_bridge", return_value=bridge), \
                mock.patch.object(watchdog, "check_go2rtc", return_value=go2rtc), \
                mock.patch.object(watchdog.subprocess, "run",
                                  side_effect=run_effect), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                watchdog.main()
        return cm.exception.code, out.getvalue()

    def test_docker_missing_binary_is_critical(self):
        code, out = self.run_main(FileNotFoundError(2, "No such file"))
        self.assertEqual(code, 2)
        self.assertIn("RESTARTING Frigate...", out)
        self.assertIn("RESTART FAILED:", out)
        self.assertNotIn("Frigate restarted", out)

    def test_docker_timeout_is_critical(self):
        code, out = self.run_main(
            subprocess.TimeoutExpired(cmd=["docker", "restart"], timeout=30))
        self.assertEqual(code, 2)
        self.assertIn("RESTARTING Frigate...", out)
        self.assertIn("RESTART FAILED:", out)
        self.assertNotIn("Frigate restarted", out)


if __name__ == "__main__":
    unittest.main()

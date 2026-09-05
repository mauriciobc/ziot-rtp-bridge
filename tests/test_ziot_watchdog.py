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


HEALTHY_GO2RTC = {
    "cat_cam1": {"producers": [{"bytes_recv": 999999}], "consumers": []},
}


class BootPhaseTests(unittest.TestCase):
    """A bridge that is up but serving no cameras -- cloud unreachable, token
    rejected -- returns an empty roster, which the camera loop walks in silence.
    The boot phase is the only field that says anything is wrong."""

    def run_main(self, bridge):
        out = io.StringIO()
        with mock.patch.object(watchdog, "check_bridge", return_value=bridge), \
                mock.patch.object(watchdog, "check_go2rtc",
                                  return_value=HEALTHY_GO2RTC), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                watchdog.main()
        return cm.exception.code, out.getvalue()

    def test_an_empty_roster_mid_boot_is_an_issue(self):
        code, out = self.run_main({
            "status": "degraded", "cameras": [],
            "boot": {"phase": "retrying",
                     "detail": "device list unavailable: timed out",
                     "attempts": 4},
        })
        self.assertEqual(code, 1)
        self.assertIn("BRIDGE NOT SERVING CAMERAS (retrying)", out)
        self.assertIn("timed out", out)

    def test_a_rejected_token_is_an_issue(self):
        code, out = self.run_main({
            "status": "degraded", "cameras": [],
            "boot": {"phase": "failed", "detail": "the cloud rejected the token"},
        })
        self.assertEqual(code, 1)
        self.assertIn("rejected the token", out)

    def test_a_ready_bridge_raises_nothing(self):
        code, out = self.run_main({
            "status": "ok", "cameras": [], "boot": {"phase": "ready"},
        })
        self.assertEqual(code, 0)
        self.assertNotIn("BRIDGE NOT SERVING CAMERAS", out)

    def test_an_older_bridge_without_the_field_is_tolerated(self):
        code, out = self.run_main({"status": "ok", "cameras": []})
        self.assertEqual(code, 0)
        self.assertNotIn("BRIDGE NOT SERVING CAMERAS", out)


if __name__ == "__main__":
    unittest.main()

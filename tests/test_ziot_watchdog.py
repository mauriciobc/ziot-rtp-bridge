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
    def setUp(self):
        watchdog._DEAD_STREAK.clear()

    def run_main(self, run_effect):
        """Two ticks: the first dead observation is a blip, the second one
        is what arms the restart that then fails."""
        bridge = {"status": "ok", "cameras": []}
        go2rtc = {"cat_cam1": {"producers": [], "consumers": []}}
        out = io.StringIO()
        with mock.patch.object(watchdog, "check_bridge", return_value=bridge), \
                mock.patch.object(watchdog, "check_go2rtc", return_value=go2rtc), \
                mock.patch.object(watchdog.subprocess, "run",
                                  side_effect=run_effect), \
                contextlib.redirect_stdout(out):
            codes = []
            for _ in range(2):
                with self.assertRaises(SystemExit) as cm:
                    watchdog.main()
                codes.append(cm.exception.code)
        return codes[-1], out.getvalue()

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


class Go2rtcStreamTests(unittest.TestCase):
    def test_uid_from_producer_url(self):
        info = {"producers": [
            {"url": "http://127.0.0.1:8085/cam/141030191094"},
        ]}
        self.assertEqual(
            watchdog.go2rtc_stream_uid("cat_cam_1", info), "141030191094")

    def test_uid_from_stream_name(self):
        self.assertEqual(
            watchdog.go2rtc_stream_uid("140979857781", {"producers": []}),
            "140979857781")

    def test_asleep_camera_is_idle_not_dead(self):
        cams = {"141030191094": {"uid": "141030191094", "streaming": False}}
        info = {"producers": [
            {"url": "http://127.0.0.1:8085/cam/141030191094",
             "bytes_recv": 0},
        ]}
        self.assertEqual(
            watchdog.go2rtc_stream_state("cat_cam_1", info, cams), "idle")

    def test_live_camera_with_no_bytes_is_dead(self):
        cams = {"141030191094": {"uid": "141030191094", "streaming": True}}
        info = {"producers": [
            {"url": "http://127.0.0.1:8085/cam/141030191094",
             "bytes_recv": 0},
        ]}
        self.assertEqual(
            watchdog.go2rtc_stream_state("cat_cam_1", info, cams), "dead")


class IdleCameraDoesNotRestartTests(unittest.TestCase):
    def setUp(self):
        watchdog._DEAD_STREAK.clear()

    def run_main(self, bridge, go2rtc):
        out = io.StringIO()
        with mock.patch.object(watchdog, "check_bridge", return_value=bridge), \
                mock.patch.object(watchdog, "check_go2rtc",
                                  return_value=go2rtc), \
                mock.patch.object(watchdog, "restart_frigate",
                                  return_value=True) as restart, \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                watchdog.main()
        return cm.exception.code, out.getvalue(), restart

    def test_asleep_camera_does_not_restart_frigate(self):
        bridge = {
            "status": "degraded", "boot": {"phase": "ready"},
            "cameras": [{
                "uid": "141030191094", "streaming": False, "fps": 0.0,
                "endpoint_moves": 0, "last_rx_ago_s": None, "mode": "direct",
            }],
        }
        go2rtc = {"cat_cam_1": {"producers": [
            {"url": "http://127.0.0.1:8085/cam/141030191094",
             "bytes_recv": 0},
        ], "consumers": []}}
        code, out, restart = self.run_main(bridge, go2rtc)
        self.assertEqual(code, 1)
        self.assertIn("[IDLE]", out)
        restart.assert_not_called()

    def test_unprefixed_stream_name_is_still_watched(self):
        bridge = {"status": "ok", "cameras": [], "boot": {"phase": "ready"}}
        go2rtc = {"front_yard": {"producers": [], "consumers": []}}
        code, out, restart = self.run_main(bridge, go2rtc)
        self.assertEqual(code, 1)
        self.assertIn("go2rtc/front_yard", out)
        # First dead observation is a blip: no restart on the tick that
        # could be a startup transient.
        restart.assert_not_called()
        self.assertIn("[BLIP]", out)
        code, out, restart = self.run_main(bridge, go2rtc)
        self.assertEqual(code, 1)
        self.assertIn("Go2rtc streams dead but bridge alive", out)
        restart.assert_called_once()


class DeadGraceTests(unittest.TestCase):
    """The first zero-byte observation is a blip, not a death: go2rtc right
    after a Frigate restart has no producers yet, and restarting on the
    first observation self-triggers on the transient the restart caused."""

    def setUp(self):
        watchdog._DEAD_STREAK.clear()

    def run_main(self, bridge, go2rtc):
        out = io.StringIO()
        with mock.patch.object(watchdog, "check_bridge", return_value=bridge), \
                mock.patch.object(watchdog, "check_go2rtc",
                                  return_value=go2rtc), \
                mock.patch.object(watchdog, "restart_frigate",
                                  return_value=True) as restart, \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                watchdog.main()
        return cm.exception.code, out.getvalue(), restart

    def test_two_consecutive_dead_ticks_restart_once(self):
        go2rtc = {"cat_cam_1": {"producers": [], "consumers": []}}
        bridge = {"status": "ok", "cameras": [], "boot": {"phase": "ready"}}
        code, out, restart = self.run_main(bridge, go2rtc)
        self.assertIn("[BLIP]", out)
        restart.assert_not_called()
        code, out, restart = self.run_main(bridge, go2rtc)
        self.assertIn("[DEAD]", out)
        restart.assert_called_once()

    def test_a_recovery_clears_the_streak(self):
        go2rtc = {"cat_cam_1": {"producers": [], "consumers": []}}
        bridge = {"status": "ok", "cameras": [], "boot": {"phase": "ready"}}
        self.run_main(bridge, go2rtc)                 # blip 1
        healthy = {"cat_cam_1": {"producers": [
            {"url": "http://127.0.0.1:8085/cam/141030191094",
             "bytes_recv": 5000}], "consumers": []}}
        code, out, restart = self.run_main(bridge, healthy)
        self.assertIn("[OK]", out)
        restart.assert_not_called()
        # Dead again later starts from scratch, not from the old streak.
        code, out, restart = self.run_main(bridge, go2rtc)
        self.assertIn("[BLIP]", out)
        restart.assert_not_called()


class RestartPolicyTests(unittest.TestCase):
    """When Frigate gets restarted, and when it must not be.

    A restart can only fix a go2rtc that is up but streamless. A go2rtc that
    is itself unreachable, a down bridge, or a relay-mode note cannot be
    fixed by restarting anything.
    """

    def setUp(self):
        watchdog._DEAD_STREAK.clear()

    def run_main(self, bridge, go2rtc):
        out = io.StringIO()
        with mock.patch.object(watchdog, "check_bridge", return_value=bridge), \
                mock.patch.object(watchdog, "check_go2rtc",
                                  return_value=go2rtc), \
                mock.patch.object(watchdog, "restart_frigate",
                                  return_value=True) as restart, \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                watchdog.main()
        return cm.exception.code, out.getvalue(), restart

    def test_an_unreachable_go2rtc_does_not_restart_frigate(self):
        """Restarting Frigate cannot revive a go2rtc that is itself down."""
        code, out, restart = self.run_main(
            {"status": "ok", "cameras": [], "boot": {"phase": "ready"}},
            None)
        self.assertEqual(code, 1)
        self.assertIn("go2rtc unreachable", out)
        restart.assert_not_called()

    def test_a_down_bridge_does_not_restart_frigate(self):
        code, out, restart = self.run_main(
            {"status": "error", "error": "refused", "cameras": []},
            {"cat_cam1": {"producers": [], "consumers": []}})
        self.assertEqual(code, 1)
        restart.assert_not_called()

    def test_a_relay_camera_is_reported_without_restarting(self):
        bridge = {
            "status": "ok", "boot": {"phase": "ready"},
            "cameras": [{"uid": "141030191094", "streaming": True,
                         "fps": 6.0, "endpoint_moves": 0, "mode": "relay"}],
        }
        code, out, restart = self.run_main(bridge, HEALTHY_GO2RTC)
        self.assertEqual(code, 1)
        self.assertIn("streaming via relay", out)
        restart.assert_not_called()


    def test_a_tolerated_blip_is_labeled_not_degraded(self):
        """A camera silent for 10s is tolerated (might recover); it used to
        print DEGRADED and then "ALL HEALTHY" with nothing telling the two
        apart."""
        bridge = {
            "status": "ok", "boot": {"phase": "ready"},
            "cameras": [{"uid": "141030191094", "streaming": False,
                         "fps": 0.0, "endpoint_moves": 0,
                         "last_rx_ago_s": 10, "mode": "direct"}],
        }
        code, out, restart = self.run_main(bridge, {})
        self.assertEqual(code, 0)
        self.assertIn("[BLIP]", out)
        self.assertNotIn("DEGRADED", out)


class MalformedCameraRowTests(unittest.TestCase):
    def setUp(self):
        watchdog._DEAD_STREAK.clear()

    def run_main(self, bridge, go2rtc):
        out = io.StringIO()
        with mock.patch.object(watchdog, "check_bridge", return_value=bridge), \
                mock.patch.object(watchdog, "check_go2rtc",
                                  return_value=go2rtc), \
                mock.patch.object(watchdog, "restart_frigate",
                                  return_value=True) as restart, \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                watchdog.main()
        return cm.exception.code, out.getvalue(), restart

    def test_a_malformed_camera_row_is_skipped_not_a_keyerror(self):
        """Tolerating an older bridge must include skipping rows that are
        not usable, not dying mid-report on cam["uid"]. No usable row and
        no go2rtc stream means no issues, so this exits 0 -- the old code
        raised KeyError instead of returning at all."""
        bridge = {
            "status": "degraded", "boot": {"phase": "ready"},
            "cameras": [{"uid": None}, {}, "garbage"],
        }
        code, out, restart = self.run_main(bridge, {})
        self.assertEqual(code, 0)
        restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()

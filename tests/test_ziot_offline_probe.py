"""Probe regressions: the hello-scan finds RTP responders with no cloud."""
import contextlib
import io
import socket
import struct
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ziot_offline_probe as probe

CAM_UID = "141030191094"
CAM_SSRC = 0x30191094


def rtp(ssrc=CAM_SSRC, pt=26):
    return struct.pack("!BBHII", 0x80, pt, 1, 1000, ssrc) + b"\x00" * 20


class FakeCamera:
    """Answers hellos with RTP, the way a live camera answers a punch."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self.sock.close()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                data, src = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if data in (b"App send hello", b"App send heart for stun"):
                for _ in range(3):
                    try:
                        self.sock.sendto(rtp(), src)
                    except OSError:
                        pass


class ProbeTests(unittest.TestCase):
    def run_probe(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["probe", *argv]), \
                contextlib.redirect_stdout(out):
            rc = probe.main()
        return rc, out.getvalue()

    def test_hello_scan_finds_rtp_responder(self):
        cam = FakeCamera().start()
        self.addCleanup(cam.close)
        rc, out = self.run_probe(
            "127.0.0.1", "--ports", f"{cam.port - 1}-{cam.port + 1}",
            "--uid", CAM_UID, "--rate", "5000", "--listen", "1")
        self.assertEqual(rc, 0)
        self.assertIn(f":{cam.port}", out)
    def test_port_spec_parses_ranges_and_singles(self):
        self.assertEqual(probe.parse_ports("1-3,5"), [1, 2, 3, 5])
    def test_hostname_target_matches_its_answers(self):
        cam = FakeCamera().start()
        self.addCleanup(cam.close)
        rc, out = self.run_probe(
            "localhost", "--ports", f"{cam.port - 1}-{cam.port + 1}",
            "--uid", CAM_UID, "--rate", "5000", "--listen", "1")
        self.assertEqual(rc, 0)
        self.assertIn(f":{cam.port}", out)

    def test_silent_target_reports_nothing(self):
        # Reserve then release: ports nothing listens on.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        rc, out = self.run_probe(
            "127.0.0.1", "--ports", f"{port}-{port + 1}",
            "--rate", "5000", "--listen", "1")
        self.assertEqual(rc, 1)
        self.assertIn("nothing answered", out)

    def test_hello_echo_is_not_a_hit(self):
        """A UDP echo of `App send hello` used to parse as RTP v1 and exit 0."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        stop = threading.Event()

        def echo():
            sock.settimeout(0.2)
            while not stop.is_set():
                try:
                    data, src = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    return
                try:
                    sock.sendto(data, src)
                except OSError:
                    return

        threading.Thread(target=echo, daemon=True).start()
        self.addCleanup(stop.set)
        self.addCleanup(sock.close)
        rc, out = self.run_probe(
            "127.0.0.1", "--ports", str(port),
            "--rate", "5000", "--listen", "1")
        self.assertEqual(rc, 1)
        self.assertNotIn("HIT ", out)


class DescribeTests(unittest.TestCase):
    def test_rtp_hello_echo_and_short_payloads_are_described(self):
        self.assertEqual(probe.describe(rtp()), f"ssrc=0x{CAM_SSRC:08x} pt=26")
        self.assertEqual(probe.describe(rtp(pt=0)), f"ssrc=0x{CAM_SSRC:08x} pt=0")
        self.assertEqual(probe.describe(b"App send hello"),
                         "non-RTP (v1 pt=112 ssrc=0x2068656c)")
        self.assertEqual(probe.describe(b"runt"), "4B non-RTP")


class ParsePortsErrorTests(unittest.TestCase):
    def test_a_bad_port_spec_is_an_argparse_error(self):
        """1024-abc used to raise a raw ValueError traceback."""
        err = io.StringIO()
        with mock.patch.object(
                    sys, "argv", ["probe", "127.0.0.1", "--ports", "1024-abc"]), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as cm:
            probe.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("bad --ports", err.getvalue())
        self.assertIn("32768-61000", err.getvalue())


# A real bluetoothctl transcript shape, from the live TXW817 (2026-10-22):
# discovery events, the `devices` listing, an RSSI both with and without
# the hex echo, and non-camera neighbours that must be ignored.
BLE_TRANSCRIPT = """\
[CHG] Controller 10:3D:1C:CB:26:30 Discovering: yes
[NEW] Device E0:44:40:60:68:82 ZIOTA_140979857781
[CHG] Device E0:44:40:60:68:82 RSSI: 0xffffffb0 (-80)
[NEW] Device A4:E2:87:EE:18:16 POCO X6 5G
[CHG] Device A4:E2:87:EE:18:16 ServiceData.0000fcc0-0000-1000-8000-00805f9b34fb:
  03 01 01 c0 db ff 8d 90 9e 06 1b 2f 80 17 0c 03
[NEW] Device FC:58:FA:B7:A3:05 PBS40BT
[CHG] Device AA:BB:CC:DD:EE:FF Name: ZIOTA_141030191094
[CHG] Device AA:BB:CC:DD:EE:FF RSSI: -54
[CHG] Device 98:B6:E9:88:A8:7C RSSI: 0xffffffc1 (-63)
Device E0:44:40:60:68:82 ZIOTA_140979857781
Device FC:58:FA:B7:A3:05 PBS40BT
Device 98:B6:E9:88:A8:7C Xbox Wireless Controller
"""


class BleDiscoveryTests(unittest.TestCase):
    def test_ziota_cameras_are_pulled_from_the_transcript(self):
        cams = probe.parse_ble_devices(BLE_TRANSCRIPT)
        self.assertEqual(
            cams,
            {"E0:44:40:60:68:82": {"uid": "140979857781", "rssi": -80,
                                   "ble_mac": "E0:44:40:60:68:82"},
             "AA:BB:CC:DD:EE:FF": {"uid": "141030191094", "rssi": -54,
                                   "ble_mac": "AA:BB:CC:DD:EE:FF"}})

    def test_wifi_mac_is_the_reversed_ble_address(self):
        # Live-verified 2026-10-22: ZIOTA_140979857781 advertises
        # E0:44:40:60:68:82 and joined the LAN as 82:68:60:40:44:e0.
        self.assertEqual(probe.ble_to_wifi_mac("E0:44:40:60:68:82"),
                         "82:68:60:40:44:e0")

    def test_lan_ip_resolves_from_neighbor_table(self):
        cams = probe.parse_ble_devices(BLE_TRANSCRIPT)
        neigh = ("192.168.68.107 dev wlan0 lladdr 82:68:60:40:44:e0 REACHABLE\n"
                 "192.168.68.1 dev wlan0 lladdr 40:ed:00:84:24:e0 REACHABLE\n")
        probe.ble_lan_ips(cams, neigh)
        self.assertEqual(cams["E0:44:40:60:68:82"]["ip"], "192.168.68.107")
        self.assertNotIn("ip", cams["AA:BB:CC:DD:EE:FF"])

    def test_a_transcript_without_cameras_yields_nothing(self):
        self.assertEqual(probe.parse_ble_devices(
            "[NEW] Device A4:E2:87:EE:18:16 POCO X6 5G\n"
            "Device FC:58:FA:B7:A3:05 PBS40BT\n"), {})

    def test_ble_only_run_reports_cameras_and_exits_zero(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["probe", "--ble", "--ble-scan", "1"]), \
                mock.patch.object(probe, "ble_scan", return_value=BLE_TRANSCRIPT), \
                contextlib.redirect_stdout(out):
            rc = probe.main()
        self.assertEqual(rc, 0)
        self.assertIn("ZIOTA_140979857781  at E0:44:40:60:68:82", out.getvalue())
        self.assertIn("RSSI -80", out.getvalue())

    def test_silent_ble_scan_with_no_target_exits_one(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["probe", "--ble", "--ble-scan", "1"]), \
                mock.patch.object(probe, "ble_scan", return_value=""), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = probe.main()
        self.assertEqual(rc, 1)
        self.assertIn("silent scan does not mean the camera is off",
                      out.getvalue())

    def test_a_missing_adapter_is_reported_not_raised(self):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["probe", "--ble", "--ble-scan", "1"]), \
                mock.patch.object(probe, "ble_scan", side_effect=RuntimeError(
                    "bluetoothctl reports no default controller")), \
                contextlib.redirect_stderr(err):
            rc = probe.main()
        self.assertEqual(rc, 1)
        self.assertIn("no default controller", err.getvalue())


if __name__ == "__main__":
    unittest.main()

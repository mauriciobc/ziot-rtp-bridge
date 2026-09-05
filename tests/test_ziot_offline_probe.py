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


if __name__ == "__main__":
    unittest.main()

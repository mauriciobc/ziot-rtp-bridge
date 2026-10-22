"""Fake-cloud regressions: the listener farm logs what the camera sends."""
import contextlib
import io
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ziot_fake_cloud as fake


class FakeCloudTests(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self._ctx = contextlib.redirect_stdout(self.out)
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)

    def start(self, kind, port=0):
        srv = fake.serve(kind, ("127.0.0.1", port))
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return srv.server_address[1]

    def test_stream_pdu_is_logged_with_ascii_and_hex(self):
        port = self.start("sig")
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(b"REG uid=140979857781 key=secret\x01\x02")
        deadline = time.time() + 3
        while "REG uid" not in self.out.getvalue() and time.time() < deadline:
            time.sleep(0.05)
        s.close()
        out = self.out.getvalue()
        self.assertIn("sig", out)
        self.assertIn("REG uid=140979857781 key=secret", out)
        self.assertIn("[", out)  # hex column

    def test_stream_survives_idle_and_multiple_pdus(self):
        port = self.start("http")
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(b"GET /api/v1/ipc HTTP/1.1")
        s.sendall(b"Host: ipc.gps555.net\r\n\r\n")
        deadline = time.time() + 3
        while "GET /api" not in self.out.getvalue() and time.time() < deadline:
            time.sleep(0.05)
        s.close()
        # one PDU logged (the 2s-idle batching is expected behavior)
        self.assertIn("GET /api/v1/ipc", self.out.getvalue())

    def test_udp_datagram_is_logged_as_stun(self):
        port = self.start("stun")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b"\x00\x01\x00\x00probe", ("127.0.0.1", port))
        deadline = time.time() + 3
        while "probe" not in self.out.getvalue() and time.time() < deadline:
            time.sleep(0.05)
        s.close()
        self.assertIn("stun", self.out.getvalue())
        self.assertIn("probe", self.out.getvalue())

    def test_unbindable_low_port_reports_and_continues(self):
        # Port 80 needs privileges; without them main must report the
        # failure on stderr and still serve the bindable ports.
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "argv",
                                  ["fake", "--http", "80", "--sig", "18882",
                                   "--stun", "0", "--ws", "0"]), \
                mock.patch.object(fake, "_wait_forever",
                           side_effect=KeyboardInterrupt):
            rc = fake.main()
        self.assertEqual(rc, 0)
        self.assertIn("cannot bind http port 80", err.getvalue())
        self.assertIn("listening sig", self.out.getvalue())

    def test_zero_ports_exits_nonzero(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "argv",
                                  ["fake", "--http", "0", "--sig", "0",
                                   "--stun", "0", "--ws", "0"]):
            rc = fake.main()
        self.assertEqual(rc, 1)
        self.assertIn("nothing to listen on", err.getvalue())


class DnsTests(unittest.TestCase):
    def query(self, name):
        parts = b"".join(bytes([len(l)]) + l.encode() for l in name.split("."))
        return b"\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + \
            parts + b"\x00\x00\x01\x00\x01"

    def test_query_name_is_parsed(self):
        self.assertEqual(fake._dns_name(self.query("ipc.gps555.net")),
                         "ipc.gps555.net")
        self.assertIsNone(fake._dns_name(b"short"))

    def test_vendor_name_is_answered_locally(self):
        srv = fake.DnsServer(("127.0.0.1", 0), "192.168.68.114", "127.0.0.1")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.sendto(self.query("sig.gps555.net"), srv.server_address)
        reply, _ = s.recvfrom(512)
        self.assertEqual(reply[2:4], b"\x81\x80")          # QR+RD+RA, no error
        self.assertEqual(reply[-4:], socket.inet_aton("192.168.68.114"))
        s.close()

    def test_foreign_name_is_forwarded_upstream(self):
        # The upstream is a local UDP socket that echoes the transaction id;
        # the test asserts relay, not real DNS.
        up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        up.bind(("127.0.0.1", 0))
        received = []
        def echo():
            for _ in range(2):
                data, addr = up.recvfrom(65535)
                received.append(data)
                up.sendto(b"\xab\xcd" + b"resp", addr)
        threading.Thread(target=echo, daemon=True).start()
        self.addCleanup(up.close)
        srv = fake.DnsServer(("127.0.0.1", 0), "10.0.0.1", "127.0.0.1")
        srv.upstream = up.getsockname()   # ("127.0.0.1", port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.sendto(self.query("example.org"), srv.server_address)
        reply, _ = s.recvfrom(512)
        self.assertEqual(reply[:4], b"\xab\xcdresp"[:4])
        self.assertIn(b"\x07example\x03org\x00", received[0])  # relayed untouched
        s.close()


class ProvisionFrameTests(unittest.TestCase):
    def test_wifi_connect_frame_matches_vendor_app_example(self):
        # Worked example recovered from the app's own construction rules
        # (BlutterMine): ssid "MyWiFi", pwd "pass1234",
        # sig "sig.gps555.net:8882", tz +120 min, seq 1.
        frame = fake.wifi_connect_frame("MyWiFi", "pass1234",
                                        "sig.gps555.net:8882", 120)
        self.assertEqual(frame.hex(),
                         "8686" "002d" "0001" "0028"
                         "0001"
                         "06" "4d7957694669"
                         "08" "7061737331323334"
                         "13" "7369672e6770733535352e6e65743a38383832"
                         "0078"
                         "33")

    def test_negative_timezone_is_biased(self):
        frame = fake.wifi_connect_frame("s", "p", "h:1", -60)
        dlen = (frame[6] << 8) | frame[7]
        tz = frame[8 + dlen - 2:8 + dlen]
        self.assertEqual(tz, b"\x7f\xc4")   # 0x8000 + (-60) = 0x7fc4
        frame = fake.wifi_connect_frame("s", "p", "h:1", 60)
        tz = frame[8 + (frame[6] << 8 | frame[7]) - 2:
                   8 + (frame[6] << 8 | frame[7])]
        self.assertEqual(tz, b"\x00\x3c")   # plain 60

    def test_reg_code_appends_four_bytes(self):
        with_reg = fake.wifi_connect_frame("s", "p", "h:1", 0, reg_code=0x11223344)
        without = fake.wifi_connect_frame("s", "p", "h:1", 0)
        self.assertEqual(len(with_reg) - len(without), 4)
        self.assertIn(b"\x11\x22\x33\x44", with_reg)

    def test_chunks_are_20_bytes(self):
        frame = fake.wifi_connect_frame("a" * 200, "p", "h:1", 0)
        chunks = fake.chunk20(frame)
        self.assertEqual(b"".join(chunks), frame)
        self.assertTrue(all(len(c) <= 20 for c in chunks))
        self.assertEqual(len(chunks), (len(frame) + 19) // 20)

    def test_success_response_parses_uid(self):
        # Example frame for uid 0123456789AB from the app's parser rules.
        resp = bytes.fromhex(
            "8686" "0015" "0001" "0010"
            "8001" "00" "0c" "303132333435363738394142"
            "8b" "0d")
        self.assertEqual(fake.parse_provision_response(resp),
                         {"result": True, "uid": "0123456789AB"})

    def test_bad_checksum_and_end_symbol_are_rejected(self):
        good = bytes.fromhex(
            "8686" "0015" "0001" "0010"
            "8001" "00" "0c" "303132333435363738394142"
            "8b" "0d")
        with self.assertRaisesRegex(ValueError, "checksum"):
            fake.parse_provision_response(good[:-2] + b"\x00" + good[-1:])
        with self.assertRaisesRegex(ValueError, "end symbol"):
            fake.parse_provision_response(good[:-1] + b"\x00")
        with self.assertRaisesRegex(ValueError, "header"):
            fake.parse_provision_response(b"\x00" + good[1:])


if __name__ == "__main__":
    unittest.main()

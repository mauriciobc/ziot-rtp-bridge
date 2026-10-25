"""Fake-cloud regressions: the listener farm logs what the camera sends."""
import contextlib
import io
import json
import socket
import struct
import sys
import tempfile
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
        # The hex column is complete and labelled, so a frame's bytes can
        # be re-read from the log exactly as they went over the wire.
        self.assertIn("hex=524547207569643d313430393739383537373831", out)
        self.assertIn("0102", out)

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
                                   "--stun", "0", "--ws", "0", "--dns", "0"]):
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

    def test_answer_echoes_the_full_question_section(self):
        """The question is name + QTYPE + QCLASS. Dropping the last four
        bytes makes a resolver read the answer's compression pointer as the
        query type and reject the reply as malformed."""
        query = self.query("ipc.gps555.net")
        reply = fake._dns_answer(query, "192.168.68.114")
        # The question section is echoed verbatim, so QTYPE is still 1 (A).
        qtype_at = len(query) - 4
        self.assertEqual(reply[qtype_at:qtype_at + 2], b"\x00\x01")
        self.assertEqual(reply[qtype_at + 2:qtype_at + 4], b"\x00\x01")
        # ... and the answer record follows the question, not inside it.
        self.assertEqual(reply[-4:], socket.inet_aton("192.168.68.114"))
        self.assertEqual(reply[-14:-12], b"\x00\x01")   # type A
        self.assertEqual(reply[-12:-10], b"\x00\x01")   # class IN

    def test_answer_count_matches_the_records_sent(self):
        """ANCOUNT is 1 when an A record follows, 0 when it does not. A
        mismatch makes the resolver report the record as trailing bytes."""
        query = self.query("ipc.gps555.net")
        reply = fake._dns_answer(query, "192.168.68.114")
        self.assertEqual(reply[6:8], b"\x00\x01")        # ANCOUNT = 1
        self.assertEqual(len(reply) - len(query), 16)    # one A record

        aaaa = bytearray(query)
        aaaa[-4:-2] = b"\x00\x1c"
        reply = fake._dns_answer(bytes(aaaa), "192.168.68.114")
        self.assertEqual(reply[6:8], b"\x00\x00")        # ANCOUNT = 0
        self.assertEqual(len(reply), len(aaaa))          # nothing extra

    def test_non_a_query_gets_no_records(self):
        """An AAAA query must not get an A record back."""
        query = bytearray(self.query("ipc.gps555.net"))
        query[-4:-2] = b"\x00\x1c"          # QTYPE = AAAA
        reply = fake._dns_answer(bytes(query), "192.168.68.114")
        # No answer record: the reply ends with the echoed question, and
        # the answer count is zero.
        self.assertEqual(reply[6:8], b"\x00\x00")   # ANCOUNT = 0
        self.assertNotIn(socket.inet_aton("192.168.68.114"), reply)

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
        # The trailing 0d end symbol was added after the camera answered a
        # frame without it -- it stays silent on an incomplete frame
        # (DECOMPILATION_REPORT.md §14.1).
        frame = fake.wifi_connect_frame("MyWiFi", "pass1234",
                                        "sig.gps555.net:8882", 120)
        self.assertEqual(frame.hex(),
                         "8686" "002d" "0001" "0028"
                         "0001"
                         "06" "4d7957694669"
                         "08" "7061737331323334"
                         "13" "7369672e6770733535352e6e65743a38383832"
                         "0078"
                         "33" "0d")

    def test_frame_ends_with_end_symbol_not_counted_by_length(self):
        # The camera counts LEN over seq+dlen+payload+xor only: the end
        # symbol is not part of it, and omitting the symbol draws silence.
        frame = fake.wifi_connect_frame("s", "p", "h:1", 0)
        self.assertTrue(frame.endswith(b"\x0d"))
        dlen = (frame[6] << 8) | frame[7]
        length = (frame[2] << 8) | frame[3]
        self.assertEqual(length, 5 + dlen)
        self.assertEqual(len(frame), 4 + length + 1)

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

    def test_string_to_uint32_is_the_apps_nonce(self):
        # DeviceAddData.stringToUint32: sha256(text), digest bytes 0/2/4/6
        # packed big-endian (device_add_controller.dart 0x6e7d5c).
        import hashlib
        text = "0123456789ABCDEF1730000000000000"
        digest = hashlib.sha256(text.encode()).digest()
        expected = (digest[0] << 24) | (digest[2] << 16) | (digest[4] << 8) | digest[6]
        self.assertEqual(fake.string_to_uint32(text), expected)
        # it is a uint32, and it tracks its input
        self.assertTrue(0 <= fake.string_to_uint32(text) <= 0xFFFFFFFF)
        self.assertNotEqual(fake.string_to_uint32(text + "x"),
                            fake.string_to_uint32(text))

    def test_reg_code_lands_after_the_timezone(self):
        # The app's order is tz then regCode; a frame that put the nonce
        # anywhere else would still be 4 bytes longer and pass the test
        # above, so pin the position too.
        frame = fake.wifi_connect_frame("s", "p", "h:1", -60,
                                        reg_code=0xAABBCCDD)
        dlen = (frame[6] << 8) | frame[7]
        self.assertEqual(frame[8 + dlen - 6:8 + dlen],
                         b"\x7f\xc4\xaa\xbb\xcc\xdd")

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


class SignallingFrameTests(unittest.TestCase):
    """The camera's registration frame, as captured on the wire.

    Captured live from two cameras (uid 140979857781 and 141030191094)
    on 2026-10-23, against the broker's signalling port.
    """

    # 86 86 | cmd=0001 | len=002c | seq=0019 | uidLen=000c |
    # "140979857781" | "TXW817_A_V1.0.12.32" + NUL pad | xor | 0d
    CAPTURED = (b"\x86\x86\x00\x01\x00\x2c\x00\x19\x00\x0c"
                b"140979857781"
                b"TXW817_A_V1.0.12.32" + b"\x00" * 13
                + b"\x61\x0d")

    def test_the_captured_constant_is_the_captured_frame(self):
        # Guards the constant above against a hand-edited NUL count: the
        # frame as logged by the broker on 2026-10-23 was 56 bytes with
        # the body padded to 32.
        self.assertEqual(len(self.CAPTURED), 56)
        self.assertEqual(self.CAPTURED[22:41], b"TXW817_A_V1.0.12.32")
        self.assertEqual(self.CAPTURED[41:54], b"\x00" * 13)
        self.assertEqual(self.CAPTURED[54:], b"\x61\x0d")

    def test_decodes_the_captured_frame(self):
        d = fake.decode_signalling(self.CAPTURED)
        self.assertEqual(d["cmd"], "0x0001")
        self.assertEqual(d["len"], 44)      # uid(12) + body(32)
        self.assertEqual(d["seq"], 0x19)
        self.assertEqual(d["uid_len"], 12)
        self.assertEqual(d["uid"], "140979857781")
        self.assertTrue(d["body"].startswith("TXW817_A_V1.0.12.32"))
        self.assertTrue(d["xor_ok"])

    def test_rebuilds_the_captured_frame_byte_for_byte(self):
        built = fake.signalling_frame(0x0001, 0x19, b"140979857781",
                                      b"TXW817_A_V1.0.12.32")
        self.assertEqual(built, self.CAPTURED)

    def test_field_order_is_magic_len_seq(self):
        # The length (constant) and the sequence (increments on every
        # retransmission) were the two fields in question; the length is
        # the one that counts the uid plus the body.
        cmd, length, seq, uid_len = 0x0001, 44, 7, 12
        frame = fake.signalling_frame(cmd, seq, b"u" * uid_len, b"x" * 32)
        self.assertEqual(frame[2:4], b"\x00\x01")      # cmd
        self.assertEqual(frame[4:6], (44).to_bytes(2, "big"))  # len
        self.assertEqual(frame[6:8], (7).to_bytes(2, "big"))   # seq
        self.assertEqual(frame[8:10], (12).to_bytes(2, "big"))  # uidLen

    def test_checksum_covers_everything_before_it(self):
        frame = fake.signalling_frame(1, 3, b"140979857781", b"hello")
        self.assertEqual(frame[-1], 0x0D)
        self.assertEqual(fake._xor(frame[:-2]), frame[-2])

    def test_padded_and_unpadded_forms(self):
        padded = fake.signalling_frame(1, 1, b"u" * 12, b"hi")
        self.assertEqual(len(padded), 56)      # 10 + 12 + 32 + 2
        unpadded = fake.signalling_frame(1, 1, b"u" * 12, b"hi", pad=False)
        self.assertEqual(len(unpadded), 26)    # 10 + 12 + 2 + 2

    def test_the_p2p_request_carries_the_pairs_at_20_40_42_62(self):
        # SIGNALLING_PROTOCOL.md §13.1: 20 bytes the handler never
        # reads, then pair A (IP at 20, port at 40) and pair B (IP at 42,
        # port at 62). The camera picks B only if A equals its own
        # STUN-learned public IP and B shares its local /24 -- so the
        # offsets are load-bearing, not decoration.
        frame = fake.vendor_p2p_conn_req("140979857781", 7,
                                         "192.168.68.114", 60587,
                                         "192.168.68.114", 60587)
        body = frame[22:-2]
        self.assertEqual(len(body), 64)
        self.assertEqual(body[20:40].rstrip(b"\x00"),
                         b"192.168.68.114")
        self.assertEqual(struct.unpack("!H", body[40:42])[0], 60587)
        self.assertEqual(body[42:62].rstrip(b"\x00"),
                         b"192.168.68.114")
        self.assertEqual(struct.unpack("!H", body[62:64])[0], 60587)

    def test_the_p2p_request_carries_the_registration_number(self):
        # The 20 unread bytes carry the number the camera echoes back in
        # its 0x0003 (§14.1) -- how the cloud knows the camera has
        # answered *this* registration.
        frame = fake.vendor_p2p_conn_req("140979857781", 7,
                                         "192.168.68.114", 60587,
                                         "192.168.68.114", 60587,
                                         seq_no=186)
        body = frame[22:-2]
        self.assertEqual(struct.unpack("!H", body[0:2])[0], 186)
        self.assertEqual(body[2:20], b"\x00" * 18)

    def test_the_play_control_carries_value_one_at_offset_20(self):
        # §13.3: the handler reads a u16 BE at body offset 20; 1 is
        # PlayLiveview, 2 PlayPause, 3 stop.
        for value in (1, 2, 3):
            frame = fake.vendor_play_ctrl("140979857781", 9, value)
            body = frame[22:-2]
            self.assertEqual(len(body), 22)
            self.assertEqual(struct.unpack("!H", body[20:22])[0], value)

    def test_the_ack_echoes_the_frames_own_sequence_number(self):
        # §4: the ack body is `cSeq/cmd/ack`, and the cSeq is the
        # camera's own sequence number. The 2 h vendor wiretap holds
        # 535 acks of `00 03 00 02 00` and 535 of `00 04 00 04 00` --
        # the camera's counter usually sat at 3 and 4 -- and a tail of
        # `00 04 00 02 00`, `00 05 00 04 00`, `00 08 00 02 00`, so the
        # vendor tracks it. An ack of the wrong number is an ack of
        # nothing, and the camera answers it by re-asking.
        pull = fake.vendor_pullvideo_answer("140979857781", 3)
        self.assertEqual(pull[22:-2], b"\x00\x03\x00\x02\x00")
        pull = fake.vendor_pullvideo_answer("140979857781", 17)
        self.assertEqual(pull[22:-2], b"\x00\x11\x00\x02\x00")
        status = fake.vendor_status_answer("140979857781", 4, 4)
        self.assertEqual(status[22:-2], b"\x00\x04\x00\x04\x00")
        status = fake.vendor_status_answer("140979857781", 19, 4)
        self.assertEqual(status[22:-2], b"\x00\x13\x00\x04\x00")

    def test_a_long_body_keeps_its_own_length(self):
        body = b"seq:1, stun port:5000, stun ip:192.168.1.2"
        frame = fake.signalling_frame(1, 1, b"u" * 12, body, pad=False)
        self.assertEqual(frame[22:-2], body)
        self.assertEqual(int.from_bytes(frame[4:6], "big"), 12 + len(body))

    def test_the_punch_instruction_carries_the_viewer_address(self):
        # The address the camera must send its media to is packed as
        # binary fields -- the firmware's own "seq:%d, stun port:%d
        # stun ip:%s" is a log template for parsed values, not the wire
        # bytes, and the camera ignores text answers.
        viewer = {"privateIp": "192.168.68.114", "privatePort": 48666}
        cands = fake._ack_candidates(b"140979857781", 5, 44, viewer)
        bodies = [frame[22:-2] for _, frame in cands]
        packed = struct.pack("!H", 48666) + socket.inet_aton("192.168.68.114")
        self.assertTrue(any(packed in b for b in bodies),
                        "the viewer's address must reach the camera")

    def test_rebuilds_the_vendors_own_registration_answer(self):
        # The vendor's real answer, captured live from
        # sig.gps555.net on 2026-10-23 (118 bytes). The body is flat
        # fixed-width fields: a u16 lead, the relay address in a
        # 20-byte slot with two ports, the STUN host in a 32-byte slot
        # with its port, and the signalling address in a 32-byte slot
        # with its port.
        captured = bytes.fromhex(
            "86868000006a0001000c3134303937393835373738310001"
            "3135362e3234362e31362e363100000000000000022a2710"
            "7374756e2e6770733535352e6e657400000000000000000000000000000000"
            "0034a634372e39372e37312e38320000000000000000000000000000000000"
            "0000000022b5cb0d")
        built = fake.vendor_registration_answer(
            "140979857781", 1, "156.246.16.61", 554, 10000,
            "stun.gps555.net", 13478, "47.97.71.82", 0x22b5)
        self.assertEqual(built, captured)
        self.assertEqual(len(built), 118)

    def test_rebuilds_the_vendors_answer_to_the_clock_request(self):
        # The camera's cmd 0x0021 (a one-byte counter) asks the server
        # for its clock; the vendor answers with the same command plus
        # the response bit and a big-endian u32 timestamp.
        captured = bytes.fromhex(
            "8686802100100002000c313430393739383537373831" "6adbd02a" "f2" "0d")
        built = fake.vendor_time_answer("140979857781", 2, 1792790570)
        self.assertEqual(built, captured)

    def test_rebuilds_the_vendors_answer_to_pullvideo(self):
        # The camera's cmd 0x0002 is pullVideo -- the same command the
        # app sends over the LAN. The vendor answers with the response
        # bit set and a five-byte body, which is the camera's "go ahead".
        #
        # Note the two numbers: the frame's header sequence is 2 (the
        # frame being answered) and the body's cSeq is 3 -- the camera's
        # *latest* counter, which had moved on by the time the answer
        # went out (§4).
        captured = bytes.fromhex(
            "8686800100110002000c313430393739383537373831" "0003000200" "99" "0d")
        built = fake.vendor_pullvideo_answer("140979857781", 2, cseq=3)
        self.assertEqual(built, captured)


class CaptureComplianceTests(unittest.TestCase):
    """The broker's answers must match the vendor's, field for field.

    The app capture (PCAPdroid_29_ago._15_13_27.pcap) holds a live
    session with the vendor: every request the app sent to
    ipc.gps555.net:80 and the vendor's answer to each. The shapes and
    the values are compared here, so the broker cannot drift from the
    contract the app and the camera depend on.
    """

    CAPTURE = Path("/home/mauriciobc/Downloads/PCAPdroid_29_ago._15_13_27.pcap")
    VENDOR = "120.26.121.165"

    @classmethod
    def setUpClass(cls):
        if not cls.CAPTURE.exists():
            raise unittest.SkipTest(f"capture not present: {cls.CAPTURE}")
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "chk", Path(__file__).resolve().parents[1]
            / "check_api_against_capture.py")
        cls.chk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.chk)
        pkts = cls.chk.pcap_streams(str(cls.CAPTURE))
        cls.pairs = cls.chk.tcp_messages(pkts)

    def _messages(self, stream):
        buf = stream
        out = []
        while True:
            head_end = buf.find(b"\r\n\r\n")
            if head_end < 0:
                break
            head = buf[:head_end].decode("latin-1", "replace")
            length = 0
            for line in head.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
            total = head_end + 4 + length
            if total > len(buf):
                break
            out.append((head, buf[head_end + 4:total]))
            buf = buf[total:]
        return out

    def _captured(self, path):
        """The vendor's answer to the first request of that path."""
        for req, rsp in self.pairs:
            for (head, _), (rhead, body) in zip(self._messages(req),
                                                self._messages(rsp)):
                parts = head.split("\r\n")[0].split()
                if len(parts) < 2 or parts[1].split("?")[0] != path:
                    continue
                return rhead, body
        return None, None

    def test_the_capture_holds_the_vendor_session(self):
        self.assertTrue(self.pairs, "no vendor HTTP traffic in the capture")

    def test_the_device_list_shape_matches(self):
        rhead, body = self._captured("/api/v1/ipc")
        self.assertIsNotNone(body, "the capture has no device list")
        vendor = json.loads(body)["data"]
        api = fake.HttpApi()
        api.add_camera("141030191094", "192.168.18.75", 53693)
        answer = api.respond("GET", "/api/v1/ipc",
                             "terminalFamilyId=3132031", b"")
        broker = json.loads(answer.split(b"\r\n\r\n", 1)[1])["data"]
        self.assertEqual(sorted(vendor.keys()), sorted(broker.keys()))
        self.assertEqual(vendor["count"], broker["count"])
        self.assertEqual(vendor["pageSize"], broker["pageSize"])

    def test_the_device_record_has_the_vendors_fields(self):
        rhead, body = self._captured("/api/v1/ipc/141030191094")
        self.assertIsNotNone(body, "the capture has no device record")
        vendor = json.loads(body)["data"]
        api = fake.HttpApi()
        api.add_camera("141030191094", "192.168.18.75", 53693)
        broker = api._camera_row("141030191094")
        missing = set(vendor) - set(broker)
        self.assertEqual(missing, set(),
                         f"the broker's record lacks {missing}")
        extra = set(broker) - set(vendor)
        self.assertEqual(extra, set(),
                         f"the broker's record has extra {extra}")
        # The values that are the camera's own state will differ; the
        # contract is the field set and the types.
        for key in vendor:
            if key in ("startTime", "createdAt", "updatedAt", "commTime",
                       "online_time", "remoteServiceEndTime", "power",
                       "signal", "wifiSsid", "relay_ip", "server_ip"):
                continue
            self.assertEqual(type(vendor[key]), type(broker[key]),
                             f"{key}: {vendor[key]!r} vs {broker[key]!r}")

    def test_the_stun_answer_carries_the_camera_address(self):
        rhead, body = self._captured("/api/v1/ipc/stun-addr/141030191094")
        self.assertIsNotNone(body, "the capture has no stun-addr answer")
        vendor = json.loads(body)["data"]
        api = fake.HttpApi()
        api.add_camera("141030191094", "192.168.18.75", 53693)
        answer = api.respond("GET", "/api/v1/ipc/stun-addr/141030191094",
                             "", b"")
        broker = json.loads(answer.split(b"\r\n\r\n", 1)[1])["data"]
        self.assertEqual(sorted(vendor.keys()), sorted(broker.keys()))
        self.assertEqual(vendor["IpcPrivateIP"], broker["IpcPrivateIP"])
        self.assertEqual(vendor["IpcPrivatePort"], broker["IpcPrivatePort"])

    def test_the_trailing_slash_redirect_matches(self):
        rhead, body = self._captured("/api/v1/ipc/")
        self.assertIsNotNone(rhead, "the capture has no redirect")
        self.assertIn("301", rhead.split("\r\n")[0])
        location = [l.split(":", 1)[1].strip()
                    for l in rhead.split("\r\n")
                    if l.lower().startswith("location:")]
        self.assertEqual(location, ["/api/v1/ipc?terminalFamilyId=3132031"])


class KeepaliveTests(unittest.TestCase):
    """The camera's session needs a message every couple of seconds.

    The vendor keeps it alive with the pullVideo go-ahead; the broker
    does the same, on its own timer: the camera goes quiet when it has
    nothing to say, and a session that is not fed dies in about twelve
    seconds.
    """

    def test_the_worker_feeds_a_silent_camera(self):
        api = fake.HttpApi()
        sent = []
        api.camera_links["cam"] = _FakeSocket(sent)
        api.viewers["cam"] = {"privateIp": "192.168.68.114",
                              "privatePort": 5000}
        api.last_seq["cam"] = 44
        api.start_keepalive_worker(interval=0.05)
        self.addCleanup(api.stop_keepalive_worker)
        deadline = time.time() + 3
        while not sent and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(sent, "the worker must feed a camera that is quiet")
        api.stop_keepalive_worker()

    def test_the_worker_stops(self):
        api = fake.HttpApi()
        thread = api.start_keepalive_worker(interval=0.05)
        api.stop_keepalive_worker()
        self.assertFalse(thread.is_alive())

    def test_the_worker_does_not_feed_an_unknown_camera(self):
        api = fake.HttpApi()
        sent = []
        api.camera_links["cam"] = _FakeSocket(sent)
        api.start_keepalive_worker(interval=0.05)
        self.addCleanup(api.stop_keepalive_worker)
        time.sleep(0.2)
        api.camera_links.clear()
        before = len(sent)
        time.sleep(0.2)
        self.assertEqual(len(sent) - before, 0)

    def test_the_first_keepalive_waits_a_full_interval(self):
        api = fake.HttpApi()
        # A camera that has just registered gets the full interval: the
        # registration answer is the first message of the session.
        self.assertFalse(api.keepalive_due("cam", now=1000.0))
        self.assertFalse(api.keepalive_due("cam", now=1000.5))
        self.assertTrue(api.keepalive_due("cam", now=1000.0 +
                                          fake.KEEPALIVE_INTERVAL + 0.1))

    def test_the_keepalive_is_periodic_not_per_frame(self):
        api = fake.HttpApi()
        api.keepalive_due("cam", now=1000.0)          # arms the timer
        sent = []
        # Two seconds of frames, 0.1s apart, crossing the interval.
        for i in range(21):
            now = 1000.0 + i * 0.1
            if api.keepalive_due("cam", now=now):
                sent.append(now)
        self.assertEqual(len(sent), 1)
        self.assertGreaterEqual(sent[0], 1000.0 + fake.KEEPALIVE_INTERVAL)

    def test_the_keepalive_echoes_the_last_sequence_number(self):
        api = fake.HttpApi()
        api.last_seq["cam"] = 44
        sent = []
        api.camera_links["cam"] = _FakeSocket(sent)
        api.viewers["cam"] = {"privateIp": "192.168.68.114",
                              "privatePort": 5000}
        api.keepalive("cam")
        self.assertTrue(sent)
        for frame in sent:
            self.assertEqual(int.from_bytes(frame[6:8], "big"), 44)

    def test_concurrent_sends_do_not_interleave(self):
        """Two writers on one socket must not interleave their frames."""
        api = fake.HttpApi()
        sink = _FakeSocket([])
        api.camera_links["cam"] = sink
        errors = []

        def writer(tag):
            try:
                for _ in range(50):
                    api.send_to("cam", tag * 64)
            except Exception as e:       # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(bytes([n]),))
                   for n in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # Every frame on the wire is one writer's, whole.
        for frame in sink.sink:
            self.assertEqual(len(frame), 64)
            self.assertEqual(len(set(frame)), 1)

    def test_a_send_without_a_connection_fails_cleanly(self):
        api = fake.HttpApi()
        self.assertFalse(api.send_to("nobody", b"hello"))




class SplitSignallingTests(unittest.TestCase):
    """The sig socket is a TCP stream: frames arrive coalesced, split
    across reads, and with garbage around them.

    Decoding a whole chunk as one frame is what left camera .108 silent
    on 2026-10-24 19:40:11: it flushed fifteen queued 0x0002 pings and a
    0x0004 as one 389-byte read, the single-frame decode failed the XOR
    check, one bogus ack went out, and the camera never came back. Every
    complete frame must be cut out and answered on its own.
    """

    def frame(self, seq=1):
        return fake.signalling_frame(0x0002, seq, b"140979857781", b"",
                                     pad=False)

    def test_cuts_back_to_back_frames(self):
        frames, tail = fake.split_signalling(
            self.frame(1) + self.frame(2) + self.frame(3))
        self.assertEqual(
            [fake.decode_signalling(f)["seq"] for f in frames], [1, 2, 3])
        self.assertEqual(tail, b"")

    def test_a_frame_straddling_two_reads_waits_in_the_tail(self):
        frames, tail = fake.split_signalling(
            self.frame(1) + self.frame(2)[:10])
        self.assertEqual([fake.decode_signalling(f)["seq"] for f in frames],
                         [1])
        self.assertEqual(tail, self.frame(2)[:10])
        frames, tail = fake.split_signalling(
            tail + self.frame(2)[10:] + self.frame(3))
        self.assertEqual([fake.decode_signalling(f)["seq"] for f in frames],
                         [2, 3])
        self.assertEqual(tail, b"")

    def test_garbage_between_frames_is_skipped(self):
        frames, tail = fake.split_signalling(
            self.frame(1) + b"junk\x00\xff\x86" + self.frame(2))
        self.assertEqual([fake.decode_signalling(f)["seq"] for f in frames],
                         [1, 2])
        self.assertEqual(tail, b"")

    def test_a_trailing_partial_magic_is_kept(self):
        frames, tail = fake.split_signalling(self.frame(1) + b"\x86")
        self.assertEqual(len(frames), 1)
        self.assertEqual(tail, b"\x86")

    def test_a_corrupt_length_does_not_wedge_the_stream(self):
        corrupt = b"\x86\x86" + struct.pack("!HHHH", 0x0002, 60000, 1, 12)
        frames, tail = fake.split_signalling(corrupt + self.frame(1))
        self.assertEqual([fake.decode_signalling(f)["seq"] for f in frames],
                         [1])
        self.assertEqual(tail, b"")

    def test_empty_input_is_no_frames(self):
        self.assertEqual(fake.split_signalling(b""), ([], b""))


class SigStreamTests(unittest.TestCase):
    """The real handler answers a coalesced or straddling stream frame
    by frame, the way split_signalling promises."""

    def start(self):
        srv = fake.serve("sig", ("127.0.0.1", 0))
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return srv.server_address[1]

    def frame(self, seq=1):
        return fake.signalling_frame(0x0002, seq, b"140979857781", b"",
                                     pad=False)

    def replies(self, s, count, timeout=3.0):
        buf = b""
        deadline = time.time() + timeout
        while len(buf) < count and time.time() < deadline:
            try:
                buf += s.recv(4096)
            except socket.timeout:
                break
        return fake.split_signalling(buf)[0]

    def test_a_coalesced_chunk_is_answered_frame_by_frame(self):
        s = socket.create_connection(("127.0.0.1", self.start()), timeout=3)
        self.addCleanup(s.close)
        s.sendall(self.frame(1) + self.frame(2) + self.frame(3))
        frames = self.replies(s, 3 * 29)
        self.assertEqual(len(frames), 3, "each queued ping needs its answer")
        self.assertEqual(
            [fake.decode_signalling(f)["seq"] for f in frames], [1, 2, 3])
        self.assertEqual(
            {fake.decode_signalling(f)["cmd"] for f in frames}, {"0x8001"})

    def test_a_frame_straddling_reads_is_answered_when_complete(self):
        s = socket.create_connection(("127.0.0.1", self.start()), timeout=3)
        self.addCleanup(s.close)
        ping = self.frame(9)
        s.sendall(ping[:15])
        time.sleep(0.1)
        s.sendall(ping[15:])
        frames = self.replies(s, 29)
        self.assertEqual(len(frames), 1)
        decoded = fake.decode_signalling(frames[0])
        self.assertEqual(decoded["cmd"], "0x8001")
        self.assertEqual(decoded["seq"], 9)


class HeartbeatTests(unittest.TestCase):
    """The bridge's notify(keepAlive) every 2 s must reach the camera as
    the 0x8002 heartbeat.

    Only 0x8002 stamps the camera's liveness tick during play; without
    it the camera stopped every live view 36 s after its start
    (SIGNALLING_PROTOCOL.md §13.4, live 16:14-16:23), and every stream
    was a ~36 s burst restarted by the bridge's re-rendezvous.
    """

    UID = "140979857781"

    def setUp(self):
        self.out = io.StringIO()
        self._ctx = contextlib.redirect_stdout(self.out)
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)

    def api_with_link(self):
        api = fake.HttpApi()
        sent = []
        api.camera_links[self.UID] = _FakeSocket(sent)
        api.last_seq[self.UID] = 7
        return api, sent

    def notify(self, api, event):
        return api.respond("GET", "/api/v1/ipc/notify-live-event",
                           f"uid={self.UID}&eventType={event}")

    def bodies(self, sent):
        # Body = what follows the header and the uid, without the XOR
        # byte and the end symbol.
        return [f[22:-2] for f in sent]

    def test_the_frame_is_the_bare_header_plus_uid(self):
        frame = fake.vendor_heartbeat(self.UID, 7)
        decoded = fake.decode_signalling(frame)
        self.assertEqual(decoded["cmd"], "0x8002")
        self.assertEqual(decoded["len"], 12)
        self.assertEqual(decoded["seq"], 7)
        self.assertTrue(decoded["xor_ok"])

    def test_keepalive_becomes_a_heartbeat(self):
        api, sent = self.api_with_link()
        self.notify(api, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(fake.decode_signalling(sent[0])["cmd"], "0x8002")

    def test_stop_becomes_play_ctrl_3(self):
        api, sent = self.api_with_link()
        self.notify(api, 3)
        self.assertEqual(len(sent), 1)
        decoded = fake.decode_signalling(sent[0])
        self.assertEqual(decoded["cmd"], "0x8004")
        # Body offset 20 is the u16 command: 3 = stop.
        self.assertEqual(struct.unpack("!H", self.bodies(sent)[0][20:22]),
                         (3,))

    def test_pause_becomes_play_ctrl_2(self):
        api, sent = self.api_with_link()
        self.notify(api, 2)
        self.assertEqual(struct.unpack("!H", self.bodies(sent)[0][20:22]),
                         (2,))

    def test_a_heartbeat_without_a_link_is_logged_once_per_outage(self):
        api = fake.HttpApi()
        self.notify(api, 0)
        self.notify(api, 0)
        self.notify(api, 0)
        dropped = "0x8002 heartbeat: camera not connected"
        self.assertEqual(self.out.getvalue().count(dropped), 1)
        # The link coming back must be fed, and a fresh outage logged
        # again -- the silencing must not outlive the outage.
        api, sent = self.api_with_link()
        self.notify(api, 0)
        self.assertEqual(len(sent), 1)
        api.camera_links.clear()
        self.notify(api, 0)
        self.assertEqual(self.out.getvalue().count(dropped), 2)

    def test_start_still_fires_the_push_and_nothing_else(self):
        api, sent = self.api_with_link()
        fires = []
        real = api.schedule_p2p_live
        api.schedule_p2p_live = lambda uid: fires.append(uid)
        self.notify(api, 0)
        self.assertEqual(fires, [])
        self.notify(api, 1)
        self.assertEqual(fires, [self.UID])

    def test_connected_event_fires_the_push_like_start(self):
        # SIGNALLING_PROTOCOL.md §17.2: on an X5 newer than
        # V1.0.11.46 the app's P2P-established notify is eventType=4
        # (connected), never eventType=1 (start). The camera has one
        # live-start arm (PlayLiveview), so 4 must drive the same
        # direct chain.
        api, sent = self.api_with_link()
        fires = []
        api.schedule_p2p_live = lambda uid: fires.append(uid)
        self.notify(api, 4)
        self.assertEqual(fires, [self.UID])
        self.notify(api, 2)
        self.notify(api, 3)
        self.notify(api, 5)
        self.assertEqual(fires, [self.UID])


class DirectChainTests(unittest.TestCase):
    """The direct live-view chain is not the relay, and must not be
    gated behind the relay flag.

    The direct chain is `0x8101` (the camera learns the viewer's
    address and binds its P2P socket) then `0x8004` value 1
    (PlayLiveview) then the hello punch. It is the only chain that has
    ever produced frames (2026-10-24 16:14).

    It used to sit behind `--relay-start` together with the `0x8005`
    relay trigger, because the two were written next to each other.
    Turning the flag off -- which is what the docs ask for, because the
    `0x8005` breaks the camera's session -- silently disarmed the
    direct chain too: both cameras were on the network, registering,
    and sending no media at all, and the log said "0x8101 held".
    """

    UID = "140979857781"

    def setUp(self):
        self.out = io.StringIO()
        self._ctx = contextlib.redirect_stdout(self.out)
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)

    def make(self, relay_start: bool = False):
        api = fake.HttpApi(relay_start=relay_start)
        api.camera_links[self.UID] = _FakeSocket([])
        api.viewers[self.UID] = {"privateIp": "192.168.68.114",
                                 "privatePort": 48666}
        api.last_seq[self.UID] = 7
        return api

    def cmds(self, api):
        link = api.camera_links.get(self.UID)
        return [fake.decode_signalling(f)["cmd"] for f in link.sink]

    def test_the_viewer_address_goes_out_with_the_relay_flag_off(self):
        api = self.make(relay_start=False)
        api.respond("GET", "/api/v1/ipc/notify-live-event",
                    f"uid={self.UID}&eventType=1")
        self.assertIn("0x8101", self.cmds(api))
        self.assertNotIn("0x8005", self.cmds(api))

    def test_the_play_is_scheduled_behind_the_viewer_address(self):
        api = self.make(relay_start=False)
        api.respond("GET", "/api/v1/ipc/notify-live-event",
                    f"uid={self.UID}&eventType=1")
        # The 0x8004 must not fire before the camera had a chance to
        # bind its P2P socket -- the firmware rejects PlayLiveview while
        # it is unbound ("p2p is not finish").
        self.assertIn(self.UID, api.p2p_fire_at)
        self.assertGreater(api.p2p_fire_at[self.UID], time.monotonic())

    def test_a_second_start_does_not_crowd_out_the_pending_play(self):
        api = self.make(relay_start=False)
        api.respond("GET", "/api/v1/ipc/notify-live-event",
                    f"uid={self.UID}&eventType=1")
        due = api.p2p_fire_at[self.UID]
        api.respond("GET", "/api/v1/ipc/notify-live-event",
                    f"uid={self.UID}&eventType=1")
        # The pending play keeps its own time: the caller must not
        # overwrite it with a shorter delay.
        self.assertEqual(api.p2p_fire_at[self.UID], due)
        self.assertEqual(self.cmds(api).count("0x8101"), 1)

    def test_the_relay_trigger_stays_opt_in(self):
        # The 0x8005 is a different thing and stays behind the flag.
        api = self.make(relay_start=False)
        self.assertFalse(api.push_relay_trigger(self.UID))
        self.assertNotIn("0x8005", self.cmds(api))

    def test_the_play_waits_for_the_camera_to_bind_its_socket(self):
        # The firmware rejects PlayLiveview while the camera's P2P socket
        # is unbound ("p2p is not finish"), and the camera binds it when
        # it re-runs STUN from a new port -- so the play cannot go out on
        # a fixed timer. Live 2026-10-24 21:07: a 1.5 s timer fired it
        # before the camera's P2P STUN arrived, and no media came.
        api = self.make(relay_start=False)
        api.respond("GET", "/api/v1/ipc/notify-live-event",
                    f"uid={self.UID}&eventType=1")
        self.assertIn("0x8101", self.cmds(api))
        self.assertNotIn("0x8004", self.cmds(api))

    def test_the_play_goes_out_when_the_camera_binds_its_socket(self):
        api = self.make(relay_start=False)
        api.respond("GET", "/api/v1/ipc/notify-live-event",
                    f"uid={self.UID}&eventType=1")
        self.assertTrue(api.p2p_socket_bound(self.UID))
        # The play is scheduled, not immediate: the camera's freshest RTP
        # port and the viewer's hello have to land first.
        self.assertNotIn("0x8004", self.cmds(api))
        late = time.monotonic() + fake.P2P_PLAY_SETTLE_S + 1
        self.assertEqual(api.serve_due_p2p_fires(late), 1)
        self.assertIn("0x8004", self.cmds(api))

    def test_a_bound_socket_without_a_pending_play_sends_nothing(self):
        api = self.make(relay_start=False)
        self.assertFalse(api.p2p_socket_bound(self.UID))
        self.assertEqual(self.cmds(api), [])

    def test_the_play_is_dropped_when_the_camera_never_binds(self):
        # §16.3: a 0x8004 sent on the timer, with no mediaState-3
        # report, is the play the firmware ignores ("p2p is not
        # finish"). The deadline drops it.
        api = self.make(relay_start=False)
        api.respond("GET", "/api/v1/ipc/notify-live-event",
                    f"uid={self.UID}&eventType=1")
        late = time.monotonic() + fake.P2P_PLAY_FALLBACK_S + 1
        self.assertEqual(api.serve_due_p2p_fires(late), 0)
        self.assertNotIn("0x8004", self.cmds(api))
        self.assertNotIn(self.UID, api.p2p_fire_at)

    def test_a_new_socket_port_is_what_counts_as_bound(self):
        api = self.make(relay_start=False)
        api.cameras[self.UID] = {"_ip": "192.168.68.107", "_port": 52122}
        self.assertFalse(api.set_camera_address(self.UID, "192.168.68.107",
                                                52122))
        self.assertTrue(api.set_camera_address(self.UID, "192.168.68.107",
                                               51323))

    def test_the_relay_trigger_is_armed_by_the_registration(self):
        # The 0x8005 has to be armed somewhere: the camera sets its
        # relay-start flag when it arrives, and the armed address (the
        # bridge's UDP socket) is where it would push.
        api = self.make(relay_start=True)
        api.schedule_relay_trigger(self.UID)
        self.assertIn(self.UID, api.relay_trigger_at)
        late = time.monotonic() + fake.RELAY_START_DELAY_S + 1
        self.assertEqual(api.serve_due_relay_triggers(late), 1)
        self.assertIn("0x8005", self.cmds(api))

    def test_the_relay_trigger_is_not_armed_without_the_flag(self):
        api = self.make(relay_start=False)
        api.schedule_relay_trigger(self.UID)
        self.assertFalse(api.relay_trigger_at)

    def test_the_status_report_is_parsed(self):
        # §13.7: a count byte, then 4-byte entries `00 id 00 value`.
        one = fake.parse_status_report(bytes([1, 0, 3, 0, 3]))
        self.assertEqual(one, {3: 3})
        full = fake.parse_status_report(bytes(
            [8, 0, 1, 0, 0, 0, 2, 0, 1, 0, 3, 0, 1, 0, 4, 0, 1,
             0, 5, 0, 0, 0, 6, 0, 0, 0, 7, 0, 0, 0, 9, 0, 1]))
        self.assertEqual(full[3], 1)
        self.assertEqual(full[9], 1)

    def test_the_address_report_is_parsed(self):
        # §12.1: the 0x0003 body -- public pair at 0/20, local pair at
        # 22/42, the registration number it answers at 44.
        body = (b"186.233.129.211".ljust(20, b"\x00")
                + struct.pack("!H", 27719)
                + b"192.168.68.107".ljust(20, b"\x00")
                + struct.pack("!H", 59596)
                + struct.pack("!H", 186)
                + b"\x00")
        report = fake.parse_local_addr_report(body)
        self.assertEqual(report["public_ip"], "186.233.129.211")
        self.assertEqual(report["public_port"], 27719)
        self.assertEqual(report["local_ip"], "192.168.68.107")
        self.assertEqual(report["local_port"], 59596)
        self.assertEqual(report["seq_no"], 186)

    def test_a_short_address_report_is_refused(self):
        self.assertEqual(fake.parse_local_addr_report(b"\x00" * 10), {})

    def test_the_play_follows_the_cameras_own_p2p_report(self):
        # The camera reports the media state; 3 means "P2P socket bound,
        # waiting for the viewer" -- the precondition PlayLiveview
        # checks. Until then there must be no play.
        api = self.make(relay_start=False)
        api.respond("GET", "/api/v1/ipc/notify-live-event",
                    f"uid={self.UID}&eventType=1")
        self.assertIn("0x8101", self.cmds(api))
        self.assertNotIn("0x8004", self.cmds(api))
        # Now the camera says its socket is up.
        self.assertTrue(api.p2p_socket_bound(self.UID))
        late = time.monotonic() + fake.P2P_PLAY_SETTLE_S + 1
        self.assertEqual(api.serve_due_p2p_fires(late), 1)
        self.assertIn("0x8004", self.cmds(api))

    def test_the_media_state_is_served_from_the_camera(self):
        api = self.make(relay_start=False)
        api.cameras[self.UID] = {"mediaState": "0"}
        api.set_media_state(self.UID, 3)
        self.assertEqual(api.cameras[self.UID]["mediaState"], "3")


class DirectChainPortAndPairTests(unittest.TestCase):
    """The hello reaches every port the camera reported, and a moved
    viewer re-sends the 0x8101 (SIGNALLING_PROTOCOL.md §18.3-§18.6)."""

    UID = DirectChainTests.UID
    setUp = DirectChainTests.setUp
    make = DirectChainTests.make
    cmds = DirectChainTests.cmds

    def start(self, api):
        api.respond("GET", "/api/v1/ipc/notify-live-event",
                    f"uid={self.UID}&eventType=1")

    def listeners(self, n):
        socks = []
        for _ in range(n):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            s.settimeout(2)
            self.addCleanup(s.close)
            socks.append(s)
        return socks

    def test_the_hello_reaches_both_ports_the_camera_reported(self):
        api = self.make()
        self.start(api)
        first, second = self.listeners(2)
        # The latest report is the second port; the first must still be
        # punched.
        api.cameras[self.UID] = {"_ip": "127.0.0.1",
                                 "_port": second.getsockname()[1]}
        api.note_p2p_port(self.UID, first.getsockname()[1])
        api.note_p2p_port(self.UID, second.getsockname()[1])
        api.punch_camera(self.UID)
        self.assertEqual(first.recvfrom(16)[0], b"hello")
        self.assertEqual(second.recvfrom(16)[0], b"hello")
        self.assertIn(f"{first.getsockname()[1]},{second.getsockname()[1]}",
                      self.out.getvalue())

    def test_a_repeated_or_zero_port_is_not_noted_twice(self):
        api = self.make()
        api.note_p2p_port(self.UID, 5000)
        api.note_p2p_port(self.UID, 5000)
        api.note_p2p_port(self.UID, 0)
        self.assertEqual(api.p2p_ports[self.UID], [5000])

    def test_a_new_0x8101_forgets_the_previous_chains_ports(self):
        api = self.make()
        api.note_p2p_port(self.UID, 5000)
        self.start(api)
        self.assertEqual(api.p2p_ports[self.UID], [])

    def test_a_pending_chain_with_the_same_viewer_skips_the_second_0x8101(self):
        api = self.make()
        self.start(api)
        api.schedule_p2p_live(self.UID)
        self.assertEqual(self.cmds(api).count("0x8101"), 1)

    def test_a_pending_chain_with_a_moved_viewer_resends_the_0x8101(self):
        api = self.make()
        self.start(api)
        api.p2p_ready[self.UID] = time.monotonic()
        api.viewers[self.UID] = {"privateIp": "192.168.68.114",
                                 "privatePort": 51111}
        api.schedule_p2p_live(self.UID)
        self.assertEqual(self.cmds(api).count("0x8101"), 2)
        self.assertNotIn(self.UID, api.p2p_ready)
        self.assertEqual(api.p2p_sent_pair[self.UID][3], 51111)
        link = api.camera_links[self.UID]
        self.assertIn(struct.pack(">H", 51111), link.sink[-1])
        self.assertIn("viewer moved", self.out.getvalue())


class DirectChainAnswerTests(unittest.TestCase):
    """stun-addr serves the 0x0003 that answered the 0x8101, not the
    latest one: a redraw must not move the bridge's punch target
    (SIGNALLING_PROTOCOL.md §14.1, §18.8)."""

    UID = DirectChainTests.UID
    setUp = DirectChainTests.setUp
    make = DirectChainTests.make
    cmds = DirectChainTests.cmds

    def sent(self):
        api = self.make()
        api.cameras[self.UID] = {"_ip": "192.168.68.107", "_port": 1111}
        api.schedule_p2p_live(self.UID)
        return api, api.p2p_sent_seq[self.UID]

    def redraw(self, api, port):
        # What the 0x0003 branch does with a report.
        api.set_camera_address(self.UID, "192.168.68.107", port)
        api.set_camera_public(self.UID, "200.1.1.1", port + 1)

    def test_the_header_seq_of_the_0x8101_is_remembered(self):
        api, seq = self.sent()
        self.assertEqual(seq, 7)

    def test_a_redraw_after_the_answer_does_not_move_stun_addr(self):
        api, seq = self.sent()
        self.assertTrue(api.note_p2p_answer(
            self.UID, seq, "200.1.1.1", 2001, "192.168.68.107", 2000))
        self.redraw(api, 3000)
        out = api._stun(self.UID)
        self.assertEqual(out["IpcPrivatePort"], 2000)
        self.assertEqual(out["IpcPublicPort"], 2001)
        self.assertEqual(out["IpcPrivateIP"], "192.168.68.107")
        self.assertIn("answers 0x8101 seq 7", self.out.getvalue())

    def test_a_report_with_another_seq_is_not_the_answer(self):
        api, seq = self.sent()
        self.assertFalse(api.note_p2p_answer(
            self.UID, 0, "200.1.1.1", 2001, "192.168.68.107", 2000))
        self.assertFalse(api.note_p2p_answer(
            self.UID, seq + 1, "200.1.1.1", 2001, "192.168.68.107", 2000))
        self.assertNotIn(self.UID, api.p2p_answer)
        self.redraw(api, 3000)
        self.assertEqual(api._stun(self.UID)["IpcPrivatePort"], 3000)

    def test_a_new_0x8101_clears_the_answer(self):
        api, seq = self.sent()
        api.note_p2p_answer(self.UID, seq, "200.1.1.1", 2001,
                            "192.168.68.107", 2000)
        api.schedule_p2p_live(self.UID, force=True)
        self.assertNotIn(self.UID, api.p2p_answer)
        self.redraw(api, 3000)
        self.assertEqual(api._stun(self.UID)["IpcPrivatePort"], 3000)

    def test_without_a_0x8101_nothing_is_an_answer(self):
        api = self.make()
        self.assertFalse(api.note_p2p_answer(
            self.UID, 7, "200.1.1.1", 2001, "192.168.68.107", 2000))


class DirectChainResendFloorTests(unittest.TestCase):
    """An unchanged viewer pair gets one 0x8101 per P2P_RESEND_MIN_S: the
    bridge's retries otherwise burst 0x8101s onto a camera that is live,
    and that took one off the network (SIGNALLING_PROTOCOL.md §18.7)."""

    UID = DirectChainTests.UID
    setUp = DirectChainTests.setUp
    make = DirectChainTests.make
    cmds = DirectChainTests.cmds

    def first(self):
        api = self.make()
        api.schedule_p2p_live(self.UID)
        # The chain has completed: nothing is pending any more.
        api.p2p_fire_at.pop(self.UID, None)
        return api

    def test_a_same_pair_resend_inside_the_floor_is_held(self):
        api = self.first()
        api.schedule_p2p_live(self.UID)
        self.assertEqual(self.cmds(api).count("0x8101"), 1)
        self.assertIn("-- held", self.out.getvalue())
        self.assertNotIn(self.UID, api.p2p_fire_at)

    def test_a_same_pair_resend_after_the_floor_is_sent(self):
        api = self.first()
        api.p2p_sent_at[self.UID] -= fake.P2P_RESEND_MIN_S
        api.schedule_p2p_live(self.UID)
        self.assertEqual(self.cmds(api).count("0x8101"), 2)
        self.assertNotIn("-- held", self.out.getvalue())

    def test_a_moved_pair_inside_the_floor_is_sent(self):
        api = self.first()
        api.viewers[self.UID] = {"privateIp": "192.168.68.114",
                                 "privatePort": 51111}
        api.schedule_p2p_live(self.UID)
        self.assertEqual(self.cmds(api).count("0x8101"), 2)
        self.assertEqual(api.p2p_sent_pair[self.UID][3], 51111)

    def test_force_inside_the_floor_sends_and_clears_the_old_chain(self):
        api = self.make()
        api.schedule_p2p_live(self.UID)
        api.p2p_ready[self.UID] = time.monotonic()
        stale = api.p2p_fire_at[self.UID] = time.monotonic() - 100
        api.schedule_p2p_live(self.UID, force=True)
        self.assertEqual(self.cmds(api).count("0x8101"), 2)
        self.assertNotIn(self.UID, api.p2p_ready)
        self.assertGreater(api.p2p_fire_at[self.UID], stale)

    def test_the_register_path_forces_the_0x8101(self):
        import inspect
        self.assertIn("api.schedule_p2p_live(uid, force=True)",
                      inspect.getsource(fake.StreamHandler))


class RelayTriggerTests(unittest.TestCase):
    """Media starts with two frames, not a search.

    The firmware made this exact: the registration answer (`0x8000`)
    arms a UDP RTP target (the camera sendto's JPEG + PCMU there) and
    an unsolicited `0x8005` -- empty body, `len` 12 -- sets the
    relay-start flag it pushes from (`ziot-fw/SIGNALLING_PROTOCOL.md`
    §5-§6). These tests pin the pair: the trigger's exact bytes, the
    seq echo, the trigger being held until --relay-start (fired
    unsolicited on 2026-10-24 it broke the handshake, so it is off by
    default and flies once per arm), the keepalive carrying only the
    validated go-ahead, and a camera with no connection being logged
    rather than written to.
    """

    def setUp(self):
        self.out = io.StringIO()
        self._ctx = contextlib.redirect_stdout(self.out)
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)

    UID = "140979857781"

    def make(self, relay_start: bool = True):
        api = fake.HttpApi(relay_start=relay_start)
        api.camera_links[self.UID] = _FakeSocket([])
        api.viewers[self.UID] = {"privateIp": "192.168.68.114",
                                 "privatePort": 48666}
        api.last_seq[self.UID] = 7
        return api

    def frames(self, api):
        link = api.camera_links.get(self.UID)
        return link.sink if link is not None else []

    def triggers(self, api):
        """The frames that carry the relay trigger."""
        out = []
        for frame in self.frames(api):
            d = fake.decode_signalling(frame)
            if int(d["cmd"], 16) == 0x8005:
                out.append(d)
        return out

    def cmds(self, api):
        """Every frame's command id, in order."""
        return [fake.decode_signalling(f)["cmd"] for f in self.frames(api)]

    def test_the_trigger_is_the_bare_frame_the_firmware_expects(self):
        api = self.make()
        self.assertTrue(api.push_relay_trigger(self.UID))
        frames = self.frames(api)
        self.assertEqual(len(frames), 1)
        # 86 86 | 80 05 | len 000c | seq 0007 | uidLen 000c | uid | xor | 0d
        self.assertEqual(len(frames[0]), 24)
        self.assertEqual(frames[0][:5], b"\x86\x86\x80\x05\x00")
        self.assertEqual(frames[0][-1:], b"\x0d")
        d = fake.decode_signalling(frames[0])
        self.assertEqual(d["cmd"], "0x8005")
        self.assertEqual(d["len"], 12)          # the uid, and nothing else
        self.assertEqual(d["seq"], 7)           # the camera's own counter
        self.assertEqual(d["uid"], self.UID)
        self.assertEqual(d["bytes"], 24)

    def test_the_keepalive_carries_only_the_go_ahead(self):
        # The trigger is NOT on the keepalive beat: firing it every two
        # seconds put the camera into a reconnect loop and produced no
        # media. It goes out on an explicit live-view press only.
        api = self.make()
        self.assertTrue(api.keepalive(self.UID))
        cmds = [fake.decode_signalling(f)["cmd"] for f in self.frames(api)]
        self.assertEqual(cmds, ["0x8001"])
        self.assertEqual(self.triggers(api), [])

    def test_the_keepalive_feeds_the_session_without_a_viewer(self):
        api = fake.HttpApi()
        api.camera_links[self.UID] = _FakeSocket([])
        api.last_seq[self.UID] = 3
        self.assertTrue(api.keepalive(self.UID))
        cmds = [fake.decode_signalling(f)["cmd"] for f in self.frames(api)]
        self.assertEqual(cmds, ["0x8001"])

    def test_push_start_fires_the_trigger(self):
        api = self.make()
        api.push_start(self.UID)
        self.assertEqual(len(self.triggers(api)), 1)
        self.assertIn("relay-trigger", self.out.getvalue())

    def test_push_start_with_no_connection_is_logged_not_sent(self):
        api = self.make()
        api.camera_links.clear()
        api.push_start(self.UID)          # must not raise
        self.assertEqual(self.triggers(api), [])
        self.assertIn("not connected", self.out.getvalue())

    def test_the_trigger_is_held_without_relay_start(self):
        # Firing the unsolicited 0x8005 made the camera reconnect-loop
        # live, so it is opt-in until a capture shows the media.
        api = self.make(relay_start=False)
        self.assertFalse(api.push_relay_trigger(self.UID))
        self.assertEqual(self.triggers(api), [])
        self.assertIn("--relay-start off", self.out.getvalue())

    def test_a_trigger_needs_a_connection_to_fly(self):
        api = self.make()
        api.camera_links.clear()
        self.assertFalse(api.push_relay_trigger(self.UID))
        self.assertEqual(self.triggers(api), [])

    def test_only_event_type_1_fires_a_push(self):
        # eventType=1 ("start live") is the app's live-view press and it
        # starts the DIRECT chain: the 0x8101 that hands the camera the
        # viewer's address, so the camera binds its P2P socket. It does
        # not fire the 0x8005 relay trigger -- that one is opt-in
        # (--relay-start) and pushing it broke the camera's session.
        api = self.make()
        api.respond("GET", "/api/v1/ipc/notify-live-event",
                    f"uid={self.UID}&eventType=20")
        self.assertEqual(self.triggers(api), [])
        self.assertEqual(self.cmds(api), [])
        api.respond("GET", "/api/v1/ipc/notify-live-event",
                    f"uid={self.UID}&eventType=1")
        # The viewer address goes out ...
        self.assertIn("0x8101", self.cmds(api))
        # ... and the relay trigger does not.
        self.assertEqual(self.triggers(api), [])


class RelayTriggerTimingTests(unittest.TestCase):
    """The delayed `0x8005` lands on a settled session or not at all.

    Firing the trigger inside the handshake broke the camera's session
    live (2026-10-24), so when --relay-start opts in, the fire waits
    RELAY_START_DELAY_S and is only sent while the camera is still
    connected; a camera that dropped during the wait is discarded, and
    its next register schedules it again.
    """

    UID = "141030191094"

    def setUp(self):
        self.out = io.StringIO()
        self._ctx = contextlib.redirect_stdout(self.out)
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)

    def make(self, relay_start: bool = True):
        api = fake.HttpApi(relay_start=relay_start)
        api.camera_links[self.UID] = _FakeSocket([])
        api.last_seq[self.UID] = 7
        return api

    def frame_cmds(self, api):
        link = api.camera_links.get(self.UID)
        if link is None:
            return []
        return [fake.decode_signalling(f)["cmd"] for f in link.sink]

    def test_the_arm_schedules_one_fire_a_delay_out(self):
        api = self.make()
        api.schedule_relay_trigger(self.UID)
        self.assertIn(self.UID, api.relay_trigger_at)
        # Nothing flies while it is not yet due.
        self.assertEqual(api.serve_due_relay_triggers(), 0)
        self.assertEqual(self.frame_cmds(api), [])

    def test_a_due_fire_flies_exactly_once(self):
        api = self.make()
        api.schedule_relay_trigger(self.UID)
        late = time.monotonic() + fake.RELAY_START_DELAY_S + 1
        self.assertEqual(api.serve_due_relay_triggers(late), 1)
        self.assertEqual(self.frame_cmds(api), ["0x8005"])
        self.assertFalse(api.relay_trigger_at)      # one pending, no more
        self.assertEqual(api.serve_due_relay_triggers(late), 0)

    def test_a_fire_for_a_dropped_camera_is_discarded(self):
        api = self.make()
        api.schedule_relay_trigger(self.UID)
        api.camera_links.clear()
        late = time.monotonic() + fake.RELAY_START_DELAY_S + 1
        self.assertEqual(api.serve_due_relay_triggers(late), 0)
        self.assertFalse(api.relay_trigger_at)
        self.assertIn("not connected", self.out.getvalue())

    def test_a_second_arm_does_not_crowd_out_the_first_fire(self):
        api = self.make()
        api.schedule_relay_trigger(self.UID)
        api.schedule_relay_trigger(self.UID)   # next register, still pending
        self.assertEqual(len(api.relay_trigger_at), 1)

    def test_scheduling_is_a_noop_without_relay_start(self):
        api = self.make(relay_start=False)
        api.schedule_relay_trigger(self.UID)
        self.assertFalse(api.relay_trigger_at)


class ArmTargetTests(unittest.TestCase):
    """The registration answer must arm a UDP RTP sink, not an RTSP server.

    The camera never speaks RTSP on this leg (no ANNOUNCE/RECORD exists in
    the firmware): after `0x8005` it opens a UDP socket and sendto's its
    RTP to the armed ip:port. So the armed target has to be the socket
    that actually reads those datagrams -- the bridge's own media socket
    -- and not `ziot_relay.py`, which waits for an ANNOUNCE that never
    comes.
    """

    UID = "141030191094"

    def test_the_viewer_socket_wins_over_the_configured_relay(self):
        viewer = {"privateIp": "192.168.68.114", "privatePort": 58918}
        self.assertEqual(
            fake.arm_target(viewer, "192.168.68.114", 8554, "192.168.68.114"),
            ("192.168.68.114", 58918))

    def test_the_registration_answer_carries_the_viewer_port_in_both_slots(self):
        api = fake.HttpApi()                       # no --relay-port
        api.viewers[self.UID] = {"privateIp": "192.168.68.114",
                                 "privatePort": 58918}
        srv = fake.StreamServer(("127.0.0.1", 0), "sig", api,
                                "192.168.68.114", "192.168.68.114", 0, 0)
        self.addCleanup(srv.server_close)
        handler = fake.StreamHandler.__new__(fake.StreamHandler)
        handler.server = srv
        frame, target = handler._vendor_answer(self.UID, 1,
                                               "192.168.68.108", api)
        self.assertEqual(target, ("192.168.68.114", 58918))
        cmd, length, seq, uid_len = struct.unpack_from(">HHHH", frame, 2)
        self.assertEqual(cmd, 0x8000)
        body = frame[10 + uid_len:10 + uid_len + length]
        # The camera's dispatcher reads offset 0x18; both slots agree.
        self.assertEqual(struct.unpack_from(">H", body, 0x16)[0], 58918)
        self.assertEqual(struct.unpack_from(">H", body, 0x18)[0], 58918)
        self.assertEqual(body[2:22].rstrip(b"\x00"), b"192.168.68.114")

    def test_with_nothing_known_the_register_is_still_answered(self):
        # An unanswered register retransmits forever, so the answer must
        # exist even with no viewer and no relay: it arms this host.
        self.assertEqual(
            fake.arm_target({}, "192.168.68.114", 0, "192.168.68.114"),
            ("192.168.68.114", 0))


class CameraLinkTests(unittest.TestCase):
    """camera_links maps a uid to the very socket the camera is on.

    A link left behind after its socket closed is what made 22 pushes
    end `Bad file descriptor` in one morning: the handler's finish()
    drops them now, and only the ones bound to the socket that died.
    """

    UID = "140979857781"

    def serve(self):
        api = fake.HttpApi()
        srv = fake.serve("sig", ("127.0.0.1", 0), api)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return api, srv.server_address[1]

    def link(self, port):
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(fake.signalling_frame(0x0001, 1, self.UID.encode(), b"reg"))
        return s

    def test_a_closed_camera_connection_is_forgotten(self):
        api, port = self.serve()
        s = self.link(port)
        deadline = time.time() + 3
        while not api.camera_links and time.time() < deadline:
            time.sleep(0.02)
        self.assertIn(self.UID, api.camera_links)
        s.close()
        deadline = time.time() + 3
        while api.camera_links and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(api.camera_links, {}, "the dead link survives")

    def test_a_dropped_link_leaves_the_camera_idle(self):
        # §17.1: a mediaState other than "0" makes the app refuse to
        # open the camera. The camera cannot report the stop once its
        # sig link is gone, so the drop itself is the idle report.
        api, port = self.serve()
        s = self.link(port)
        deadline = time.time() + 3
        while self.UID not in api.camera_links and time.time() < deadline:
            time.sleep(0.02)
        api.set_media_state(self.UID, 1)
        s.close()
        deadline = time.time() + 3
        while api.cameras.get(self.UID, {}).get("mediaState") != "0" \
                and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(api.cameras[self.UID]["mediaState"], "0")

    def test_a_replaced_connection_survives_the_old_close(self):
        """A camera that reconnects must not lose its new link when the
        old socket's handler is still unwinding."""
        api, port = self.serve()
        old = self.link(port)
        deadline = time.time() + 3
        while self.UID not in api.camera_links and time.time() < deadline:
            time.sleep(0.02)
        # The dict holds the server-side accepted socket, not our client
        # end, so the swap is observed through the dict rather than by
        # comparing against `old`/`new`.
        first = api.camera_links[self.UID]
        new = self.link(port)
        deadline = time.time() + 3
        while api.camera_links.get(self.UID) is first and time.time() < deadline:
            time.sleep(0.02)
        self.assertIsNot(api.camera_links.get(self.UID), first,
                         "the reconnect never replaced the link")
        second = api.camera_links[self.UID]
        old.close()                      # the OLD socket goes away
        # Give the old handler time to unwind through finish(): it must
        # not drop the link the reconnect installed.
        time.sleep(0.3)
        self.assertIn(self.UID, api.camera_links, "the new link was dropped")
        self.assertIs(api.camera_links.get(self.UID), second)
        new.close()


class IdleWindowTests(unittest.TestCase):
    """The sig connection must outlive the camera's silent wait.

    A camera holds its connection 40-90 s without saying a word while
    it waits for the server push; the old flat 10 s timeout closed it
    underneath every push. The windows are pinned so the constants
    cannot drift away from the camera's measured cycle.
    """

    UID = "141030191094"

    def test_the_idle_windows_match_the_measured_cycle(self):
        # 75 s: just over the longest observed silent gap; 2 s: the
        # keepalive beat, same as the vendor's.
        self.assertEqual(fake.STREAM_IDLE_S, 75.0)
        self.assertEqual(fake.KEEPALIVE_INTERVAL, 2.0)

    def test_a_sig_connection_idles_out_and_drops_its_link(self):
        old = fake.STREAM_IDLE_S
        fake.STREAM_IDLE_S = 0.25
        self.addCleanup(setattr, fake, "STREAM_IDLE_S", old)
        api = fake.HttpApi()
        srv = fake.serve("sig", ("127.0.0.1", 0), api)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        port = srv.server_address[1]
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(fake.signalling_frame(0x0001, 1, self.UID.encode(), b"reg"))
        # The connection says nothing more: after the idle window the
        # handler gives it back, and the link goes with it.
        seen = False
        deadline = time.time() + 3
        while time.time() < deadline:
            if api.camera_links:
                seen = True
            if seen and not api.camera_links:
                break
            time.sleep(0.02)
        self.assertTrue(seen, "the camera was never linked")
        self.assertFalse(api.camera_links,
                         "the idle connection was never closed")
        s.close()


class TokenWarnTests(unittest.TestCase):
    """A token the broker did not mint is noticed, once, not enforced:
    enforcing it would put the bridge into its dead-end 401 path."""

    def setUp(self):
        self.out = io.StringIO()
        self._ctx = contextlib.redirect_stdout(self.out)
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)
        self.addCleanup(fake._warned_tokens.clear)

    def test_a_foreign_token_is_logged_once(self):
        fake._warned_tokens.clear()
        api = fake.HttpApi()
        api.respond("GET", "/api/v1/ipc", token="vendor.jwt.rest")
        api.respond("GET", "/api/v1/ipc", token="vendor.jwt.rest")
        self.assertEqual(
            self.out.getvalue().count("not issued by this broker"), 1)

    def test_a_locally_minted_token_is_not_logged(self):
        fake._warned_tokens.clear()
        api = fake.HttpApi()
        api.respond("GET", "/api/v1/ipc", token=fake.mint_local_token())
        self.assertNotIn("not issued by this broker", self.out.getvalue())

    def test_no_token_logs_nothing(self):
        fake._warned_tokens.clear()
        api = fake.HttpApi()
        api.respond("GET", "/api/v1/ipc")
        self.assertNotIn("not issued by this broker", self.out.getvalue())




class LocalTokenTests(unittest.TestCase):
    """The local cloud issues its own token, in the vendor's shape."""

    def test_the_minted_token_is_accepted(self):
        token = fake.mint_local_token()
        self.assertTrue(fake.local_token_valid(token))
        self.assertEqual(token.count("."), 2)

    def test_the_minted_token_carries_the_vendors_claims(self):
        import base64
        token = fake.mint_local_token()
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(
            payload + "=" * (-len(payload) % 4)))
        # The same claims the vendor's token has (read from a live one).
        for key in ("email", "exp", "family_id", "lang", "orig_iat",
                    "role_id", "user_id", "username"):
            self.assertIn(key, claims)

    def test_a_token_with_a_broken_signature_is_rejected(self):
        token = fake.mint_local_token()
        head, payload, _ = token.split(".")
        self.assertFalse(fake.local_token_valid(f"{head}.{payload}.AAAA"))
        self.assertFalse(fake.local_token_valid("not-a-token"))


class _FakeSocket:
    """A stand-in for a camera's signalling connection."""

    def __init__(self, sink):
        self.sink = sink

    def sendall(self, data):
        self.sink.append(data)


class StunTests(unittest.TestCase):
    """The camera asks this server for its own address; the answer has to
    look like the vendor's or the camera will not register."""

    # The request and reply from the captured app session.
    REQUEST = bytes.fromhex("00010000150e529c613d2c1d45cfa7fcd00f116e")
    VENDOR_REPLY = bytes.fromhex(
        "01010024150e529c613d2c1d45cfa7fcd00f116e"
        "000100080001fc9cb1dd7874"     # MAPPED-ADDRESS
        "00040008000134a6ac106e40"     # the server's own address
        "802000080001e992a4d32ae8")     # the peer again, XOR-obfuscated

    def attrs(self, reply):
        out = {}
        pos = 20
        while pos + 4 <= len(reply):
            atype = int.from_bytes(reply[pos:pos + 2], "big")
            alen = int.from_bytes(reply[pos + 2:pos + 4], "big")
            out[atype] = reply[pos + 4:pos + 4 + alen]
            pos += 4 + alen
        return out

    def test_header_cookie_and_transaction_id_are_echoed(self):
        reply = fake.stun_binding_success(self.REQUEST, "177.221.120.116",
                                          64668, "172.16.110.64", 13478)
        # Binding Success, the same cookie and txid.
        self.assertEqual(reply[:2], b"\x01\x01")
        # The length covers the attribute block only: four attributes of
        # eight bytes each, plus their four-byte headers.
        self.assertEqual(reply[2:4], b"\x00\x30")
        self.assertEqual(reply[4:20], self.REQUEST[4:20])

    def test_the_camera_gets_the_xor_mapped_address_it_accepts(self):
        # The camera's STUN client (`CUSTOM_TASK_StunXorAddr`) accepts a
        # Binding Success only if it carries a standard XOR-MAPPED-ADDRESS
        # (attribute 0x0020). Without it the camera retries four times
        # from fresh ports and then closes its own signalling socket --
        # which is what every session before 2026-10-24 ended on.
        reply = fake.stun_binding_success(self.REQUEST, "177.221.120.116",
                                          64668, "172.16.110.64", 13478)
        attrs = self.attrs(reply)
        self.assertIn(0x0020, attrs)
        val = attrs[0x0020]
        port = int.from_bytes(val[2:4], "big") ^ 0x2112
        mask = reply[4:8] + reply[8:12]
        ip = bytes(x ^ y for x, y in zip(val[4:8], mask))
        self.assertEqual(port, 64668)
        self.assertEqual(".".join(str(b) for b in ip), "177.221.120.116")

    def test_mapped_address_and_server_address_match_the_vendor(self):
        reply = fake.stun_binding_success(self.REQUEST, "177.221.120.116",
                                          64668, "172.16.110.64", 13478)
        ours, vendor = self.attrs(reply), self.attrs(self.VENDOR_REPLY)
        self.assertEqual(ours[0x0001], vendor[0x0001])   # MAPPED-ADDRESS
        self.assertEqual(ours[0x0004], vendor[0x0004])   # the server itself

    def test_the_xor_attribute_decodes_back_to_the_peer(self):
        reply = fake.stun_binding_success(self.REQUEST, "177.221.120.116",
                                          64668, "172.16.110.64", 13478)
        val = self.attrs(reply)[0x8020]
        port = int.from_bytes(val[2:4], "big") ^ 0x2112
        mask = reply[4:8] + reply[8:12]
        ip = bytes(x ^ y for x, y in zip(val[4:8], mask))
        self.assertEqual(port, 64668)
        self.assertEqual(".".join(str(b) for b in ip), "177.221.120.116")

    def test_a_non_binding_request_is_not_answered(self):
        self.assertEqual(fake.stun_binding_success(b"\x00" * 20, "1.2.3.4",
                                                    1, "5.6.7.8", 13478), b"")

    def test_the_server_answers_over_udp(self):
        srv = fake.StunServer(("127.0.0.1", 0), "127.0.0.1")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.sendto(self.REQUEST, srv.server_address)
        reply, _ = s.recvfrom(512)
        s.close()
        self.assertEqual(reply[:2], b"\x01\x01")
        self.assertEqual(reply[4:20], self.REQUEST[4:20])
        # The peer is told the address its request came from.
        mapped = self.attrs(reply)[0x0001]
        self.assertEqual(".".join(str(b) for b in mapped[4:8]), "127.0.0.1")


class ClaimIpsTests(unittest.TestCase):
    """`--claim-ips` makes this host answer for the vendor's addresses, so
    a static route in the router can send the camera's cloud traffic here."""

    def args(self, print_only=True):
        return mock.Mock(claim_print_only=print_only, claim_iface="wlan0")

    def test_route_entries_name_every_vendor_address(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = fake.claim_vendor_ips(self.args())
        self.assertEqual(rc, 0)
        text = out.getvalue()
        for ip in fake.VENDOR_IPS:
            self.assertIn(ip, text)
        # The router needs the gateway to be this host and a host route.
        self.assertIn("255.255.255.255", text)

    def test_print_only_does_not_touch_the_host(self):
        with mock.patch.object(fake.subprocess, "run") as run:
            with contextlib.redirect_stdout(io.StringIO()):
                fake.claim_vendor_ips(self.args())
        run.assert_not_called()

    def test_already_assigned_counts_as_claimed(self):
        """Re-running the command must not report failure."""
        done = mock.Mock(returncode=2,
                         stdout="",
                         stderr="RTNETLINK answers: File exists\n"
                                "Error: ipv4: Address already assigned.")
        with mock.patch.object(fake.subprocess, "run", return_value=done):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = fake.claim_vendor_ips(self.args(print_only=False))
        self.assertEqual(rc, 0)

    def test_real_failure_is_reported(self):
        done = mock.Mock(returncode=1, stdout="",
                         stderr="RTNETLINK answers: Operation not permitted")
        with mock.patch.object(fake.subprocess, "run", return_value=done):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = fake.claim_vendor_ips(self.args(print_only=False))
        self.assertEqual(rc, 1)


class EnvTests(unittest.TestCase):
    def test_env_values_are_loaded_and_comments_skipped(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# comment\nWIFI_SSID=House of Cats\nWIFI_PWD= s3cret \n"
                    "NOEQUALS\n\nBLE_MAC=E0:44:40:60:68:82\n")
            path = f.name
        self.addCleanup(os.unlink, path)
        env = fake._load_env(path)
        self.assertEqual(env["WIFI_SSID"], "House of Cats")
        self.assertEqual(env["WIFI_PWD"], "s3cret")
        self.assertEqual(env["BLE_MAC"], "E0:44:40:60:68:82")
        self.assertNotIn("NOEQUALS", env)

    def test_missing_env_file_is_empty(self):
        self.assertEqual(fake._load_env("/nonexistent/.env"), {})


class VendorConformanceTests(unittest.TestCase):
    """The broker's answers must match what the vendor actually sent.

    Every expectation here comes from the live app session captured in
    PCAPdroid_29_ago._15_13_27.pcap (DECOMPILATION_REPORT.md section 11):
    the header block, the envelope, the device record's field set and
    order, the trailing-slash redirect and the per-endpoint "msg".
    """

    # The device row the vendor returned, in the order it returned it.
    VENDOR_ROW_FIELDS = [
        "uid", "name", "email", "pwd", "deviceType", "version", "familyId",
        "terminalFamilyId", "natType", "wifiSsid", "connectionState",
        "interpolation", "remark", "startTime", "createdAt", "updatedAt",
        "createBy", "updateBy", "commTime", "status", "relay_ip", "sd_total",
        "sd_available", "power", "signal", "online_time", "server_ip",
        "event_sensitivity", "resolution", "onlineState", "sdState",
        "sdPolicy", "infraredLight", "lampLight", "vflip", "hflip", "led",
        "mediaState", "ringing", "ringingTime", "charged",
        "isShowRemoteService", "isRemoteServiceExpired",
        "remoteServiceEndTime", "showAd", "unReadEventCount",
    ]

    def setUp(self):
        self.api = fake.HttpApi()
        self.api.add_camera("141030191094", "192.168.68.50", 34567)

    def raw(self, method, path, query="", body=b""):
        return self.api.respond(method, path, query, body)

    def test_device_row_has_the_vendor_fields_in_the_vendor_order(self):
        raw = self.raw("GET", "/api/v1/ipc", "terminalFamilyId=3132031")
        row = json.loads(raw.partition(b"\r\n\r\n")[2])["data"]["list"][0]
        self.assertEqual([k for k in row if not k.startswith("_")],
                         self.VENDOR_ROW_FIELDS)

    def test_device_row_types_match_the_vendor(self):
        """The app parses these: onlineState and mediaState are strings,
        the remote-service flags are booleans, the counters are numbers."""
        raw = self.raw("GET", "/api/v1/ipc", "terminalFamilyId=3132031")
        row = json.loads(raw.partition(b"\r\n\r\n")[2])["data"]["list"][0]
        self.assertIsInstance(row["onlineState"], str)
        self.assertIsInstance(row["mediaState"], str)
        self.assertIsInstance(row["isShowRemoteService"], bool)
        self.assertIsInstance(row["isRemoteServiceExpired"], bool)
        self.assertIsInstance(row["unReadEventCount"], int)
        self.assertIsInstance(row["familyId"], int)
        self.assertIsInstance(row["terminalFamilyId"], int)
        self.assertIsInstance(row["createBy"], int)
        self.assertIsInstance(row["updateBy"], int)

    def test_list_envelope_matches_the_vendor(self):
        raw = self.raw("GET", "/api/v1/ipc", "terminalFamilyId=3132031")
        data = json.loads(raw.partition(b"\r\n\r\n")[2])["data"]
        self.assertEqual(list(data.keys()),
                         ["count", "pageIndex", "pageSize", "list", "showAd"])

    def test_response_carries_the_vendor_headers(self):
        raw = self.raw("GET", "/api/v1/ipc", "terminalFamilyId=3132031")
        head = raw.partition(b"\r\n\r\n")[0].decode()
        self.assertIn("Server: nginx/1.24.0 (Ubuntu)", head)
        self.assertIn("Content-Type: application/json; charset=utf-8", head)
        self.assertIn("Access-Control-Allow-Origin: *", head)
        self.assertIn("Cache-Control: no-cache, no-store, max-age=0,"
                      " must-revalidate", head)
        self.assertIn("Expires: Thu, 01 Jan 1970 00:00:00 GMT", head)
        self.assertIn("X-Content-Type-Options: nosniff", head)
        self.assertIn("X-Xss-Protection: 1; mode=block", head)
        self.assertIn("Connection: keep-alive", head)

    def test_trailing_slash_draws_a_301_to_the_bare_path(self):
        """The vendor's nginx redirects /api/v1/ipc/?... to /api/v1/ipc?...;
        the app follows it. The query must survive the redirect."""
        raw = self.raw("GET", "/api/v1/ipc/", "terminalFamilyId=3132031")
        head, _, body = raw.partition(b"\r\n\r\n")
        head = head.decode()
        self.assertIn("HTTP/1.1 301 Moved Permanently", head)
        self.assertIn("Location: /api/v1/ipc?terminalFamilyId=3132031", head)
        self.assertIn("Content-Type: text/html; charset=utf-8", head)
        self.assertEqual(body,
                         b'<a href="/api/v1/ipc?terminalFamilyId=3132031">'
                         b'Moved Permanently</a>.\n\n')

    def test_envelope_keys_match_the_vendor(self):
        raw = self.raw("GET", "/api/v1/ipc", "terminalFamilyId=3132031")
        body = json.loads(raw.partition(b"\r\n\r\n")[2])
        self.assertEqual(list(body.keys()),
                         ["requestId", "code", "msg", "data"])
        # The request id is a UUID, as the vendor's is.
        self.assertRegex(body["requestId"],
                         r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                         r"[0-9a-f]{4}-[0-9a-f]{12}$")

    def test_package_record_keeps_the_vendors_chinese_message(self):
        body = json.loads(self.raw(
            "GET", "/api/v1/cloud-service-record/current-package",
            "uid=141030191094&serviceType=2").partition(b"\r\n\r\n")[2])
        self.assertEqual(body["msg"], "查询成功")
        self.assertEqual(body["data"], {"uid": "141030191094",
                                        "packageStatus": "3",
                                        "expiredDuration": "",
                                        "packageInfo": []})

    def test_share_list_is_an_empty_array(self):
        body = json.loads(self.raw("GET", "/api/v1/share-list")
                          .partition(b"\r\n\r\n")[2])
        self.assertEqual(body["data"], [])

    def test_stun_addr_shape_matches_the_vendor(self):
        body = json.loads(self.raw("GET", "/api/v1/ipc/stun-addr/141030191094")
                          .partition(b"\r\n\r\n")[2])
        data = body["data"]
        self.assertEqual(sorted(data.keys()),
                         ["IpcPrivateIP", "IpcPrivatePort", "IpcPublicIP",
                          "IpcPublicPort", "seqNo", "updateTime"])

    def test_send_stun_addr_shape_matches_the_vendor(self):
        body = json.loads(self.raw(
            "GET", "/api/v1/ipc/send-stun-addr",
            "appId=x&uid=141030191094&publicIp=1.2.3.4&publicPort=1"
            "&privateIp=5.6.7.8&privatePort=2").partition(b"\r\n\r\n")[2])
        data = body["data"]
        self.assertEqual(sorted(data.keys()),
                         ["IpcPrivateIP", "IpcPrivatePort", "IpcPublicIP",
                          "IpcPublicPort", "receiveTs", "seqNo"])

    def test_send_stun_addr_remembers_the_viewers_address(self):
        """The viewer registers where it wants the media delivered; the
        camera is told that address over the signalling connection."""
        self.raw("GET", "/api/v1/ipc/send-stun-addr",
                 "appId=x&uid=141030191094&publicIp=177.221.120.116"
                 "&publicPort=64550&privateIp=192.168.18.30"
                 "&privatePort=55795")
        viewer = self.api.viewer_for("141030191094")
        self.assertEqual(viewer["privateIp"], "192.168.18.30")
        self.assertEqual(viewer["privatePort"], 55795)
        self.assertEqual(viewer["publicIp"], "177.221.120.116")
        self.assertEqual(viewer["publicPort"], 64550)

    def test_viewer_address_can_be_read_back(self):
        self.raw("GET", "/api/v1/ipc/send-stun-addr",
                 "appId=x&uid=141030191094&publicIp=1.2.3.4&publicPort=1"
                 "&privateIp=192.168.68.111&privatePort=40000")
        body = json.loads(self.raw("GET", "/api/v1/ipc/viewer/141030191094")
                          .partition(b"\r\n\r\n")[2])
        self.assertEqual(body["data"]["privateIp"], "192.168.68.111")
        self.assertEqual(body["data"]["privatePort"], 40000)


class HttpApiTests(unittest.TestCase):
    """The broker answers the vendor's API, so the camera and the bridge
    can both be pointed at it instead of at gps555.net."""

    def setUp(self):
        self.api = fake.HttpApi()
        self.api.add_camera("141030191094", "192.168.68.50", 34567)

    def call(self, method, path, query="", body=b""):
        raw = self.api.respond(method, path, query, body)
        self.assertIsNotNone(raw)
        head, _, payload = raw.partition(b"\r\n\r\n")
        return json.loads(payload)

    def test_device_list_carries_the_camera_row(self):
        body = self.call("GET", "/api/v1/ipc", "terminalFamilyId=3132031")
        self.assertEqual(body["code"], 200)
        self.assertEqual(body["msg"], "OK")
        rows = body["data"]["list"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["uid"], "141030191094")
        # The address bookkeeping fields are internal, not API fields.
        self.assertNotIn("_ip", rows[0])
        self.assertNotIn("_port", rows[0])

    def test_send_stun_addr_hands_back_the_camera_address(self):
        body = self.call("GET", "/api/v1/ipc/send-stun-addr",
                         "appId=x&uid=141030191094&publicIp=1.2.3.4"
                         "&publicPort=1&privateIp=5.6.7.8&privatePort=2")
        data = body["data"]
        self.assertEqual(data["IpcPrivateIP"], "192.168.68.50")
        self.assertEqual(data["IpcPrivatePort"], 34567)
        self.assertIn("seqNo", data)

    def test_stun_addr_is_answered_for_the_camera(self):
        body = self.call("GET", "/api/v1/ipc/stun-addr/141030191094")
        self.assertEqual(body["code"], 200)
        self.assertEqual(body["data"]["IpcPrivateIP"], "192.168.68.50")
        self.assertIn("updateTime", body["data"])

    def test_the_public_pair_is_the_cameras_own(self):
        # §14.1: the vendor's IpcPublic* is the CAMERA's STUN mapping,
        # not the viewer's. The camera reports it in its 0x0003.
        self.api.set_camera_public("141030191094", "186.233.129.211", 27719)
        body = self.call("GET", "/api/v1/ipc/stun-addr/141030191094")
        self.assertEqual(body["data"]["IpcPublicIP"], "186.233.129.211")
        self.assertEqual(body["data"]["IpcPublicPort"], 27719)
        # And the viewer's own address is not in the answer.
        self.assertNotEqual(body["data"]["IpcPublicIP"], "1.2.3.4")

    def test_the_registration_number_is_per_camera(self):
        # §14.1: each send-stun-addr takes the next number FOR THAT
        # CAMERA, and the camera echoes it back in its 0x0003. One
        # shared counter never shows the viewer's own number, and the
        # app waits for exactly that before it starts the view.
        uid = "141030191094"
        first = self.call("GET", "/api/v1/ipc/send-stun-addr",
                          f"uid={uid}&publicIp=1.2.3.4&publicPort=1"
                          f"&privateIp=5.6.7.8&privatePort=2")
        second = self.call("GET", "/api/v1/ipc/send-stun-addr",
                           f"uid={uid}&publicIp=1.2.3.4&publicPort=3"
                           f"&privateIp=5.6.7.8&privatePort=4")
        self.assertEqual(first["data"]["seqNo"] + 1,
                         second["data"]["seqNo"])
        # Polling must not move it on: the number belongs to the
        # registration, not to the poll.
        poll = self.call("GET", f"/api/v1/ipc/stun-addr/{uid}")
        self.assertEqual(poll["data"]["seqNo"], 0)
        # Another camera has its own counter.
        self.api.add_camera("140979857781", "192.168.68.51", 1)
        other = self.call("GET", "/api/v1/ipc/send-stun-addr",
                          "uid=140979857781&publicIp=1.2.3.4&publicPort=1"
                          "&privateIp=5.6.7.8&privatePort=2")
        self.assertEqual(other["data"]["seqNo"], 1)

    def test_the_reported_number_is_what_stun_addr_shows(self):
        # The camera answers a registration by echoing its number in the
        # 0x0003; only then does stun-addr show it.
        uid = "141030191094"
        self.call("GET", "/api/v1/ipc/send-stun-addr",
                  f"uid={uid}&publicIp=1.2.3.4&publicPort=1"
                  f"&privateIp=5.6.7.8&privatePort=2")
        self.api.set_reported_seq(uid, 1)
        body = self.call("GET", f"/api/v1/ipc/stun-addr/{uid}")
        self.assertEqual(body["data"]["seqNo"], 1)

    def test_notify_is_acknowledged(self):
        body = self.call("GET", "/api/v1/ipc/notify-live-event",
                         "appId=x&eventType=1&uid=141030191094")
        self.assertEqual(body["data"], "ok")

    def test_share_list_is_a_list_not_an_object(self):
        body = self.call("GET", "/api/v1/share-list")
        self.assertEqual(body["data"], [])

    def test_unknown_endpoint_is_a_404_body(self):
        body = self.call("GET", "/api/v1/nope")
        self.assertEqual(body["code"], 404)

    def test_a_camera_registers_itself_over_http(self):
        body = self.call("POST", "/api/v1/ipc", "",
                         b'{"uid":"140979857781","ip":"192.168.68.77",'
                         b'"port":9000}')
        self.assertEqual(body["code"], 200)
        rows = self.call("GET", "/api/v1/ipc", "terminalFamilyId=3132031")
        uids = {r["uid"] for r in rows["data"]["list"]}
        self.assertEqual(uids, {"141030191094", "140979857781"})
        stun = self.call("GET", "/api/v1/ipc/stun-addr/140979857781")
        self.assertEqual(stun["data"]["IpcPrivateIP"], "192.168.68.77")
        self.assertEqual(stun["data"]["IpcPrivatePort"], 9000)

    def test_a_poll_does_not_move_the_registration_number(self):
        # §14.1: the number belongs to a registration, not to a poll.
        # The old shared counter moved on every answer -- including the
        # poll -- so the viewer's own number never came back and the app
        # gave up and went to relay.
        first = self.call("GET", "/api/v1/ipc/stun-addr/141030191094")
        second = self.call("GET", "/api/v1/ipc/stun-addr/141030191094")
        self.assertEqual(second["data"]["seqNo"], first["data"]["seqNo"])

    def test_get_sig_ip_hands_the_app_this_brokers_address(self):
        # §17.5: the app saves `data` as icpSigAddress only when `code`
        # is 400, and that string is the BLE wifiConnect urlAndPort.
        self.api.sig_host = "192.168.68.114"
        self.api.sig_port = 8882
        body = self.call("GET", "/api/v1/ipc-function/get-sig-ip",
                         "supportLoadBalance=1")
        self.assertEqual(body["code"], 400)
        self.assertEqual(body["data"], "192.168.68.114:8882")

    def test_get_sig_ip_is_empty_when_signalling_is_off(self):
        self.api.sig_port = 0
        body = self.call("GET", "/api/v1/ipc-function/get-sig-ip")
        self.assertEqual(body["code"], 400)
        self.assertEqual(body["data"], "")

    def test_a_restart_serves_an_idle_media_state(self):
        # §17.1: the app will not open a camera whose mediaState is not
        # "0". A state left over from the previous run is not live.
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(Path(fh.name).unlink, missing_ok=True)
        json.dump({"141030191094": {"uid": "141030191094",
                                    "mediaState": "3"}}, fh)
        fh.close()
        api = fake.HttpApi(camera_file=fh.name)
        api.load_cameras()
        self.assertEqual(api.cameras["141030191094"]["mediaState"], "0")


class HttpApiServerTests(unittest.TestCase):
    """The API is reachable over the socket the bridge actually talks to."""

    def setUp(self):
        self.api = fake.HttpApi()
        self.api.add_camera("141030191094", "192.168.68.50", 34567)
        srv = fake.serve("http", ("127.0.0.1", 0), self.api)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        self.port = srv.server_address[1]

    def test_device_list_over_http(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"GET /api/v1/ipc?terminalFamilyId=3132031 HTTP/1.1\r\n"
                  b"Host: ipc.gps555.net\r\n\r\n")
        chunks = b""
        deadline = time.time() + 5
        while b"\r\n\r\n" not in chunks and time.time() < deadline:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks += chunk
        # keep reading until the body is complete
        while len(chunks.split(b"\r\n\r\n", 1)[1]) < int(
                chunks.split(b"Content-Length: ")[1].split(b"\r\n")[0]):
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks += chunk
        s.close()
        head, _, body = chunks.partition(b"\r\n\r\n")
        self.assertIn(b"200 OK", head)
        parsed = json.loads(body)
        self.assertEqual(parsed["data"]["list"][0]["uid"], "141030191094")


class ScriptedAttSocket:
    """A fake ATT peer: records what was sent, answers from a script.

    `script(pdu)` returns the PDUs to queue in reply to one sent PDU.
    recv() hands out one whole PDU per call, like a SOCK_SEQPACKET socket.
    """

    def __init__(self, script):
        self.script = script
        self.sent = []
        self.queue = []

    def bind(self, addr):
        pass

    def settimeout(self, t):
        pass

    def connect(self, addr):
        pass

    def close(self):
        pass

    def send(self, pdu):
        self.sent.append(bytes(pdu))
        self.queue.extend(self.script(bytes(pdu)))

    def recv(self, n):
        if not self.queue:
            raise socket.timeout()
        return self.queue.pop(0)[:n]


class AttTransportTests(unittest.TestCase):
    def test_read_returns_a_whole_multibyte_pdu(self):
        """L2CAP is SOCK_SEQPACKET: a short recv() discards the rest of the
        datagram, so a PDU read byte by byte arrives as its first byte."""
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        notify = bytes([0x1B, 0x08, 0x00]) + b"\x86\x86\x00\x15"
        b.send(notify)
        self.assertEqual(fake._att_read(a, 1.0), notify)

    def test_notification_before_response_is_not_the_response(self):
        notify = bytes([0x1B, 0x0A, 0x00, 0xAA, 0xBB])
        script = lambda pdu: [notify, b"\x13"]
        sock = ScriptedAttSocket(script)
        notes = []
        rsp = fake._att_request(sock, fake.ATT_WRITE_REQUEST,
                                b"\x08\x00data", 1.0, notes)
        self.assertEqual(rsp, b"\x13")
        self.assertEqual(notes, [(0x000A, b"\xaa\xbb")])

    def test_cameras_own_mtu_request_is_skipped_and_not_answered(self):
        """The camera sends `02 17 00` where a write response is expected.
        Taken as the response it hides the real `13`; answered with a 0x03
        it provokes `01 03 .. .. 06` (verified live), which then displaces
        the next write's response."""
        script = lambda pdu: [b"\x02\x17\x00", b"\x13"]
        sock = ScriptedAttSocket(script)
        rsp = fake._att_request(sock, fake.ATT_WRITE_REQUEST,
                                b"\x0b\x00\x01\x00", 1.0, [])
        self.assertEqual(rsp, b"\x13")
        self.assertEqual(len(sock.sent), 1)   # only our write; no reply

    def test_timeout_is_empty_not_an_error(self):
        sock = ScriptedAttSocket(lambda pdu: [])
        self.assertEqual(
            fake._att_request(sock, fake.ATT_WRITE_REQUEST, b"\x08\x00x",
                              0.05, []), b"")

    def _run_attempt(self, script):
        sock = ScriptedAttSocket(script)
        frame = fake.wifi_connect_frame("net", "pw", "10.0.0.1:8882", -180)
        chunks = fake.chunk20(frame)
        out = io.StringIO()
        with mock.patch.object(fake.socket, "socket", return_value=sock), \
                mock.patch.object(fake, "ATT_DRAIN_S", 0.2), \
                contextlib.redirect_stdout(out):
            result = fake._att_attempt("AA:BB:CC:DD:EE:FF", chunks)
        return result, sock, chunks, out.getvalue()

    def test_wire_bytes_use_little_endian_handles_and_two_byte_cccd(self):
        def script(pdu):
            return [b"\x03\x17\x00"] if pdu[0] == 0x02 else [b"\x13"]
        result, sock, chunks, _ = self._run_attempt(script)
        self.assertEqual(result, "sent")
        # Exchange MTU: opcode + little-endian MTU.
        self.assertEqual(sock.sent[0], bytes([0x02, 247, 0x00]))
        # CCCD 0x000b enabled with the 2-byte value 01 00 -- handle bytes
        # little-endian, no stray value bytes.
        self.assertEqual(sock.sent[1], bytes([0x12, 0x0B, 0x00, 0x01, 0x00]))
        # Every chunk goes to handle 0x0008, in order, unmodified.
        writes = sock.sent[2:]
        self.assertEqual(writes,
                         [bytes([0x12, 0x08, 0x00]) + c for c in chunks])

    def test_reply_split_across_notifications_is_reassembled(self):
        reply = bytes.fromhex(
            "8686" "0015" "0001" "0010"
            "8001" "00" "0c" "303132333435363738394142"
            "8b" "0d")
        n1 = bytes([0x1B, 0x0A, 0x00]) + reply[:20]
        n2 = bytes([0x1B, 0x0A, 0x00]) + reply[20:]
        state = {"writes": 0}

        def script(pdu):
            if pdu[0] == 0x02:
                return [b"\x03\x17\x00"]
            if pdu[0] == 0x12 and pdu[1:3] == b"\x08\x00":
                state["writes"] += 1
                if state["writes"] == 2:   # last chunk: the frame is 34 B
                    return [b"\x13", n1, n2]
            return [b"\x13"]
        result, _, _, out = self._run_attempt(script)
        self.assertEqual(result, "sent")
        self.assertIn("'uid': '0123456789AB'", out)

    def test_error_response_to_a_write_aborts_the_attempt(self):
        def script(pdu):
            if pdu[0] == 0x02:
                return [b"\x03\x17\x00"]
            if pdu[0] == 0x12 and pdu[1:3] == b"\x08\x00":
                return [bytes([0x01, 0x12, 0x08, 0x00, 0x03])]
            return [b"\x13"]
        result, _, _, _ = self._run_attempt(script)
        self.assertEqual(result, "write 0 rejected")



if __name__ == "__main__":
    unittest.main()

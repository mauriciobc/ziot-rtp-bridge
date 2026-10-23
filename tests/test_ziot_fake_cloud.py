"""Fake-cloud regressions: the listener farm logs what the camera sends."""
import contextlib
import io
import json
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
        "remoteServiceEndTime", "unReadEventCount",
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

    def test_seqno_advances_between_answers(self):
        first = self.call("GET", "/api/v1/ipc/stun-addr/141030191094")
        second = self.call("GET", "/api/v1/ipc/stun-addr/141030191094")
        self.assertGreater(second["data"]["seqNo"], first["data"]["seqNo"])


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

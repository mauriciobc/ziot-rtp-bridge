"""Behavior regressions for direct/relay RTP acceptance and startup validation."""
import json
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ziot_rtp_bridge as bridge


CAM_UID = "141030191094"        # -> ssrc 0x30191094, per the README mapping
CAM_SSRC = 0x30191094


def rtp_packet(pt, payload, ts=1000, seq=1, marker=False, b0=0x80, ssrc=CAM_SSRC):
    b1 = (0x80 if marker else 0x00) | (pt & 0x7F)
    return struct.pack("!BBHII", b0, b1, seq, ts, ssrc) + payload


def jpeg_payload(frag_off=0, jtype=0, q=0, width=1, height=1,
                 data=b"\x00\x01\x02\x03", quant=None, dri=None):
    hdr = bytes([0,
                 (frag_off >> 16) & 0xFF, (frag_off >> 8) & 0xFF, frag_off & 0xFF,
                 jtype, q, width, height])
    if dri is not None:
        hdr += struct.pack(">H", dri) + b"\x00\x00"
    if quant is not None and frag_off == 0:
        hdr += b"\x00\x00" + struct.pack(">H", len(quant)) + quant
    return hdr + data


def make_camera(uid=CAM_UID):
    api = mock.MagicMock()
    return bridge.ZiotCamera(api, {"uid": uid}, "127.0.0.1")


def bind_loopback():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    return sock


class DirectPacketTests(unittest.TestCase):
    def setUp(self):
        self.cam = make_camera()
        self.sock = bind_loopback()
        self.peer = bind_loopback()
        self.cam.sock = self.sock
        self.cam.addr = ("127.0.0.1", self.peer.getsockname()[1])
        self.asm = bridge.RtpJpegReassembler(lambda jpeg: None)
        self.addCleanup(self.sock.close)
        self.addCleanup(self.peer.close)

    def source(self):
        return ("127.0.0.1", self.peer.getsockname()[1])

    def test_matching_audio_advances_liveness_and_publishes(self):
        with self.cam.audio.subscribe() as q:
            before = self.cam._last_rx
            pkt = rtp_packet(0, b"\x11" * 160, ts=11)
            ok = self.cam._handle_direct_packet(
                self.sock, self.source(), pkt, self.asm)
            self.assertTrue(ok)
            self.assertEqual(self.cam.stats["audio_pkts"], 1)
            self.assertGreater(self.cam._last_rx, before)
            got = q.get(timeout=2)
            self.assertEqual(got, bridge.ulaw_to_pcm16(b"\x11" * 160))

    def test_retired_socket_changes_nothing(self):
        good = rtp_packet(0, b"\x22" * 160, ts=21)
        self.assertTrue(self.cam._handle_direct_packet(
            self.sock, self.source(), good, self.asm))
        audio_pkts = self.cam.stats["audio_pkts"]
        last_rx = self.cam._last_rx
        with self.cam.audio.subscribe() as q:
            retired = bind_loopback()
            self.addCleanup(retired.close)
            self.assertFalse(self.cam._handle_direct_packet(
                retired, self.source(), good, self.asm))
            self.assertEqual(self.cam.stats["audio_pkts"], audio_pkts)
            self.assertEqual(self.cam._last_rx, last_rx)
            self.assertTrue(q.empty())

    def test_foreign_ssrc_changes_nothing(self):
        last_rx = self.cam._last_rx
        with self.cam.audio.subscribe() as q:
            for pkt in (rtp_packet(0, b"\x33" * 160, ts=41, ssrc=CAM_SSRC + 1),
                        rtp_packet(26, jpeg_payload(), ts=42, ssrc=0)):
                self.assertFalse(self.cam._handle_direct_packet(
                    self.sock, self.source(), pkt, self.asm))
            self.assertEqual(self.cam.stats["audio_pkts"], 0)
            self.assertEqual(self.cam.stats["foreign_ssrc"], 2)
            self.assertEqual(self.cam._last_rx, last_rx)
            self.assertTrue(q.empty())

    def test_media_from_another_address_is_still_accepted(self):
        """The address the broker reports and the one the camera sends from
        routinely disagree -- a per-session rebind, a LAN wider than /24, or
        symmetric NAT. Dropping on that mismatch strands a working stream."""
        moved = ("127.0.0.2", self.peer.getsockname()[1] + 1)
        before = self.cam._last_rx
        with self.cam.audio.subscribe() as q:
            self.assertTrue(self.cam._handle_direct_packet(
                self.sock, moved, rtp_packet(0, b"\x44" * 160, ts=51), self.asm))
            self.assertEqual(self.cam.stats["audio_pkts"], 1)
            self.assertGreater(self.cam._last_rx, before)
            self.assertEqual(q.get(timeout=2),
                             bridge.ulaw_to_pcm16(b"\x44" * 160))
        self.assertEqual(self.cam._media_source, moved)

    def test_resolve_moving_the_endpoint_does_not_stop_media(self):
        good = rtp_packet(0, b"\x55" * 160, ts=61)
        self.assertTrue(self.cam._handle_direct_packet(
            self.sock, self.source(), good, self.asm))
        self.cam.addr = ("10.0.0.9", 12345)     # what a stale _resolve installs
        before = self.cam._last_rx
        self.assertTrue(self.cam._handle_direct_packet(
            self.sock, self.source(), good, self.asm))
        self.assertGreaterEqual(self.cam._last_rx, before)
        self.assertEqual(self.cam.stats["audio_pkts"], 2)

    def test_uid_without_an_ssrc_accepts_any_ssrc(self):
        cam = make_camera(uid="cam1")
        self.assertIsNone(cam._ssrc)
        cam.sock = self.sock
        cam.addr = self.cam.addr
        self.assertTrue(cam._handle_direct_packet(
            self.sock, self.source(),
            rtp_packet(0, b"\x66" * 160, ts=71, ssrc=0xDEADBEEF), self.asm))
        self.assertEqual(cam.stats["audio_pkts"], 1)
        self.assertEqual(cam.stats["foreign_ssrc"], 0)

    def test_valid_video_fragment_advances_liveness_without_frame(self):
        emitted = []
        asm = bridge.RtpJpegReassembler(emitted.append)
        before = self.cam._last_rx
        pkt = rtp_packet(26, jpeg_payload(frag_off=0, q=0), ts=31)
        self.assertTrue(self.cam._handle_direct_packet(
            self.sock, self.source(), pkt, asm))
        self.assertEqual(emitted, [])
        self.assertGreater(self.cam._last_rx, before)

    def test_complete_frame_still_emits(self):
        emitted = []
        asm = bridge.RtpJpegReassembler(emitted.append)
        ts = 32
        first = rtp_packet(
            26, jpeg_payload(frag_off=0, q=128, quant=b"\x10" * 64,
                             data=b"AA"), ts=ts)
        second = rtp_packet(
            26, jpeg_payload(frag_off=2, q=0, data=b"BB"), ts=ts, marker=True)
        self.assertTrue(asm.feed(first))
        self.assertEqual(emitted, [])
        self.assertTrue(asm.feed(second))
        self.assertEqual(len(emitted), 1)
        self.assertTrue(emitted[0].startswith(b"\xff\xd8"))
        self.assertTrue(emitted[0].endswith(b"\xff\xd9"))

    def test_structural_rejections_leave_state_untouched(self):
        cases = [
            # truncated restart-marker header (jtype >= 64, fewer than 12 bytes)
            rtp_packet(26, jpeg_payload(jtype=70)[:10], ts=41),
            # q >= 128 first fragment without its quantization header
            rtp_packet(26, bytes([0, 0, 0, 0, 0, 128, 1, 1]), ts=42),
            # declared quantization table overruns the payload
            rtp_packet(26, bytes([0, 0, 0, 0, 0, 128, 1, 1])
                       + b"\x00\x00\x00\x40" + b"\x10" * 4, ts=43),
            # malformed padded PCMU (pad count 0 is invalid per RFC 3550)
            rtp_packet(0, b"\x01\x02\x00", ts=44, b0=0xA0),
            # malformed padded PCMU (pad count longer than the payload)
            rtp_packet(0, b"\x01\x02\x05", ts=45, b0=0xA0),
            # empty PCMU payload
            rtp_packet(0, b"", ts=46),
            # short packet and unsupported payload type
            b"\x80\x1a\x00\x01",
            rtp_packet(8, b"\x03" * 160, ts=47),
        ]
        for pkt in cases:
            with self.subTest(pkt=pkt[:16]):
                emitted = []
                asm = bridge.RtpJpegReassembler(emitted.append)
                before_audio = self.cam.stats["audio_pkts"]
                before_rx = self.cam._last_rx
                with self.cam.audio.subscribe() as q:
                    self.assertFalse(self.cam._handle_direct_packet(
                        self.sock, self.source(), pkt, asm))
                    self.assertEqual(emitted, [])
                    self.assertEqual(self.cam.stats["audio_pkts"], before_audio)
                    self.assertEqual(self.cam._last_rx, before_rx)
                    self.assertTrue(q.empty())
                self.assertEqual(asm._frags, {})
                self.assertEqual(asm._meta, {})

    def test_feed_rejects_without_mutation(self):
        asm = bridge.RtpJpegReassembler(lambda jpeg: None)
        bad = rtp_packet(26, jpeg_payload(jtype=70)[:10], ts=51)
        self.assertFalse(asm.feed(bad))
        self.assertEqual(asm._frags, {})
        self.assertEqual(asm._meta, {})
        self.assertFalse(asm.feed(b"short"))

    def test_receiver_survives_callback_failure_over_loopback(self):
        rx = bind_loopback()
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.bind(("127.0.0.1", 0))
        self.addCleanup(rx.close)
        self.addCleanup(tx.close)
        cam = make_camera()
        cam.sock = rx
        cam.addr = ("127.0.0.1", tx.getsockname()[1])
        original_publish = cam.audio.publish
        calls = {"n": 0}

        def flaky(pcm):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return original_publish(pcm)

        with mock.patch.object(cam.audio, "publish", side_effect=flaky):
            with cam.audio.subscribe() as q:
                worker = threading.Thread(target=cam._receive, daemon=True)
                worker.start()
                try:
                    first = rtp_packet(0, b"\x33" * 40, ts=61)
                    second = rtp_packet(0, b"\x34" * 40, ts=62)
                    tx.sendto(first, ("127.0.0.1", rx.getsockname()[1]))
                    deadline = time.monotonic() + 5
                    while calls["n"] < 1 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertGreaterEqual(calls["n"], 1)
                    tx.sendto(second, ("127.0.0.1", rx.getsockname()[1]))
                    got = q.get(timeout=5)
                    self.assertEqual(got, bridge.ulaw_to_pcm16(b"\x34" * 40))
                    self.assertGreater(cam._last_rx, 0.0)
                    self.assertEqual(cam.stats["audio_pkts"], 2)
                    self.assertEqual(cam.stats["rx_errors"], 1)
                    self.assertEqual(cam.stats["rx_datagrams"], 2)
                finally:
                    cam._stop.set()
                    worker.join(timeout=5)
        self.assertFalse(worker.is_alive())


class RxDatagramTests(unittest.TestCase):
    """Counted before any filter runs, so an empty /health can distinguish
    "the camera is silent" from "packets arrive and we drop them all"."""

    def receive(self, packets):
        rx = bind_loopback()
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.bind(("127.0.0.1", 0))
        self.addCleanup(rx.close)
        self.addCleanup(tx.close)
        cam = make_camera()
        cam.sock = rx
        cam.addr = ("127.0.0.1", tx.getsockname()[1])
        worker = threading.Thread(target=cam._receive, daemon=True)
        worker.start()
        try:
            for pkt in packets:
                tx.sendto(pkt, ("127.0.0.1", rx.getsockname()[1]))
            deadline = time.monotonic() + 5
            while (cam.stats["rx_datagrams"] < len(packets)
                   and time.monotonic() < deadline):
                time.sleep(0.01)
        finally:
            cam._stop.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        return cam

    def test_rejected_traffic_still_shows_up_as_arrivals(self):
        cam = self.receive([
            rtp_packet(0, b"\x11" * 160, ts=1, ssrc=CAM_SSRC + 1),  # foreign
            rtp_packet(0, b"\x22" * 160, ts=2, ssrc=CAM_SSRC + 1),  # foreign
            b"runt",                                                # under 12B
            rtp_packet(8, b"\x33" * 160, ts=3),                     # unknown pt
        ])
        self.assertEqual(cam.stats["rx_datagrams"], 4)
        self.assertEqual(cam.stats["foreign_ssrc"], 2)
        self.assertEqual(cam.stats["audio_pkts"], 0)
        self.assertEqual(cam.stats["frames"], 0)
        self.assertEqual(cam._last_rx, 0.0)

    def test_accepted_traffic_counts_once_each(self):
        cam = self.receive([rtp_packet(0, b"\x44" * 160, ts=i)
                            for i in range(3)])
        self.assertEqual(cam.stats["rx_datagrams"], 3)
        self.assertEqual(cam.stats["audio_pkts"], 3)
        self.assertEqual(cam.stats["foreign_ssrc"], 0)

    def test_a_silent_camera_reads_differently_from_a_rejected_one(self):
        silent = self.receive([])
        self.assertEqual(silent.stats["rx_datagrams"], 0)
        rejected = self.receive([rtp_packet(0, b"\x55" * 160, ts=9,
                                            ssrc=CAM_SSRC + 1)])
        self.assertEqual(rejected.stats["rx_datagrams"], 1)
        # Both look identical on every other counter; this is the one that
        # tells them apart.
        for cam in (silent, rejected):
            self.assertEqual(cam.stats["frames"], 0)
            self.assertEqual(cam.stats["audio_pkts"], 0)


class RelayConsumeTests(unittest.TestCase):
    def make_stream(self):
        on_frame = mock.MagicMock()
        on_audio = mock.MagicMock()
        on_rx = mock.MagicMock()
        stream = bridge.RelayStream(
            "rtsp://example/live/x", on_frame, on_audio, on_rx,
            threading.Event(), "test")
        return stream, on_frame, on_audio, on_rx

    def run_frame(self, stream, channel, packet):
        hdr = b"$" + bytes([channel]) + struct.pack("!H", len(packet))
        with mock.patch.object(stream, "_read_exact",
                               side_effect=[hdr, packet]) as _:
            stream._consume_interleaved()

    def test_accepted_video_and_audio_fire_on_rx_once(self):
        stream, on_frame, on_audio, on_rx = self.make_stream()
        stream._channels = {0: "video", 1: "audio"}

        video = rtp_packet(26, jpeg_payload(frag_off=0, q=0), ts=71)
        self.run_frame(stream, 0, video)
        on_rx.assert_called_once_with()
        on_rx.reset_mock()

        audio = rtp_packet(0, b"\x44" * 40, ts=72)
        self.run_frame(stream, 1, audio)
        on_audio.assert_called_once()
        on_rx.assert_called_once_with()

    def test_malformed_media_never_refreshes_liveness(self):
        stream, on_frame, on_audio, on_rx = self.make_stream()
        stream._channels = {0: "video", 1: "audio"}
        bad_cases = [
            (0, rtp_packet(26, jpeg_payload(jtype=70)[:10], ts=81)),
            (1, rtp_packet(0, b"", ts=82)),
            (1, rtp_packet(0, b"\x01\x02\x00", ts=83, b0=0xA0)),
            (9, rtp_packet(26, jpeg_payload(), ts=84)),
            (0, b"\x80\x1a\x00\x01"),
        ]
        for channel, packet in bad_cases:
            with self.subTest(channel=channel):
                on_rx.reset_mock()
                on_audio.reset_mock()
                self.run_frame(stream, channel, packet)
                on_rx.assert_not_called()
                on_audio.assert_not_called()

    def test_relay_audio_callback_failure_skips_liveness(self):
        stream, on_frame, on_audio, on_rx = self.make_stream()
        stream._channels = {1: "audio"}
        on_audio.side_effect = RuntimeError("boom")
        audio = rtp_packet(0, b"\x45" * 40, ts=91)
        with self.assertRaises(RuntimeError):
            self.run_frame(stream, 1, audio)
        on_rx.assert_not_called()


class PunchIntervalTests(unittest.TestCase):
    def setUp(self):
        self.old_interval = bridge.PUNCH_INTERVAL
        self.old_argv = sys.argv[:]
        self.addCleanup(setattr, bridge, "PUNCH_INTERVAL", self.old_interval)
        self.addCleanup(sys.argv.__setitem__, slice(None), self.old_argv)

    def run_main(self, config):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            json.dump(config, f)
            path = f.name
        self.addCleanup(Path(path).unlink, missing_ok=True)
        sys.argv = ["ziot_rtp_bridge.py", "--config", path]
        return path

    def test_invalid_intervals_clamp_and_keep_booting(self):
        """A bad interval must not strand the fleet: exiting here puts every
        camera dark in a --restart unless-stopped loop."""
        for value in (0, -1, 2.0, 2.5, "fast", "nan", "inf",
                      float("nan"), float("inf"), None, [], {}):
            with self.subTest(value=value):
                bridge.PUNCH_INTERVAL = 1.0
                self.run_main({"token": "t", "user_id": 1,
                               "punch_interval": value})
                with mock.patch.object(bridge, "GPS555") as gps:
                    gps.return_value.list_cameras.return_value = []
                    with self.assertLogs(bridge.log, "ERROR") as logs:
                        bridge.main()
                    self.assertEqual(bridge.PUNCH_INTERVAL, 1.0)
                    gps.assert_called_once_with("t")
                self.assertTrue(
                    any("punch_interval" in line for line in logs.output),
                    logs.output)

    def test_valid_interval_passes_validation(self):
        self.run_main({"token": "t", "user_id": 1, "punch_interval": 1.999})
        with mock.patch.object(bridge, "GPS555") as gps:
            gps.return_value.list_cameras.return_value = []
            bridge.main()
            self.assertAlmostEqual(bridge.PUNCH_INTERVAL, 1.999)
            gps.assert_called_once_with("t")
            gps.return_value.list_cameras.assert_called_once_with(1)


class UidSsrcTests(unittest.TestCase):
    def test_last_eight_digits_read_as_hex(self):
        self.assertEqual(bridge.uid_ssrc("141030191094"), 0x30191094)
        self.assertEqual(bridge.uid_ssrc("30191094"), 0x30191094)

    def test_uids_the_mapping_does_not_cover(self):
        for uid in ("cam1", "", None, "1234567", "12345678a", "\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668"):
            with self.subTest(uid=uid):
                self.assertIsNone(bridge.uid_ssrc(uid))


class ResolveRateLimitTests(unittest.TestCase):
    """get_stun_addr blocks for up to 10s and the punch loop asks for one on
    every tick without media, so the lookup needs a floor."""

    def setUp(self):
        self.cam = make_camera()
        self.cam._fetch_endpoint = mock.Mock(
            return_value=("10.0.0.5", 9000, "private"))

    def test_first_resolve_installs_the_address(self):
        self.cam._resolve()
        self.assertEqual(self.cam.addr, ("10.0.0.5", 9000))
        self.assertEqual(self.cam._fetch_endpoint.call_count, 1)

    def test_second_resolve_inside_the_floor_is_a_no_op(self):
        self.cam._resolve()
        self.cam._fetch_endpoint.return_value = ("10.0.0.6", 9001, "private")
        self.cam._resolve()
        self.assertEqual(self.cam._fetch_endpoint.call_count, 1)
        self.assertEqual(self.cam.addr, ("10.0.0.5", 9000))

    def test_resolve_runs_again_once_the_floor_has_passed(self):
        self.cam._resolve()
        self.cam._last_resolve -= bridge.RESOLVE_MIN_INTERVAL
        self.cam._fetch_endpoint.return_value = ("10.0.0.6", 9001, "private")
        self.cam._resolve()
        self.assertEqual(self.cam._fetch_endpoint.call_count, 2)
        self.assertEqual(self.cam.addr, ("10.0.0.6", 9001))

    def test_a_failed_lookup_still_holds_the_floor(self):
        self.cam._fetch_endpoint.side_effect = OSError("stun down")
        self.cam._resolve()
        self.cam._resolve()
        self.assertEqual(self.cam._fetch_endpoint.call_count, 1)


class LogThrottleTests(unittest.TestCase):
    """The two per-packet log sites fire either once or at frame rate, so a
    traceback per packet would bury every other line the bridge writes."""

    def setUp(self):
        self.cam = make_camera()

    def test_repeats_within_the_interval_are_suppressed(self):
        with self.assertLogs(bridge.log, "ERROR") as logs:
            for i in range(50):
                self.cam._log_throttled("rx_error", 40, "boom %d", i)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("boom 0", logs.output[0])

    def test_the_interval_lets_a_later_line_through(self):
        with self.assertLogs(bridge.log, "ERROR") as logs:
            self.cam._log_throttled("rx_error", 40, "boom 0")
            self.cam._log_throttle["rx_error"] -= bridge.LOG_THROTTLE_INTERVAL
            self.cam._log_throttled("rx_error", 40, "boom 1")
        self.assertEqual(len(logs.output), 2)

    def test_keys_are_throttled_independently(self):
        with self.assertLogs(bridge.log, "WARNING") as logs:
            self.cam._log_throttled("rx_error", 40, "boom")
            self.cam._log_throttled("foreign_ssrc", 30, "stranger")
        self.assertEqual(len(logs.output), 2)

    def test_a_foreign_ssrc_flood_logs_once(self):
        sock = bind_loopback()
        self.addCleanup(sock.close)
        self.cam.sock = sock
        self.cam.addr = ("127.0.0.1", 9)
        asm = bridge.RtpJpegReassembler(lambda jpeg: None)
        with self.assertLogs(bridge.log, "WARNING") as logs:
            for i in range(30):
                self.assertFalse(self.cam._handle_direct_packet(
                    sock, self.cam.addr,
                    rtp_packet(0, b"\x77" * 160, ts=i, ssrc=CAM_SSRC + 1), asm))
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(self.cam.stats["foreign_ssrc"], 30)


def http_error(code):
    return urllib.error.HTTPError("https://cloud/v1/ipc", code,
                                  "rejected", {}, None)


class AuthFailureTests(unittest.TestCase):
    """A rejected token is a config error wearing a network error's clothes;
    a cloud outage is the thing retrying actually fixes."""

    def test_rejections_that_retrying_cannot_fix(self):
        for e in (http_error(401), http_error(403),
                  bridge.CloudError(401, "Erro comum"),
                  bridge.CloudError(403, "")):
            with self.subTest(e=str(e)):
                self.assertTrue(bridge.is_auth_failure(e))

    def test_everything_else_is_worth_retrying(self):
        for e in (http_error(500), http_error(502), http_error(429),
                  bridge.CloudError(500, "server blew up"),
                  urllib.error.URLError("connection refused"),
                  socket.timeout("timed out"), KeyError("data"),
                  OSError("network unreachable")):
            with self.subTest(e=type(e).__name__):
                self.assertFalse(bridge.is_auth_failure(e))

    def test_the_cloud_reports_rejection_in_the_body_not_the_status(self):
        """Observed live: a bogus token gets 200 OK with {"code": 401}. The
        HTTP status says nothing, so the body is the only signal."""
        self.assertEqual(bridge.cloud_error_code({"code": 401, "msg": "x"}), 401)
        self.assertEqual(bridge.cloud_error_code({"code": "401"}), 401)

    def test_a_response_carrying_data_is_never_an_error(self):
        for body in ({"data": {"list": []}},
                     {"code": 401, "data": {"list": []}},
                     {"data": None}):
            with self.subTest(body=body):
                self.assertIsNone(bridge.cloud_error_code(body))

    def test_unreadable_codes_are_not_invented(self):
        for body in ({}, {"code": None}, {"code": "nope"}, [], None):
            with self.subTest(body=body):
                self.assertIsNone(bridge.cloud_error_code(body))

    def test_list_cameras_raises_what_the_cloud_said(self):
        api = bridge.GPS555("bogus")
        with mock.patch.object(api, "_get",
                               return_value={"code": 401, "msg": "Erro comum"}):
            with self.assertRaises(bridge.CloudError) as cm:
                api.list_cameras(1)
        self.assertEqual(cm.exception.code, 401)
        self.assertIn("Erro comum", str(cm.exception))
        self.assertTrue(bridge.is_auth_failure(cm.exception))

    def test_list_cameras_still_returns_the_list(self):
        api = bridge.GPS555("t")
        rows = [{"uid": CAM_UID}]
        with mock.patch.object(api, "_get",
                               return_value={"data": {"list": rows}}):
            self.assertEqual(api.list_cameras(1), rows)


class BootStateTests(unittest.TestCase):
    def test_starts_out_starting_with_nothing_else_to_say(self):
        self.assertEqual(bridge.BootState().snapshot(), {"phase": "starting"})

    def test_detail_and_attempts_appear_only_once_set(self):
        state = bridge.BootState()
        state.set("retrying", "device list unavailable: timed out", 3)
        self.assertEqual(state.snapshot(), {
            "phase": "retrying",
            "detail": "device list unavailable: timed out",
            "attempts": 3,
        })
        state.set("ready", None)
        self.assertEqual(state.snapshot(), {"phase": "ready"})


class HealthEndpointTests(unittest.TestCase):
    """The watchdog reads this payload, so its shape is a contract."""

    def serve(self, cameras, boot, lock=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     bridge.make_handler(
                                         cameras, boot,
                                         lock or threading.Lock()))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=5) as r:
            return json.loads(r.read())

    def test_health_answers_before_any_camera_exists(self):
        boot = bridge.BootState()
        boot.set("retrying", "device list unavailable: timed out", 3)
        data = self.serve({}, boot)
        # "error" belongs to the watchdog, for "could not reach the bridge".
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(data["cameras"], [])
        self.assertEqual(data["boot"]["phase"], "retrying")
        self.assertEqual(data["boot"]["attempts"], 3)
        self.assertIn("timed out", data["boot"]["detail"])

    def test_a_ready_bridge_says_so(self):
        boot = bridge.BootState()
        boot.set("ready", None)
        data = self.serve({}, boot)
        self.assertEqual(data["boot"], {"phase": "ready"})


class BootResilienceTests(unittest.TestCase):
    def setUp(self):
        self.old_argv = sys.argv[:]
        self.old_interval = bridge.PUNCH_INTERVAL
        self.addCleanup(sys.argv.__setitem__, slice(None), self.old_argv)
        self.addCleanup(setattr, bridge, "PUNCH_INTERVAL", self.old_interval)
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            json.dump({"token": "t", "user_id": 1}, f)
            self.path = f.name
        self.addCleanup(Path(self.path).unlink, missing_ok=True)
        sys.argv = ["ziot_rtp_bridge.py", "--config", self.path]

    def test_a_rejected_token_at_startup_exits_before_binding(self):
        """Nothing is serving yet and no token un-expires itself, so this is
        fatal in the same sense as an unreadable config file."""
        with mock.patch.object(bridge, "GPS555") as gps, \
                mock.patch.object(bridge, "ThreadingHTTPServer") as server:
            gps.return_value.list_cameras.side_effect = \
                bridge.CloudError(401, "Erro comum")
            with self.assertLogs(bridge.log, "ERROR") as logs:
                with self.assertRaises(SystemExit) as cm:
                    bridge.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("rejected the token", "\n".join(logs.output))
        server.assert_not_called()

    def test_a_cloud_outage_at_startup_serves_health_anyway(self):
        """Exiting instead is the same retry loop with every camera dark in
        between and no /health answering to say why."""
        with mock.patch.object(bridge, "GPS555") as gps, \
                mock.patch.object(bridge, "ThreadingHTTPServer") as server:
            gps.return_value.list_cameras.side_effect = \
                urllib.error.URLError("connection refused")
            with self.assertLogs(bridge.log, "WARNING"):
                bridge.main()          # returns once serve_forever does
        server.assert_called_once()
        served = server.call_args[0]
        self.assertEqual(served[0], ("0.0.0.0", 8085))
        server.return_value.serve_forever.assert_called_once()

    def test_an_unreachable_broker_falls_back_to_the_default_route(self):
        """get_stun_addr was the other unguarded cloud call in the boot path."""
        with mock.patch.object(bridge, "GPS555") as gps, \
                mock.patch.object(bridge, "ThreadingHTTPServer") as server, \
                mock.patch.object(bridge, "ZiotCamera") as cam, \
                mock.patch.object(bridge, "local_ip_for",
                                  return_value="192.168.1.5") as local_ip:
            gps.return_value.list_cameras.return_value = [{"uid": CAM_UID}]
            gps.return_value.get_stun_addr.side_effect = socket.timeout("nope")
            cam.return_value.start.return_value = True
            with self.assertLogs(bridge.log, "WARNING") as logs:
                bridge.main()
        server.assert_called_once()
        local_ip.assert_called_with("8.8.8.8")
        self.assertIn("falling back to the default route",
                      "\n".join(logs.output))


class AllowListTests(unittest.TestCase):
    """The cameras allow-list must filter the device list at startup."""

    def setUp(self):
        self.old_argv = sys.argv[:]
        self.addCleanup(sys.argv.__setitem__, slice(None), self.old_argv)
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            json.dump({"token": "t", "user_id": 1, "cameras": ["AAA"]}, f)
            self.path = f.name
        self.addCleanup(Path(self.path).unlink, missing_ok=True)
        sys.argv = ["ziot_rtp_bridge.py", "--config", self.path,
                    "--bind-ip", "127.0.0.1"]

    def test_only_listed_cameras_boot(self):
        roster = [{"uid": "AAA"}, {"uid": "BBB"}, {"uid": "CCC"}]
        with mock.patch.object(bridge, "GPS555") as gps, \
                mock.patch.object(bridge, "ThreadingHTTPServer") as server, \
                mock.patch.object(bridge, "ZiotCamera") as cam:
            gps.return_value.list_cameras.return_value = roster
            cam.return_value.start.return_value = True
            bridge.main()
        self.assertEqual(cam.call_count, 1)
        rec = cam.call_args[0][1]
        self.assertEqual(rec["uid"], "AAA")
        server.assert_called_once()

class EndpointParseTests(unittest.TestCase):
    """Unsendable endpoints must fail at boot, not punch silence."""

    def test_valid_map_parses(self):
        self.assertEqual(
            bridge.parse_static_endpoints(
                {"A": "192.168.18.75:52901", "B": "10.0.0.5:9"}),
            {"A": ("192.168.18.75", 52901), "B": ("10.0.0.5", 9)})

    def test_unsendable_values_raise(self):
        for bad in ("52901",                    # bare port -> 0.0.0.0
                    "1.2.3.4:99999",            # sendto OverflowError
                    "1.2.3.4:-1",
                    "1.2.3.4:0",
                    "not-an-endpoint",
                    "1.2.3.4:notaport",
                    "[fe80::1]:5000"):          # AF_INET socket, never sends
            with self.subTest(ep=bad):
                with self.assertRaises(ValueError):
                    bridge.parse_static_endpoints({"A": bad})

class ConfigLoadTests(unittest.TestCase):
    def setUp(self):
        self.old_argv = sys.argv[:]
        self.addCleanup(sys.argv.__setitem__, slice(None), self.old_argv)

    def test_missing_config_names_the_mount(self):
        sys.argv = ["ziot_rtp_bridge.py", "--config",
                    "/nonexistent/dir/ziot_config.json"]
        with mock.patch.object(bridge, "GPS555") as gps:
            with self.assertLogs(bridge.log, "ERROR") as logs:
                with self.assertRaises(SystemExit) as cm:
                    bridge.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("-v /host/path/ziot_config.json", "\n".join(logs.output))
        gps.assert_not_called()

    def run_main_on(self, path):
        sys.argv = ["ziot_rtp_bridge.py", "--config", path]
        with mock.patch.object(bridge, "GPS555") as gps:
            with self.assertLogs(bridge.log, "ERROR") as logs:
                with self.assertRaises(SystemExit) as cm:
                    bridge.main()
        gps.assert_not_called()
        return cm.exception.code, "\n".join(logs.output)

    def write_config(self, data, mode="w"):
        with tempfile.NamedTemporaryFile(mode, suffix=".json",
                                         delete=False) as f:
            f.write(data)
            path = f.name
        self.addCleanup(Path(path).unlink, missing_ok=True)
        return path

    def test_malformed_config_blames_the_file_not_the_mount(self):
        """The file was read, so the mount is already right. Pointing at it
        sends whoever is reading the logs to check the wrong thing."""
        path = self.write_config("{not valid json")
        code, out = self.run_main_on(path)
        self.assertEqual(code, 2)
        self.assertIn(path, out)
        self.assertIn("is not valid JSON", out)
        self.assertNotIn("-v /host/path/ziot_config.json", out)

    def test_malformed_config_keeps_the_parser_position(self):
        path = self.write_config('{"token": "t",\n')
        code, out = self.run_main_on(path)
        self.assertEqual(code, 2)
        self.assertRegex(out, r"line \d+ column \d+")

    def test_non_utf8_config_blames_the_file_not_the_mount(self):
        path = self.write_config(b'{"token": "\xff\xfe not utf-8"}', mode="wb")
        code, out = self.run_main_on(path)
        self.assertEqual(code, 2)
        self.assertIn(path, out)
        self.assertIn("is not valid JSON", out)
        self.assertNotIn("-v /host/path/ziot_config.json", out)

    def test_an_unreadable_config_still_names_the_mount(self):
        code, out = self.run_main_on("/nonexistent/dir/ziot_config.json")
        self.assertEqual(code, 2)
        self.assertIn("-v /host/path/ziot_config.json", out)
        self.assertNotIn("is not valid JSON", out)


class StubCam:
    def __init__(self, uid):
        self.uid = uid
        self.cloud = {}
        self.mode = "direct"
        self.is_streaming = False
        self.fps = 0.0
        self.stats = {}

    def health(self):
        return {"uid": self.uid}


class HandlerConcurrencyTests(unittest.TestCase):
    """Registering cameras mid-serve must not break in-flight /health.

    The obvious version of this test -- register 50 cameras in a tight loop,
    then join the fetchers -- passes against the *unfixed* handler, because the
    loop finishes long before the first request is served and the two never
    overlap. Two things are needed to reproduce the real failure:
    registration has to run while requests are in flight, and the switch
    interval has to be short enough for the GIL to yield mid-iteration.
    Otherwise the whole iteration completes inside one slice and the bug hides.

    Verified: with `snapshot()` reverted to iterating the live dict, this fails
    with "dictionary changed size during iteration"; with the fix, it passes.
    """

    def setUp(self):
        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, old)

    def test_health_and_index_survive_concurrent_registration(self):
        cameras = {}
        lock = threading.Lock()
        boot = bridge.BootState()
        boot.set("ready", None)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            bridge.make_handler(cameras, boot, lock))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        stop = threading.Event()
        errors = []
        served = []

        def register():
            i = 0
            while not stop.is_set():
                with lock:
                    cameras[f"cam{i}"] = StubCam(f"cam{i}")
                i += 1
                time.sleep(0.0002)

        def fetch():
            while not stop.is_set():
                for endpoint in ("health", ""):
                    try:
                        with urllib.request.urlopen(
                                f"http://127.0.0.1:{port}/{endpoint}",
                                timeout=5) as r:
                            json.loads(r.read())
                        served.append(endpoint)
                    except Exception as e:      # noqa: BLE001 -- asserted below
                        errors.append(repr(e))
                        return

        writer = threading.Thread(target=register, daemon=True)
        writer.start()
        readers = [threading.Thread(target=fetch, daemon=True) for _ in range(6)]
        for t in readers:
            t.start()

        # Run until both sides have done enough to have overlapped, then stop.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not errors:
            with lock:
                registered = len(cameras)
            if registered > 400 and len(served) > 60:
                break
            time.sleep(0.02)
        stop.set()
        writer.join(timeout=5)
        for t in readers:
            t.join(timeout=5)

        self.assertEqual(errors, [])
        # Assert the test actually exercised the condition. Without this the
        # test can silently stop overlapping and go on passing forever, which
        # is exactly how the version it replaces came to protect nothing.
        with lock:
            registered = len(cameras)
        self.assertGreater(registered, 400, "registration never got going")
        self.assertGreater(len(served), 60, "no requests were served")


class CloudAuthParkTests(unittest.TestCase):
    def test_revoked_token_after_boot_parks_failed_once(self):
        api = mock.MagicMock()
        api.list_cameras.side_effect = bridge.CloudError(401, "Expired")
        boot = bridge.BootState()
        boot.set("ready", None)
        cloud = bridge.CloudState(api, 1, boot)
        with self.assertLogs(bridge.log) as logs:
            cloud.poll()
            cloud.poll()
        self.assertEqual(boot.snapshot()["phase"], "failed")
        rejection_lines = [line for line in logs.output
                           if "rejected the token" in line]
        self.assertEqual(len(rejection_lines), 1)

    def test_ordinary_outage_does_not_park_failed(self):
        api = mock.MagicMock()
        api.list_cameras.side_effect = ConnectionError("down")
        boot = bridge.BootState()
        boot.set("ready", None)
        cloud = bridge.CloudState(api, 1, boot)
        cloud.poll()
        self.assertEqual(boot.snapshot()["phase"], "ready")

    def test_a_park_lifts_when_the_cloud_accepts_the_token_again(self):
        """Latching for the process lifetime would leave /health reporting
        "failed" -- and the watchdog shouting -- for a bridge that is
        demonstrably working again."""
        api = mock.MagicMock()
        api.list_cameras.side_effect = bridge.CloudError(401, "Expired")
        boot = bridge.BootState()
        boot.set("ready", None)
        cloud = bridge.CloudState(api, 1, boot)
        cloud.poll()
        self.assertEqual(boot.snapshot()["phase"], "failed")

        api.list_cameras.side_effect = None
        api.list_cameras.return_value = [{"uid": CAM_UID}]
        cloud.poll()
        self.assertEqual(boot.snapshot(), {"phase": "ready"})

        # And a later rejection parks -- and logs -- again, rather than being
        # swallowed by a latch that was never reset.
        api.list_cameras.side_effect = bridge.CloudError(401, "Expired")
        with self.assertLogs(bridge.log, "ERROR") as logs:
            cloud.poll()
        self.assertEqual(boot.snapshot()["phase"], "failed")
        self.assertTrue(any("rejected the token" in line
                            for line in logs.output), logs.output)

    def test_a_park_is_not_lifted_for_a_state_we_did_not_set(self):
        """Only ever undo a park we made ourselves -- a successful poll must
        not promote a startup phase to ready behind bring_up's back."""
        api = mock.MagicMock()
        api.list_cameras.return_value = [{"uid": CAM_UID}]
        boot = bridge.BootState()
        boot.set("retrying", "device list unavailable: timed out", 2)
        cloud = bridge.CloudState(api, 1, boot)
        cloud.poll()
        cloud.poll()
        self.assertEqual(boot.snapshot()["phase"], "retrying")


class OfflineModeTests(unittest.TestCase):
    """--offline punches probe-found endpoints with zero cloud calls."""

    def setUp(self):
        self.old_argv = sys.argv[:]
        self.addCleanup(sys.argv.__setitem__, slice(None), self.old_argv)

    def write_cfg(self, cfg):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            json.dump(cfg, f)
            path = f.name
        self.addCleanup(Path(path).unlink, missing_ok=True)
        return path

    def test_static_rendezvous_needs_no_cloud(self):
        peer = bind_loopback()
        self.addCleanup(peer.close)
        port = peer.getsockname()[1]
        cam = bridge.ZiotCamera(None, {"uid": CAM_UID}, "127.0.0.1",
                                static_addr=("127.0.0.1", port))
        self.assertTrue(cam._rendezvous())
        self.assertEqual(cam.addr, ("127.0.0.1", port))
        self.assertEqual(cam._fetch_endpoint(),
                         ("127.0.0.1", port, "static"))
        cam.start()
        cam.stop()

    def test_main_offline_boots_without_token_or_cloud(self):
        path = self.write_cfg(
            {"offline_endpoints": {CAM_UID: "127.0.0.1:56061"}})
        sys.argv = ["ziot_rtp_bridge.py", "--config", path,
                    "--offline", "--bind-ip", "127.0.0.1"]
        with mock.patch.object(bridge, "GPS555") as gps, \
                mock.patch.object(bridge, "ThreadingHTTPServer") as server, \
                mock.patch.object(bridge, "ZiotCamera") as cam:
            cam.return_value.start.return_value = True
            bridge.main()
        gps.assert_not_called()
        _, kwargs = cam.call_args
        self.assertEqual(kwargs["static_addr"], ("127.0.0.1", 56061))
        server.assert_called_once()

    def test_offline_without_endpoints_is_argparse_error(self):
        path = self.write_cfg({"offline": True})
        sys.argv = ["ziot_rtp_bridge.py", "--config", path, "--offline"]
        with mock.patch.object(bridge, "GPS555") as gps:
            with self.assertRaises(SystemExit) as cm:
                bridge.main()
        self.assertEqual(cm.exception.code, 2)
        gps.assert_not_called()

    def test_offline_ignores_only_online(self):
        # Cloud flags are absent offline, so the startup-only filter would
        # drop every camera if it were applied.
        path = self.write_cfg(
            {"offline": True, "only_online": True,
             "offline_endpoints": {CAM_UID: "127.0.0.1:56061"}})
        sys.argv = ["ziot_rtp_bridge.py", "--config", path,
                    "--offline", "--bind-ip", "127.0.0.1"]
        with mock.patch.object(bridge, "GPS555"), \
                mock.patch.object(bridge, "ThreadingHTTPServer") as server, \
                mock.patch.object(bridge, "ZiotCamera") as cam:
            cam.return_value.start.return_value = True
            bridge.main()
        server.assert_called_once()

    def test_offline_binds_from_static_endpoint(self):
        # Without --bind-ip the default route (8.8.8.8) may leave through
        # the wrong NIC; the static endpoint already knows the camera LAN.
        path = self.write_cfg(
            {"offline_endpoints": {CAM_UID: "192.168.18.75:52901"}})
        sys.argv = ["ziot_rtp_bridge.py", "--config", path, "--offline"]
        with mock.patch.object(bridge, "GPS555"), \
                mock.patch.object(bridge, "ThreadingHTTPServer"), \
                mock.patch.object(bridge, "ZiotCamera") as cam, \
                mock.patch.object(bridge, "local_ip_for",
                                  return_value="192.168.18.45") as local_ip:
            cam.return_value.start.return_value = True
            bridge.main()
        local_ip.assert_called_once_with("192.168.18.75")

    def test_offline_rejects_unsendable_endpoint(self):
        path = self.write_cfg(
            {"offline_endpoints": {CAM_UID: "52901"}})
        sys.argv = ["ziot_rtp_bridge.py", "--config", path, "--offline"]
        with mock.patch.object(bridge, "GPS555") as gps:
            with self.assertRaises(SystemExit) as cm:
                bridge.main()
        self.assertEqual(cm.exception.code, 2)
        gps.assert_not_called()


if __name__ == "__main__":
    unittest.main()

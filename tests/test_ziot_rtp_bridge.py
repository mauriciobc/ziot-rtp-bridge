"""Behavior regressions for direct/relay RTP acceptance and startup validation."""
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
                finally:
                    cam._stop.set()
                    worker.join(timeout=5)
        self.assertFalse(worker.is_alive())


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


if __name__ == "__main__":
    unittest.main()

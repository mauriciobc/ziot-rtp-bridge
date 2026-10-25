"""The local RTSP relay, and the broker's handing of the relay address.

The camera is an RTSP publisher -- its own firmware says so
(`spook_rtsp_set_relay_info`, `rtsp://%s:%d/live/%s`, "start push,
session id:%s"), and the vendor's registration answer hands it a relay
address in the `0x8000` reply. So the relay address is a TCP address the
camera connects to, and the broker has to put the local relay's there
rather than the viewer's UDP media port, which no RTSP client can use.

These tests pin that contract end to end: the broker's answer, the
device list the bridge reads, and the relay serving a publisher's media
to a reader.
"""
import contextlib
import io
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ziot_fake_cloud as fake
import ziot_relay as relay_mod
from ziot_rtp_bridge import RtpJpegReassembler, RelayStream

UID = "140979857781"


class BrokerRelayAddressTests(unittest.TestCase):
    """The broker must hand the camera the relay's TCP address."""

    def setUp(self):
        self.out = io.StringIO()
        self._ctx = contextlib.redirect_stdout(self.out)
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)

    def test_the_registration_answer_carries_the_viewer_media_socket(self):
        """The 0x8000 reply arms the socket that reads the camera's RTP.

        The camera never speaks RTSP on this leg (the firmware has no
        ANNOUNCE/RECORD): after `0x8005` it opens a UDP socket and
        sendto's its RTP to the armed ip:port. So the viewer's media
        socket -- the one that actually reads those datagrams -- wins
        over the RTSP relay, which would wait for an ANNOUNCE forever.
        """
        api = fake.HttpApi(relay_host="192.168.68.114", relay_port=8554)
        api.viewers[UID] = {"privateIp": "192.168.68.114",
                            "privatePort": 53263}
        srv = fake.serve("sig", ("127.0.0.1", 0), api, "192.168.68.114",
                         "192.168.68.114", 8554)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        port = srv.server_address[1]

        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(fake.signalling_frame(0x0001, 1, UID.encode(),
                                        b"TXW817_A_V1.0.12.32"))
        deadline = time.time() + 3
        reply = b""
        while not reply and time.time() < deadline:
            try:
                reply = s.recv(4096)
            except socket.timeout:
                break
        s.close()
        self.assertTrue(reply, "the broker did not answer the registration")

        body = reply[10 + len(UID):]
        relay_ip = body[2:22].rstrip(b"\x00").decode()
        port1, port2 = struct.unpack(">HH", body[22:26])
        self.assertEqual(relay_ip, "192.168.68.114")
        # Both slots carry the viewer's UDP media port: the camera reads
        # offset 0x18, and this is where its RTP has to land.
        self.assertEqual(port1, 53263)
        self.assertEqual(port2, 53263)

    def test_without_a_relay_the_viewer_address_is_used(self):
        """With no relay configured, the old behaviour stands."""
        api = fake.HttpApi()
        api.viewers[UID] = {"privateIp": "192.168.68.114",
                            "privatePort": 53263}
        srv = fake.serve("sig", ("127.0.0.1", 0), api, "192.168.68.114")
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        port = srv.server_address[1]

        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(fake.signalling_frame(0x0001, 1, UID.encode(), b"reg"))
        deadline = time.time() + 3
        reply = b""
        while not reply and time.time() < deadline:
            try:
                reply = s.recv(4096)
            except socket.timeout:
                break
        s.close()
        body = reply[10 + len(UID):]
        port1, _ = struct.unpack(">HH", body[22:26])
        self.assertEqual(port1, 53263)

    def test_the_device_list_carries_the_relay_host_and_port(self):
        """The bridge learns where the camera publishes from this row."""
        api = fake.HttpApi(relay_host="192.168.68.114", relay_port=8554)
        api.add_camera(UID, "192.168.68.107")
        row = api._camera_row(UID)
        self.assertEqual(row["relay_ip"], "192.168.68.114:8554")

    def test_no_relay_leaves_the_relay_field_empty(self):
        api = fake.HttpApi()
        api.add_camera(UID, "192.168.68.107")
        self.assertEqual(api._camera_row(UID).get("relay_ip"), "")


class RelayTests(unittest.TestCase):
    """The relay accepts a publisher and serves its media to a reader."""

    def setUp(self):
        self.out = io.StringIO()
        self._ctx = contextlib.redirect_stdout(self.out)
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)
        self.relay = relay_mod.Relay(bind_ip="127.0.0.1", port=0)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.relay.port = self.port
        threading.Thread(target=self.relay.serve_forever, daemon=True).start()
        deadline = time.time() + 3
        while not self.relay._sock and time.time() < deadline:
            time.sleep(0.02)
        self.addCleanup(self.relay.stop.set)

    def publish(self, frames=8):
        """Act as the camera: ANNOUNCE, RECORD, then interleaved RTP.

        A frame is two fragments of one timestamp -- the first carries
        the quantisation tables, the second carries the marker bit, and
        only then does the reader's reassembler emit a JPEG.
        """
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.settimeout(5)
        sdp = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Test\r\n"
               "a=type:broadcast\r\nt=0 0\r\nc=IN IP4 0.0.0.0\r\n"
               "m=video 0 RTP/AVP 26\r\na=control:track0\r\n"
               "m=audio 0 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000/1\r\n"
               "a=control:track1\r\n").encode()
        s.sendall(b"ANNOUNCE rtsp://127.0.0.1:" + str(self.port).encode()
                  + b"/live/" + UID.encode() + b" RTSP/1.0\r\n"
                  b"CSeq: 1\r\nContent-Type: application/sdp\r\n"
                  b"Content-Length: " + str(len(sdp)).encode()
                  + b"\r\n\r\n" + sdp)
        s.recv(4096)
        s.sendall(b"RECORD rtsp://127.0.0.1:" + str(self.port).encode()
                  + b"/live/" + UID.encode() + b" RTSP/1.0\r\nCSeq: 2\r\n\r\n")
        s.recv(4096)
        for i in range(frames):
            first = self._fragment(i, marker=False, first=True)
            s.sendall(b"$" + bytes([0]) + struct.pack(">H", len(first)) + first)
            # The offset is where this data starts in the scan data: right
            # after the first fragment's 32 bytes (RFC 2435).
            last = self._fragment(i, marker=True, first=False, offset=32)
            s.sendall(b"$" + bytes([0]) + struct.pack(">H", len(last)) + last)
            time.sleep(0.03)
        return s

    @staticmethod
    def _fragment(index, marker=False, first=False, offset=0):
        """One RFC 2435 JPEG fragment, the shape the camera sends."""
        quant = bytes(range(1, 65)) + bytes(range(1, 65))
        hdr = bytearray(12)
        hdr[0] = 0x80
        hdr[1] = 26 | (0x80 if marker else 0)
        struct.pack_into(">I", hdr, 4, 1000 + index)
        struct.pack_into(">I", hdr, 8, 0x11223344)
        body = bytearray(8)
        body[1] = (offset >> 16) & 0xFF
        body[2] = (offset >> 8) & 0xFF
        body[3] = offset & 0xFF
        body[4] = 0
        body[5] = 0x80 if first else 0x00    # tables only in the first
        body[6] = 8                       # width / 8
        body[7] = 6                       # height / 8
        if first:
            body += bytes([0, 0])
            body += struct.pack(">H", len(quant))
            body += quant
        body += bytes(range(32))
        return bytes(hdr) + bytes(body)

    def test_a_publisher_is_accepted_and_its_media_served(self):
        """Publish, then read back with the bridge's own relay client."""
        pub = self.publish()
        self.addCleanup(pub.close)
        deadline = time.time() + 3
        while UID not in self.relay.sessions and time.time() < deadline:
            time.sleep(0.02)
        self.assertIn(UID, self.relay.sessions,
                      "the relay did not register the publisher")

        frames = []
        rx = []
        stop = threading.Event()
        stream = RelayStream(f"rtsp://127.0.0.1:{self.port}/live/{UID}",
                              frames.append, lambda p: None,
                              lambda: rx.append(1), stop, "test")
        self.assertTrue(stream.open(),
                        "the bridge's relay client could not open the relay")
        threading.Thread(target=stream.pump, daemon=True).start()
        deadline = time.time() + 5
        while not frames and time.time() < deadline:
            time.sleep(0.05)
        stop.set()
        stream.close()
        self.assertTrue(frames, "no media reached the reader")
        self.assertTrue(rx, "the reader saw no packets at all")

    def test_a_reader_gets_404_when_nothing_is_published(self):
        """An empty relay says so, rather than hanging."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.settimeout(5)
        s.sendall(f"DESCRIBE rtsp://127.0.0.1:{self.port}/live/{UID} "
                  "RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
        reply = s.recv(4096)
        s.close()
        self.assertIn(b"404", reply.split(b"\r\n")[0])

    def test_the_publisher_uid_comes_from_the_publish_url(self):
        """`/live/<uid>` names the camera; the uid is what the bridge asks for."""
        self.assertEqual(self.relay._uid_from_uri(
            f"rtsp://127.0.0.1:{self.port}/live/{UID}"), UID)
        self.assertEqual(self.relay._uid_from_uri(
            f"rtsp://127.0.0.1:{self.port}/live/{UID}/track0"), UID)


if __name__ == "__main__":
    unittest.main()

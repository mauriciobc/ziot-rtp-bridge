#!/usr/bin/env python3
"""A local RTSP relay: the camera publishes here, the bridge plays it here.

The camera is an RTSP *publisher*, not a server. Its own firmware says
so, and the strings are the proof:

    spook_rtsp_set_relay_info   (0x5dd1c)  takes the relay {ip, port}
    rtsp://%s:%d/live/%s         (0x5dd63)  the URL it publishes to
    "start push, session id:%s"  (0x5d6fc)  it opens a push session
    "relaySession:%s"            (0x5d889)
    rtsp://%s:%d/webcam/jpeg_dvp (0x5d830)  the path it serves to peers
    m=video %d RTP/AVP 26        (0x5cd08)  JPEG video
    m=audio %d RTP/AVP 0         (0x5cc00)  PCMU audio
    RTP/AVP/TCP                  (0x5d40a)  interleaved transport

The vendor's own signalling answer (captured byte for byte, see
ziot-fw/REPORT.md section 2.6) hands the camera a relay address in its
`0x8000` registration answer: a 20-byte ASCII IP and two big-endian
u16 ports (554 = service, 10000 = media). That address is where the
camera *connects* -- it is an RTSP client. So replacing that address
with ours is all it takes to move the camera off the vendor's cloud and
onto this relay: no push command to guess, no app, no vendor account.

This is a real RTSP server for both roles:

  * as a PUBLISH target, it accepts the camera's ANNOUNCE (with the
    camera's SDP) and RECORD, and starts pulling RTP;
  * as a PLAY target, it serves DESCRIBE/SETUP/PLAY to the bridge and
    forwards the media it holds.

Media is RTP with JPEG video (payload type 26) and PCMU audio (type 0),
exactly what the bridge's RtpJpegReassembler and µ-law decoder expect,
so the relay is a transport swap only -- it never touches the payload.

Standard library only. No third-party dependencies.

Usage:
    python3 ziot_relay.py --port 8554 --bind-ip 192.168.68.114
"""
import argparse
import socket
import struct
import sys
import threading
import time
from urllib.parse import urlsplit

# The relay's own SDP, advertised to readers. It mirrors what the
# camera publishes: one JPEG video track and one PCMU audio track, both
# over RTSP interleaved.
VIDEO_PT = 26
AUDIO_PT = 0


def sdp_for(path: str, video_track: int, audio_track: int) -> bytes:
    """The SDP a reader gets from DESCRIBE."""
    lines = [
        "v=0",
        "o=- 1 1 IN IP4 127.0.0.1",
        "s=Test",
        "a=type:broadcast",
        "t=0 0",
        f"c=IN IP4 0.0.0.0",
        f"m=video 0 RTP/AVP {VIDEO_PT}",
        f"a=control:track{video_track}",
        f"m=audio 0 RTP/AVP {AUDIO_PT}",
        "a=rtpmap:0 PCMU/8000/1",
        f"a=control:track{audio_track}",
    ]
    return ("\r\n".join(lines) + "\r\n").encode()


def parse_sdp(body: bytes) -> list:
    """The (kind, control) of every media line in an SDP."""
    tracks = []
    kind = None
    for raw in body.decode("latin-1", "replace").splitlines():
        line = raw.strip()
        if line.startswith("m="):
            parts = line.split()
            kind = "video" if parts[0] == "m=video" else (
                "audio" if parts[0] == "m=audio" else None)
            pt = int(parts[3].split("/")[0]) if len(parts) > 3 else 0
            if kind:
                tracks.append({"kind": kind, "pt": pt, "control": None})
        elif line.startswith("a=control:") and tracks:
            tracks[-1]["control"] = line.split(":", 1)[1]
    return tracks


def read_line_block(sock: socket.socket, buf: bytearray) -> bytes:
    """Read one RTSP message head (up to the blank line)."""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed during header read")
        buf.extend(chunk)
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    buf.clear()
    buf.extend(rest)
    return head


def parse_head(head: bytes) -> tuple:
    """(method, uri, headers) of an RTSP request head."""
    lines = head.decode("latin-1", "replace").split("\r\n")
    parts = lines[0].split()
    method = parts[0] if parts else ""
    uri = parts[1] if len(parts) > 1 else ""
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return method, uri, headers


def response(status: int, reason: str, headers: dict = None,
             body: bytes = b"") -> bytes:
    """One RTSP response."""
    out = [f"RTSP/1.0 {status} {reason}"]
    for k, v in (headers or {}).items():
        out.append(f"{k}: {v}")
    if body:
        out.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(out) + "\r\n\r\n").encode() + body


def interleaved(channel: int, payload: bytes) -> bytes:
    """One interleaved RTSP frame ($ + channel + length + payload)."""
    return b"$" + bytes([channel]) + struct.pack(">H", len(payload)) + payload


class Reader:
    """One reader's socket, with the lock its writes share.

    The media fan-out and the reader's own keepalive reply both write to
    this socket. Two writers on one TCP connection interleave their
    bytes, so every write goes through `lock`.
    """

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.lock = threading.Lock()

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)


class Session:
    """One camera publishing into the relay."""

    # A reader that connects after the publisher started must still get
    # media, so the session keeps a short ring of the last packets. A
    # real relay does the same: a viewer joining late is served from
    # the buffer, not from the moment it arrived.
    BUFFER_PACKETS = 128

    def __init__(self, uid: str, sock: socket.socket, addr):
        self.uid = uid
        self.sock = sock
        self.addr = addr
        self.sdp = b""
        self.tracks = []
        self.session_id = ""
        self.readers: list = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.last_media = time.monotonic()
        self.packets = 0
        self.bytes = 0
        # (channel, payload) of the most recent packets.
        self.buffer: list = []

    def publish(self, channel: int, payload: bytes) -> None:
        """Fan one RTP packet out to the readers.

        The write is serialized per reader: the media fan-out and the
        reader's own keepalive reply share the socket, and two writers
        on one TCP connection interleave their bytes.
        """
        with self.lock:
            self.packets += 1
            self.bytes += len(payload)
            self.last_media = time.monotonic()
            self.buffer.append((channel, payload))
            if len(self.buffer) > self.BUFFER_PACKETS:
                del self.buffer[0]
            readers = list(self.readers)
        for reader in readers:
            try:
                with reader.lock:
                    reader.send(interleaved(channel, payload))
            except OSError:
                pass

    def flush_to(self, reader: "Reader") -> None:
        """Send the buffered packets to a reader that just joined."""
        with self.lock:
            buffered = list(self.buffer)
        for channel, payload in buffered:
            try:
                with reader.lock:
                    reader.send(interleaved(channel, payload))
            except OSError:
                return


class Relay:
    """The relay server: one publisher per camera, many readers."""

    def __init__(self, bind_ip: str = "0.0.0.0", port: int = 8554,
                 extra_ports: list = None, log=None):
        self.bind_ip = bind_ip
        self.port = port
        # The camera's registration answer carries TWO relay ports (554
        # and 10000 at the vendor; the firmware reads the second one),
        # and which of them the camera dials is not settled -- so the
        # relay listens on both and the publisher is found on whichever.
        self.extra_ports = list(extra_ports or [])
        self._log = log or (lambda kind, msg: print(f"{kind:<9} {msg}",
                                                    flush=True))
        self.sessions: dict = {}
        self.lock = threading.Lock()
        self._sock = None
        self._socks: list = []
        # Set to stop the accept loop and every session thread.
        self.stop = threading.Event()

    # ---- helpers -----------------------------------------------------------

    def _uid_from_uri(self, uri: str) -> str:
        """The camera's uid from the publish URL.

        The camera publishes to `rtsp://<relay>/live/<uid>` (its own
        format string) and serves peers at `/webcam/jpeg_dvp`. Accept
        both, plus the bare uid, so the path shape cannot break it.
        """
        path = urlsplit(uri).path if "://" in uri else uri
        for part in reversed([p for p in path.split("/") if p]):
            if part.isdigit() and len(part) >= 6:
                return part
        return path.strip("/").split("/")[-1] if path.strip("/") else ""

    def _session_for(self, uid: str) -> Session | None:
        with self.lock:
            return self.sessions.get(uid)

    def _publish(self, uid: str, session: Session) -> None:
        with self.lock:
            self.sessions[uid] = session
        self._log("publish", f"uid={uid} publishing from "
                             f"{session.addr[0]}:{session.addr[1]}")

    def _unpublish(self, uid: str, session: Session) -> None:
        with self.lock:
            if self.sessions.get(uid) is session:
                del self.sessions[uid]
        self._log("publish", f"uid={uid} stopped "
                             f"({session.packets} packets, "
                             f"{session.bytes} bytes)")

    # ---- the camera's side: ANNOUNCE / RECORD ------------------------------

    @staticmethod
    def _read_body(sock: socket.socket, buf: bytearray,
                   headers: dict) -> bytes:
        """Read a message body of the declared length."""
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            length = 0
        while len(buf) < length:
            chunk = sock.recv(65535)
            if not chunk:
                break
            buf.extend(chunk)
        body = bytes(buf[:length])
        del buf[:length]
        return body

    def _pump_interleaved(self, sock: socket.socket, buf: bytearray,
                          session: Session) -> None:
        """Read interleaved RTP from the camera and fan it out.

        The camera sends RTP over the same TCP connection ($ framing),
        which is what "RTP/AVP/TCP" in its own string table means.
        Channel 0 is the video track, channel 1 the audio track -- the
        order its SETUP negotiates.
        """
        sock.settimeout(10.0)
        while not self.stop.is_set():
            # Any bytes before a '$' are RTSP responses we do not need.
            while buf and buf[0:1] != b"$":
                if buf[:1] == b"R":
                    # A response head: consume it whole.
                    try:
                        read_line_block(sock, buf)
                    except (ConnectionError, OSError, socket.timeout):
                        return
                else:
                    del buf[0]
            if len(buf) < 4:
                try:
                    chunk = sock.recv(65535)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buf.extend(chunk)
                continue
            channel = buf[1]
            length = struct.unpack(">H", bytes(buf[2:4]))[0]
            while len(buf) < 4 + length:
                try:
                    chunk = sock.recv(65535)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buf.extend(chunk)
            payload = bytes(buf[4:4 + length])
            del buf[:4 + length]
            session.publish(channel, payload)

    # ---- the bridge's side: DESCRIBE / SETUP / PLAY -------------------------

    def _serve_media(self, sock: socket.socket, buf: bytearray,
                     session: Session, reader_id: int,
                     reader: "Reader | None" = None) -> None:
        """Forward the session's RTP to one reader until it goes away.

        The reader is registered BEFORE the PLAY response is sent, so a
        publisher that is already streaming cannot lose its first
        packets between the PLAY and the registration.
        """
        if reader is None:
            reader = Reader(sock)
            with session.lock:
                session.readers.append(reader)
        sock.settimeout(1.0)
        try:
            while not self.stop.is_set():
                # A reader's own keepalive (the bridge sends OPTIONS
                # every few seconds) must be answered, or it drops us.
                if buf:
                    try:
                        head = read_line_block(sock, buf)
                    except (socket.timeout, ConnectionError, OSError):
                        head = None
                    if head:
                        _, _, headers = parse_head(head)
                        with reader.lock:
                            sock.sendall(response(200, "OK", {
                                "CSeq": headers.get("cseq", "0"),
                            }))
                time.sleep(0.02)
        finally:
            with session.lock:
                session.readers = [r for r in session.readers
                                   if id(r.sock) != reader_id]

    # ---- accept loop -------------------------------------------------------

    def serve_forever(self) -> None:
        """Listen on every configured port and serve them all.

        The camera's registration answer carries two relay ports, and
        the firmware reads the second one, so the relay listens on
        both: whichever the camera dials, the publisher is found.
        """
        iface = ""
        if self.bind_ip and self.bind_ip not in ("0.0.0.0", "127.0.0.1"):
            iface = self._interface_for(self.bind_ip)

        listeners = []
        for port in [self.port] + [p for p in self.extra_ports
                                   if p and p != self.port]:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # The broker's address is a /32 alias on the LAN interface
            # (192.168.68.114/32 on wlan0). Binding that address
            # directly fails with EADDRNOTAVAIL, because it is not a
            # local address on the interface -- it is an alias the
            # kernel accepts for *outgoing* traffic but not as a bind
            # target. So: bind every address and, when an interface was
            # named, restrict the socket to it with SO_BINDTODEVICE.
            if iface:
                try:
                    srv.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                                   iface.encode())
                    self._log("relay", f"bound to device {iface}")
                except OSError as e:
                    self._log("relay", f"SO_BINDTODEVICE {iface} failed: "
                                      f"{e} -- serving every interface")
            try:
                srv.bind(("0.0.0.0", port))
            except OSError as e:
                self._log("relay", f"cannot bind port {port}: {e}")
                srv.close()
                continue
            srv.listen(16)
            listeners.append(srv)
            self._log("relay", f"listening on {self.bind_ip}:{port}")

        if not listeners:
            raise SystemExit("the relay could not listen on any port")
        self._sock = listeners[0]
        self._socks = listeners
        for srv in listeners:
            threading.Thread(target=self._accept_loop, args=(srv,),
                             daemon=True).start()
        while not self.stop.is_set():
            time.sleep(0.5)

    def _accept_loop(self, srv: socket.socket) -> None:
        """Accept on one listening socket."""
        while not self.stop.is_set():
            try:
                sock, addr = srv.accept()
            except OSError:
                break
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # ANNOUNCE means a publisher; anything else is a reader.
            threading.Thread(target=self._route, args=(sock, addr),
                             daemon=True).start()

    @staticmethod
    def _interface_for(ip: str) -> str:
        """The interface that holds `ip`, or "" if it cannot be told.

        The broker's own address is a /32 alias on the home-LAN
        interface, so the relay has to bind to that interface to be
        reachable at the address the camera is told.
        """
        import subprocess
        try:
            out = subprocess.run(
                ["ip", "-brief", "address"],
                capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            if ip in parts[2:]:
                return parts[0]
        return ""

    def _route(self, sock: socket.socket, addr) -> None:
        """Peek at the first request to tell publisher from reader."""
        buf = bytearray()
        sock.settimeout(15.0)
        try:
            head = read_line_block(sock, buf)
        except (ConnectionError, OSError, socket.timeout):
            try:
                sock.close()
            except OSError:
                pass
            return
        method, uri, headers = parse_head(head)
        if method == "ANNOUNCE":
            self._handle_announce(sock, addr, head, buf, uri, headers)
        else:
            self._handle_reader_first(sock, addr, head, buf, method, uri,
                                      headers)

    def _handle_announce(self, sock, addr, head, buf, uri, headers) -> None:
        uid = self._uid_from_uri(uri)
        session = Session(uid, sock, addr)
        session.sdp = self._read_body(sock, buf, headers)
        session.tracks = parse_sdp(session.sdp)
        session.session_id = f"{int(time.time() * 1000)}"
        self._publish(uid, session)
        sock.sendall(response(200, "OK", {
            "CSeq": headers.get("cseq", "0"),
            "Session": session.session_id,
        }))
        self._log("publish", f"uid={uid} announced {len(session.sdp)} bytes "
                             f"of SDP from {addr[0]}:{addr[1]}")
        try:
            self._publisher_loop(sock, buf, session)
        finally:
            self._unpublish(uid, session)
            try:
                sock.close()
            except OSError:
                pass

    def _publisher_loop(self, sock, buf, session) -> None:
        """After ANNOUNCE, expect RECORD then interleaved RTP."""
        sock.settimeout(30.0)
        while not self.stop.is_set():
            try:
                head = read_line_block(sock, buf)
            except socket.timeout:
                continue
            except (ConnectionError, OSError):
                return
            method, uri, headers = parse_head(head)
            cseq = headers.get("cseq", "0")
            if method == "RECORD":
                sock.sendall(response(200, "OK", {
                    "CSeq": cseq,
                    "Session": session.session_id,
                    "RTP-Info": f"url={uri};seq=0;rtptime=0,",
                }))
                self._pump_interleaved(sock, buf, session)
                return
            if method == "TEARDOWN":
                sock.sendall(response(200, "OK", {"CSeq": cseq}))
                return
            sock.sendall(response(501, "Not Implemented", {"CSeq": cseq}))

    def _handle_reader_first(self, sock, addr, head, buf, method, uri,
                             headers) -> None:
        """Answer the reader's first request, then keep serving it."""
        cseq = headers.get("cseq", "0")
        uid = self._uid_from_uri(uri)
        session = self._session_for(uid)
        if method == "DESCRIBE":
            if session is None:
                sock.sendall(response(404, "Not Found", {"CSeq": cseq}))
                self._log("reader", f"uid={uid}: nothing published yet")
                try:
                    sock.close()
                except OSError:
                    pass
                return
            body = session.sdp or sdp_for(uri, 0, 1)
            sock.sendall(response(200, "OK", {
                "CSeq": cseq,
                "Content-Type": "application/sdp",
                "Content-Base": uri,
            }, body))
            self._log("reader", f"uid={uid}: DESCRIBE served")
            self._reader_continue(sock, buf, session, uid)
        elif method == "OPTIONS":
            sock.sendall(response(200, "OK", {
                "CSeq": cseq,
                "Public": "OPTIONS, DESCRIBE, SETUP, TEARDOWN, PLAY",
            }))
            self._reader_continue(sock, buf, session, uid)
        else:
            sock.sendall(response(501, "Not Implemented", {"CSeq": cseq}))
            try:
                sock.close()
            except OSError:
                pass

    def _reader_continue(self, sock, buf, session, uid) -> None:
        """The rest of a reader's conversation."""
        sock.settimeout(30.0)
        reader_id = id(sock)
        try:
            while not self.stop.is_set():
                try:
                    head = read_line_block(sock, buf)
                except socket.timeout:
                    break
                except (ConnectionError, OSError):
                    break
                method, uri, headers = parse_head(head)
                cseq = headers.get("cseq", "0")
                if method == "DESCRIBE":
                    # The bridge asks OPTIONS first and DESCRIBE second,
                    # so DESCRIBE is the second request, not the first.
                    if session is None:
                        session = self._session_for(uid)
                    if session is None:
                        sock.sendall(response(404, "Not Found",
                                              {"CSeq": cseq}))
                        break
                    body = session.sdp or sdp_for(uri, 0, 1)
                    sock.sendall(response(200, "OK", {
                        "CSeq": cseq,
                        "Content-Type": "application/sdp",
                        "Content-Base": uri,
                    }, body))
                    self._log("reader", f"uid={uid}: DESCRIBE served "
                                        f"({len(body)} bytes)")
                elif method == "SETUP":
                    if session is not None and not session.session_id:
                        session.session_id = f"{int(time.time() * 1000)}"
                    sock.sendall(response(200, "OK", {
                        "CSeq": cseq,
                        "Session": (session.session_id if session
                                    else f"{int(time.time() * 1000)}"),
                        "Transport": "RTP/AVP/TCP;unicast;"
                                     "interleaved=0-1",
                    }))
                elif method == "PLAY":
                    if session is None:
                        sock.sendall(response(454, "Session Not Found",
                                              {"CSeq": cseq}))
                        break
                    # Register the reader before answering, so a
                    # publisher already streaming cannot lose its
                    # first packets.
                    reader = Reader(sock)
                    with session.lock:
                        session.readers.append(reader)
                    sock.sendall(response(200, "OK", {
                        "CSeq": cseq,
                        "Session": session.session_id,
                        "RTP-Info": f"url={uri};seq=0;rtptime=0,",
                    }))
                    session.flush_to(reader)
                    session.flush_to(reader)
                    self._log("reader", f"uid={uid}: PLAY")
                    self._serve_media(sock, buf, session, reader_id, reader)
                    return
                elif method == "TEARDOWN":
                    sock.sendall(response(200, "OK", {"CSeq": cseq}))
                    break
                elif method == "OPTIONS":
                    sock.sendall(response(200, "OK", {"CSeq": cseq}))
                else:
                    sock.sendall(response(501, "Not Implemented",
                                          {"CSeq": cseq}))
        finally:
            if session is not None:
                with session.lock:
                    session.readers = [r for r in session.readers
                                       if id(r) != reader_id]
            try:
                sock.close()
            except OSError:
                pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Local RTSP relay: the camera publishes here, the "
                    "bridge plays it here.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("The camera is an RTSP publisher. Its signalling answer "
                "(0x8000) must carry this host's address in the relay slot, "
                "with a TCP port the camera can connect to -- see "
                "ziot_fake_cloud.py."))
    ap.add_argument("--port", type=int, default=8554,
                    help="TCP port to listen on (default 8554)")
    ap.add_argument("--extra-port", action="append", type=int, default=[],
                    help="another port to listen on (repeatable). The "
                         "camera's registration answer carries two relay "
                         "ports and the firmware reads the second one, so "
                         "the relay listens on both.")
    ap.add_argument("--bind-ip", default="0.0.0.0",
                    help="address to bind (default all)")
    args = ap.parse_args(argv)
    relay = Relay(bind_ip=args.bind_ip, port=args.port,
                  extra_ports=args.extra_port)
    try:
        relay.serve_forever()
    except KeyboardInterrupt:
        relay.stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())

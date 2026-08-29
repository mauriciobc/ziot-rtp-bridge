#!/usr/bin/env python3
"""
ziot_rtp_bridge.py — v2 with auto-recovery
============================================
Changes from v1:
  - Full re-rendezvous on endpoint move (send_stun_addr + notify wake)
  - Auto-recovery thread: tears down socket and re-rendezvous from scratch
    when stream dies permanently
  - Exponential backoff on re-rendezvous (5s → 60s)
  - Socket recreation to avoid stale NAT bindings
  - /health endpoint with re-rendezvous count + backoff state
"""
import argparse
import json
import logging
import queue
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_URL = "https://ipc.gps555.net"
APP_ID = "b1ee47e92dfa22635907aa6bb882b1dc0ebc0285"
PUNCH = b"App send hello"
KEEPALIVE_INTERVAL = 2
PUNCH_INTERVAL = 0.5
STARVED_THRESHOLD = 2.0        # re-resolve after 2s of silence
FPS_LOG_INTERVAL = 30           # log fps every 30s
RECOVERY_DEAD_THRESHOLD = 10.0  # start recovery after 10s dead
RECOVERY_MAX_BACKOFF = 60.0     # max wait between re-rendezvous attempts
RECOVERY_INITIAL_BACKOFF = 5.0  # first retry after 5s

log = logging.getLogger("ziot")


class Fanout:
    def __init__(self, maxsize: int):
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def publish(self, item) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(item)
                except (queue.Empty, queue.Full):
                    pass

    @contextmanager
    def subscribe(self):
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.add(q)
        try:
            yield q
        finally:
            with self._lock:
                self._subs.discard(q)


def _build_ulaw_table() -> list[int]:
    table = []
    for byte in range(256):
        u = ~byte & 0xff
        t = (((u & 0x0f) << 3) + 0x84) << ((u & 0x70) >> 4)
        table.append((0x84 - t) if (u & 0x80) else (t - 0x84))
    return table


_ULAW = _build_ulaw_table()
_ULAW_PCM = b"".join(struct.pack("<h", v) for v in _ULAW)


def ulaw_to_pcm16(payload: bytes) -> bytes:
    out = bytearray(len(payload) * 2)
    for i, b in enumerate(payload):
        out[i * 2:i * 2 + 2] = _ULAW_PCM[b * 2:b * 2 + 2]
    return bytes(out)


AUDIO_RATE = 8000


def wav_header(rate: int = AUDIO_RATE, channels: int = 1, bits: int = 16) -> bytes:
    block_align = channels * bits // 8
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                    rate * block_align, block_align, bits)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


class GPS555:
    def __init__(self, token: str):
        self._h = {
            "Authorization": f"Bearer {token}",
            "language": "pt",
            "User-Agent": "Dart/3.10 (dart:io)",
        }

    def _get(self, path: str, **params) -> dict:
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode({k: str(v) for k, v in params.items()})
        req = urllib.request.Request(url, headers=self._h)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def list_cameras(self, user_id: int) -> list[dict]:
        return self._get("/api/v1/ipc", terminalFamilyId=user_id)["data"]["list"]

    def send_stun_addr(self, uid, ip, port):
        return self._get("/api/v1/ipc/send-stun-addr", appId=APP_ID, uid=uid,
                         publicIp=ip, publicPort=port,
                         privateIp=ip, privatePort=port)["data"]

    def get_stun_addr(self, uid):
        return self._get(f"/api/v1/ipc/stun-addr/{uid}")["data"]

    def notify(self, uid, event_type: int):
        self._get("/api/v1/ipc/notify-live-event",
                  appId=APP_ID, eventType=event_type, uid=uid)


_DC_SY = bytes(range(12))
_LUM_DC_CL = bytes([0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0])
_CHM_DC_CL = bytes([0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0])
_LUM_AC_CL = bytes([0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7d])
_LUM_AC_SY = bytes([
    0x01,0x02,0x03,0x00,0x04,0x11,0x05,0x12,0x21,0x31,0x41,0x06,0x13,0x51,0x61,0x07,
    0x22,0x71,0x14,0x32,0x81,0x91,0xa1,0x08,0x23,0x42,0xb1,0xc1,0x15,0x52,0xd1,0xf0,
    0x24,0x33,0x62,0x72,0x82,0x09,0x0a,0x16,0x17,0x18,0x19,0x1a,0x25,0x26,0x27,0x28,
    0x29,0x2a,0x34,0x35,0x36,0x37,0x38,0x39,0x3a,0x43,0x44,0x45,0x46,0x47,0x48,0x49,
    0x4a,0x53,0x54,0x55,0x56,0x57,0x58,0x59,0x5a,0x63,0x64,0x65,0x66,0x67,0x68,0x69,
    0x6a,0x73,0x74,0x75,0x76,0x77,0x78,0x79,0x7a,0x83,0x84,0x85,0x86,0x87,0x88,0x89,
    0x8a,0x92,0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9a,0xa2,0xa3,0xa4,0xa5,0xa6,0xa7,
    0xa8,0xa9,0xaa,0xb2,0xb3,0xb4,0xb5,0xb6,0xb7,0xb8,0xb9,0xba,0xc2,0xc3,0xc4,0xc5,
    0xc6,0xc7,0xc8,0xc9,0xca,0xd2,0xd3,0xd4,0xd5,0xd6,0xd7,0xd8,0xd9,0xda,0xe1,0xe2,
    0xe3,0xe4,0xe5,0xe6,0xe7,0xe8,0xe9,0xea,0xf1,0xf2,0xf3,0xf4,0xf5,0xf6,0xf7,0xf8,
    0xf9,0xfa])
_CHM_AC_CL = bytes([0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77])
_CHM_AC_SY = bytes([
    0x00,0x01,0x02,0x03,0x11,0x04,0x05,0x21,0x31,0x06,0x12,0x41,0x51,0x07,0x61,0x71,
    0x13,0x22,0x32,0x81,0x08,0x14,0x42,0x91,0xa1,0xb1,0xc1,0x09,0x23,0x33,0x52,0xf0,
    0x15,0x62,0x72,0xd1,0x0a,0x16,0x24,0x34,0xe1,0x25,0xf1,0x17,0x18,0x19,0x1a,0x26,
    0x27,0x28,0x29,0x2a,0x35,0x36,0x37,0x38,0x39,0x3a,0x43,0x44,0x45,0x46,0x47,0x48,
    0x49,0x4a,0x53,0x54,0x55,0x56,0x57,0x58,0x59,0x5a,0x63,0x64,0x65,0x66,0x67,0x68,
    0x69,0x6a,0x73,0x74,0x75,0x76,0x77,0x78,0x79,0x7a,0x82,0x83,0x84,0x85,0x86,0x87,
    0x88,0x89,0x8a,0x92,0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9a,0xa2,0xa3,0xa4,0xa5,
    0xa6,0xa7,0xa8,0xa9,0xaa,0xb2,0xb3,0xb4,0xb5,0xb6,0xb7,0xb8,0xb9,0xba,0xc2,0xc3,
    0xc4,0xc5,0xc6,0xc7,0xc8,0xc9,0xca,0xd2,0xd3,0xd4,0xd5,0xd6,0xd7,0xd8,0xd9,0xda,
    0xe2,0xe3,0xe4,0xe5,0xe6,0xe7,0xe8,0xe9,0xea,0xf2,0xf3,0xf4,0xf5,0xf6,0xf7,0xf8,
    0xf9,0xfa])


def _dht(class_id: int, codelens: bytes, symbols: bytes) -> bytes:
    return (b"\xff\xc4" + struct.pack(">H", 3 + 16 + len(symbols))
            + bytes([class_id]) + codelens + symbols)


def build_jpeg_header(width, height, qtables, jtype, dri) -> bytes:
    out = bytearray(b"\xff\xd8")
    out += b"\xff\xdb\x00\x43\x00" + qtables[:64]
    out += b"\xff\xdb\x00\x43\x01" + qtables[64:128]
    if dri:
        out += b"\xff\xdd\x00\x04" + struct.pack(">H", dri)
    sampling = b"\x22" if (jtype & 0x3f) == 1 else b"\x21"
    out += (b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", height, width)
            + b"\x03\x01" + sampling + b"\x00\x02\x11\x01\x03\x11\x01")
    out += _dht(0x00, _LUM_DC_CL, _DC_SY)
    out += _dht(0x10, _LUM_AC_CL, _LUM_AC_SY)
    out += _dht(0x01, _CHM_DC_CL, _DC_SY)
    out += _dht(0x11, _CHM_AC_CL, _CHM_AC_SY)
    out += b"\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"
    return bytes(out)


class RtpJpegReassembler:
    def __init__(self, emit):
        self._frags = defaultdict(dict)
        self._meta = {}
        self._emit = emit

    def reset(self):
        self._frags.clear()
        self._meta.clear()

    def feed(self, pkt: bytes) -> None:
        marker = pkt[1] >> 7
        ts = struct.unpack(">I", pkt[4:8])[0]
        p = pkt[12:]
        frag_off = struct.unpack(">I", b"\x00" + p[1:4])[0]
        jtype, q = p[4], p[5]
        width, height = p[6] * 8, p[7] * 8
        off, dri = 8, 0
        if jtype >= 64:
            dri = struct.unpack(">H", p[8:10])[0]
            off = 12
        if q >= 128 and frag_off == 0:
            qlen = struct.unpack(">H", p[off + 2:off + 4])[0]
            self._meta[ts] = (width, height, p[off + 4:off + 4 + qlen], jtype, dri)
            off += 4 + qlen
        self._frags[ts][frag_off] = p[off:]
        if marker and ts in self._meta:
            w, h, qt, jt, dri = self._meta.pop(ts)
            parts = self._frags.pop(ts)
            body = b"".join(parts[o] for o in sorted(parts))
            self._emit(build_jpeg_header(w, h, qt, jt, dri) + body + b"\xff\xd9")
        if len(self._frags) > 8:
            for old in sorted(self._frags)[:-4]:
                self._frags.pop(old, None)
                self._meta.pop(old, None)


class ZiotCamera:
    def __init__(self, api: GPS555, cam: dict, bind_ip: str):
        self.api = api
        self.uid = cam["uid"]
        self.bind_ip = bind_ip
        self.video = Fanout(maxsize=4)
        self.audio = Fanout(maxsize=64)
        self._stop = threading.Event()
        self.addr = None
        self.sock = None
        self._sock_lock = threading.Lock()  # guards socket swap
        self.stats = {"frames": 0, "audio_pkts": 0}
        self._last_rx = 0.0
        # FPS tracking
        self._frame_times: deque = deque(maxlen=120)
        self._last_fps_log = time.monotonic()
        # Endpoint move tracking
        self._endpoint_moves = 0
        self._last_resolve = 0.0
        # Recovery tracking
        self._re_rendezvous_count = 0
        self._backoff = RECOVERY_INITIAL_BACKOFF
        self._last_re_rendezvous = 0.0

    def _rendezvous(self) -> bool:
        """Full rendezvous: bind socket, register port, wake camera, get address.
        Returns True if we got a valid camera address."""
        # Close old socket if any
        old_sock = None
        with self._sock_lock:
            if self.sock:
                old_sock = self.sock
                self.sock = None
        if old_sock:
            try:
                old_sock.close()
            except Exception:
                pass

        # Create fresh socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.bind_ip, 0))
        port = sock.getsockname()[1]

        # Register our port with cloud
        for _ in range(3):
            try:
                self.api.send_stun_addr(self.uid, self.bind_ip, port)
            except Exception as e:
                log.warning("[%s] send-stun-addr: %s", self.uid, e)

        # Wake the camera
        try:
            self.api.notify(self.uid, 1)
        except Exception as e:
            log.warning("[%s] notify(1): %s", self.uid, e)

        # Get camera address
        addr = None
        for _ in range(5):
            try:
                d = self.api.get_stun_addr(self.uid)
                addr = (d["IpcPrivateIP"], d["IpcPrivatePort"])
                break
            except Exception:
                time.sleep(1)

        if not addr:
            log.error("[%s] no address from broker", self.uid)
            try:
                sock.close()
            except Exception:
                pass
            return False

        # Install the new socket
        with self._sock_lock:
            self.sock = sock
            self.addr = addr

        log.info("[%s] camera at %s:%d (we are %s:%d)",
                 self.uid, addr[0], addr[1], self.bind_ip, port)
        return True

    def start(self) -> bool:
        if not self._rendezvous():
            return False

        for fn in (self._keepalive, self._punch, self._receive, self._fps_logger,
                   self._recovery):
            threading.Thread(target=fn, daemon=True).start()
        return True

    def _keepalive(self):
        while not self._stop.is_set():
            try:
                self.api.notify(self.uid, 0)
            except Exception:
                pass
            self._stop.wait(KEEPALIVE_INTERVAL)

    def _resolve(self) -> None:
        try:
            d = self.api.get_stun_addr(self.uid)
            addr = (d["IpcPrivateIP"], d["IpcPrivatePort"])
        except Exception:
            return
        if addr != self.addr:
            log.info("[%s] endpoint moved %s -> %s", self.uid, self.addr, addr)
            self.addr = addr
            self._endpoint_moves += 1
        self._last_resolve = time.monotonic()

    def _punch(self):
        while not self._stop.is_set():
            if time.monotonic() - self._last_rx > STARVED_THRESHOLD:
                self._resolve()
                try:
                    with self._sock_lock:
                        if self.sock:
                            self.sock.sendto(PUNCH, self.addr)
                except Exception:
                    pass
            try:
                with self._sock_lock:
                    if self.sock:
                        self.sock.sendto(PUNCH, self.addr)
            except Exception:
                pass
            self._stop.wait(PUNCH_INTERVAL)

    def _recovery(self):
        """Monitor stream health and trigger full re-rendezvous when dead."""
        while not self._stop.is_set():
            self._stop.wait(RECOVERY_DEAD_THRESHOLD)
            if self._stop.is_set():
                break

            if self.is_streaming:
                # Stream is alive — reset backoff
                self._backoff = RECOVERY_INITIAL_BACKOFF
                continue

            # Stream is dead
            dead_for = time.monotonic() - self._last_rx if self._last_rx else float('inf')

            # Don't re-rendezvous too often
            since_last = time.monotonic() - self._last_re_rendezvous
            if since_last < self._backoff:
                continue

            log.warning("[%s] stream dead %.0fs — re-rendezvous (backoff %.0fs)",
                        self.uid, dead_for, self._backoff)
            self._last_re_rendezvous = time.monotonic()
            self._re_rendezvous_count += 1

            if self._rendezvous():
                # Reset frame tracker
                self._frame_times.clear()
                # After re-rendezvous, wait a bit before checking again
                self._stop.wait(self._backoff)
                # Exponential backoff
                self._backoff = min(self._backoff * 2, RECOVERY_MAX_BACKOFF)
            else:
                log.error("[%s] re-rendezvous failed", self.uid)
                self._stop.wait(self._backoff)
                self._backoff = min(self._backoff * 2, RECOVERY_MAX_BACKOFF)

    @property
    def is_streaming(self) -> bool:
        return (time.monotonic() - self._last_rx) < 3.0

    @property
    def fps(self) -> float:
        now = time.monotonic()
        cutoff = now - 5.0
        count = sum(1 for t in self._frame_times if t > cutoff)
        return count / 5.0

    def _on_frame(self, jpeg: bytes) -> None:
        self.stats["frames"] += 1
        self._frame_times.append(time.monotonic())
        self.video.publish(jpeg)

    def _receive(self):
        asm = RtpJpegReassembler(self._on_frame)
        while not self._stop.is_set():
            # Get the current socket (may change during re-rendezvous)
            with self._sock_lock:
                sock = self.sock
            if not sock:
                self._stop.wait(0.5)
                continue
            sock.settimeout(0.5)
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                # Socket was closed (re-rendezvous in progress)
                self._stop.wait(0.5)
                continue
            if len(data) < 12:
                continue
            self._last_rx = time.monotonic()
            pt = data[1] & 0x7f
            if pt == 26:
                asm.feed(data)
            elif pt == 0:
                self.stats["audio_pkts"] += 1
                self.audio.publish(ulaw_to_pcm16(data[12:]))

    def _fps_logger(self):
        while not self._stop.is_set():
            self._stop.wait(FPS_LOG_INTERVAL)
            if self._stop.is_set():
                break
            fps = self.fps
            streaming = self.is_streaming
            moves = self._endpoint_moves
            rr = self._re_rendezvous_count
            if streaming:
                log.info("[%s] %.1f fps, %d frames total, %d endpoint moves, %d re-rendezvous",
                         self.uid, fps, self.stats["frames"], moves, rr)
            else:
                log.warning("[%s] NO STREAM — last rx %.0fs ago, %d moves, %d re-rendezvous",
                            self.uid, time.monotonic() - self._last_rx if self._last_rx else 0,
                            moves, rr)

    def health(self) -> dict:
        now = time.monotonic()
        return {
            "uid": self.uid,
            "streaming": self.is_streaming,
            "fps": round(self.fps, 1),
            "frames_total": self.stats["frames"],
            "audio_pkts": self.stats["audio_pkts"],
            "endpoint_moves": self._endpoint_moves,
            "re_rendezvous_count": self._re_rendezvous_count,
            "backoff_s": round(self._backoff, 1),
            "last_rx_ago_s": round(now - self._last_rx, 1) if self._last_rx else None,
            "addr": f"{self.addr[0]}:{self.addr[1]}" if self.addr else None,
        }

    def stop(self):
        self._stop.set()
        try:
            self.api.notify(self.uid, 0)
        except Exception:
            pass
        with self._sock_lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None


def make_handler(cameras: dict):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path.strip("/")
            if path in ("", "cameras"):
                self._index()
            elif path == "health":
                self._health()
            elif path.startswith("view/") and path[5:] in cameras:
                self._view(path[5:])
            elif path.startswith("cam/") and path[4:] in cameras:
                self._mjpeg(cameras[path[4:]])
            elif path.startswith("audio/") and path[6:] in cameras:
                self._wav(cameras[path[6:]])
            else:
                self.send_error(404)

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _index(self):
            self._send(json.dumps([{
                "uid": u,
                "view": f"/view/{u}",
                "video": f"/cam/{u}",
                "audio": f"/audio/{u}",
                "streaming": c.is_streaming,
                "fps": round(c.fps, 1),
                "stats": c.stats,
            } for u, c in cameras.items()], indent=2).encode(),
                "application/json")

        def _health(self):
            data = {
                "status": "ok" if any(c.is_streaming for c in cameras.values()) else "degraded",
                "cameras": [c.health() for c in cameras.values()],
            }
            self._send(json.dumps(data, indent=2).encode(), "application/json")

        def _view(self, uid: str):
            self._send(f"""<!doctype html><meta charset=utf-8>
<title>ZIOT {uid}</title>
<style>body{{background:#111;color:#ddd;font:14px system-ui;text-align:center;
padding:1rem}}img{{max-width:100%;border-radius:6px}}audio{{margin-top:1rem;width:640px;
max-width:100%}}</style>
<h3>ZIOT {uid}</h3>
<img src="/cam/{uid}" alt="live video">
<audio src="/audio/{uid}" controls autoplay></audio>
<p style="opacity:.6">Audio is G.711 u-law 8 kHz. Browsers block autoplay with
sound until you interact &mdash; press play if silent.</p>
""".encode(), "text/html; charset=utf-8")

        def _mjpeg(self, cam: ZiotCamera):
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                with cam.video.subscribe() as q:
                    while True:
                        try:
                            frame = q.get(timeout=10)
                        except queue.Empty:
                            continue
                        self.wfile.write(
                            b"--frame\r\nContent-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(frame)).encode()
                            + b"\r\n\r\n" + frame + b"\r\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _wav(self, cam: ZiotCamera):
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                self.wfile.write(wav_header())
                self.wfile.flush()
                with cam.audio.subscribe() as q:
                    while True:
                        try:
                            chunk = q.get(timeout=10)
                        except queue.Empty:
                            continue
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def local_ip_for(target: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="ZIOT RTP/JPEG -> MJPEG bridge")
    ap.add_argument("--config", default="ziot_config.json")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--list-cameras", action="store_true")
    ap.add_argument("--bind-ip", default=None,
                    help="LAN IP to stream from (must be on the camera's subnet)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    cfg = json.load(open(args.config))
    api = GPS555(cfg["token"])
    cams = api.list_cameras(cfg["user_id"])

    if args.list_cameras:
        print(f"\n{'UID':<16}  {'State':<6}  {'NAT':<6}  {'Relay':<22}  WiFi")
        print("-" * 74)
        for c in cams:
            print(f"{c['uid']:<16}  {c['connectionState']:<6}  {c['natType']:<6}  "
                  f"{c.get('relay_ip',''):<22}  {c.get('wifiSsid','')}")
        return

    wanted = set(cfg.get("cameras") or [])
    cams = [c for c in cams if not wanted or c["uid"] in wanted]
    if not cams:
        log.error("no cameras matched")
        return

    probe = api.get_stun_addr(cams[0]["uid"])["IpcPrivateIP"]
    bind_ip = args.bind_ip or local_ip_for(probe)
    log.info("binding on %s (camera LAN %s)", bind_ip, probe)

    live: dict[str, ZiotCamera] = {}
    lock = threading.Lock()

    def boot(cam_rec):
        z = ZiotCamera(api, cam_rec, bind_ip)
        if z.start():
            with lock:
                live[cam_rec["uid"]] = z

    threads = [threading.Thread(target=boot, args=(c,)) for c in cams]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if not live:
        log.error("no cameras started")
        return

    port = args.port or cfg.get("port", 5001)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(live))
    log.info("serving on http://0.0.0.0:%d/", port)
    for uid in live:
        log.info("  view  http://localhost:%d/view/%s", port, uid)
        log.info("  video http://localhost:%d/cam/%s", port, uid)
        log.info("  audio http://localhost:%d/audio/%s", port, uid)
    log.info("  health http://localhost:%d/health", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.shutdown()
        for z in live.values():
            z.stop()


if __name__ == "__main__":
    main()

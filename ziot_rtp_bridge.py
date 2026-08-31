#!/usr/bin/env python3
"""
ziot_rtp_bridge.py — v3, aligned with the decompiled vendor app
================================================================
v2 added auto-recovery: full re-rendezvous on endpoint move, a recovery thread
with exponential backoff (5s → 60s), socket recreation to avoid stale NAT
bindings, and a /health endpoint.

v3 follows a Dart-level decompilation of the vendor app (DECOMPILATION_REPORT.md):
  - Stopped sending cmdType "20" on every rendezvous. It is speakOff, not
    "start live"; the CameraCMDType enum has no live-view command at all.
  - CameraEventType is named, and teardown sends stop(3) instead of keepAlive(0).
  - Endpoint selection mirrors DeviceStunItem.ipAddress: the camera's LAN address
    when it shares our /24, otherwise its public address.
  - STUN answers are freshness-checked on seqNo, as the app does.
  - mediaState/onlineState are reduced to the app's own free/busy and on/off
    readings rather than being interpreted further.
  - RTSP relay fallback (RelayStream), the path the app uses when it cannot
    reach the camera directly.
  - Punch interval defaults to the app's 1s and is configurable.
"""
import argparse
import base64
import hashlib
import json
import logging
import queue
import re
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# API base mirrors the vendor app: http://ipc.gps555.net/api (TLS on)
BASE_URL = "https://ipc.gps555.net/api"
APP_ID = "b1ee47e92dfa22635907aa6bb882b1dc0ebc0285"
PUNCH = b"App send hello"
HEART = b"App send heart for stun"   # the app also sends this to keep NAT alive

# CameraEventType, recovered whole from the app's Dart snapshot. The wire value
# equals the enum index. We use start/keepAlive/stop; relay asks the cloud to
# open a forwarding session (see RelayStream).
EVENT_KEEPALIVE = 0
EVENT_START = 1
EVENT_PAUSE = 2
EVENT_STOP = 3
EVENT_CONNECTED = 4
EVENT_RELAY = 5

# CameraCMDType, also recovered whole. These are device *control* commands sent
# via POST /v1/cmd/send-cmd. None of them starts a live session -- live view is
# the STUN rendezvous plus the UDP hello, nothing more. In particular "20" is
# speakOff: an earlier revision of this bridge sent it on every rendezvous in the
# mistaken belief that it meant "start live".
CAMERA_CMD = {
    "restart": "1", "restore": "2", "light": "3", "sdCard": "4",
    "formatSDCard": "5", "firmwareOTA": "6", "infraredLight": "7",
    "originHorizontal": "8", "originVertical": "9",
    "ptzUp": "10", "ptzDown": "11", "ptzLeft": "12", "ptzRight": "13",
    "ptzAlwaysUp": "14", "ptzAlwaysDown": "15", "ptzAlwaysLeft": "16",
    "ptzAlwaysRight": "17", "ptzMoveStop": "18",
    "speakOn": "19", "speakOff": "20", "lampLight": "21",
    "definition": "22", "ptzReset": "23", "sensitivity": "25",
}

KEEPALIVE_INTERVAL = 2
# The app's punch loop is Timer.periodic(1s) in StunManager::_startP2PConnect.
# Overridable per deployment via "punch_interval" in the config.
PUNCH_INTERVAL = 1.0
# The app recomputes its stun-heart interval at runtime rather than holding it
# fixed, so this ratio is ours: one heart every N punches.
HEART_EVERY_N_PUNCHES = 5
# Must stay above PUNCH_INTERVAL -- if you retune one, look at the other.
STARVED_THRESHOLD = 2.0        # re-resolve after 2s of silence
FPS_LOG_INTERVAL = 30           # log fps every 30s
RECOVERY_DEAD_THRESHOLD = 10.0  # start recovery after 10s dead
RECOVERY_MAX_BACKOFF = 60.0     # max wait between re-rendezvous attempts
RECOVERY_INITIAL_BACKOFF = 5.0  # first retry after 5s
STATUS_INTERVAL = 30            # refresh onlineState/mediaState from the cloud
STATUS_FAIL_WARN = 3            # consecutive refresh failures before warning
RELAY_AFTER_FAILURES = 3        # failed direct rendezvous before trying the relay
RELAY_RETRY_DIRECT = 120.0      # while relayed, retry a direct rendezvous this often
RELAY_FIRMWARE_PIVOT = "TXW817_A_V1.0.11.52"   # app's CameraInfoModel gate
RELAY_DEFAULT_PORT = 554
STUN_SUBNET_MASK = "255.255.255.0"   # the app's isSameSubnet() mask

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


def same_subnet(a: str, b: str, mask: str = STUN_SUBNET_MASK) -> bool:
    """The app's isSameSubnet(): compare two IPv4 addresses under a netmask.

    DeviceStunItem.ipAddress uses this with 255.255.255.0 to decide whether to
    talk to the camera's LAN address or its public one.
    """
    try:
        ia, ib, im = (struct.unpack("!I", socket.inet_aton(x))[0] for x in (a, b, mask))
    except OSError:
        return False
    return (ia & im) == (ib & im)


_VERSION_RE = re.compile(r"V?(\d+(?:\.\d+)+)")


def version_lt(current: str, target: str) -> bool:
    r"""True when `current` is older than `target`.

    Mirrors CameraInfoModel::deviceNeedUpdate, which pulls the first
    `V?(\d+(\.\d+)+)` out of each string and compares component-wise. Firmware
    reads like "TXW817_A_V1.0.12.32", so the leading "817" is skipped -- it is
    not followed by a dotted group.
    """
    def parts(v: str) -> list[int]:
        m = _VERSION_RE.search(v or "")
        return [int(x) for x in m.group(1).split(".")] if m else []

    pc, pt = parts(current), parts(target)
    if not pc or not pt:
        return False        # unparseable: assume no update needed, like the app
    width = max(len(pc), len(pt))
    pc += [0] * (width - len(pc))
    pt += [0] * (width - len(pt))
    return pc < pt


def _flag(row: dict, key: str) -> str:
    """Normalise a cloud state flag the way the app does: null/empty -> "0"."""
    v = row.get(key)
    if v is None:
        return ""
    v = str(v)
    return v or "0"


def cloud_is_on(row: dict) -> bool:
    """CameraInfoModel::isOn -- onlineState == "1", null being false."""
    v = _flag(row, "onlineState")
    return bool(v) and v == "1"


def cloud_is_free(row: dict) -> bool:
    """CameraInfoModel::isFree -- mediaState == "0", null being false.

    This is the app's *only* use of mediaState. It never distinguishes 1 from 3,
    so neither do we: the flag is free/busy and nothing finer.
    """
    v = _flag(row, "mediaState")
    return bool(v) and v == "0"


def relay_urls(row: dict) -> list[str]:
    """Candidate relay RTSP URLs for a device-list row, best guess first.

    CameraInfoModel::relayPath builds one of two forms, chosen by
    _supportRelayPath():

        rtsp://<host>/rtp/<last 8 of uid>   when _supportRelayPath() is true
        rtsp://<host>/live/<uid>            otherwise

    and _supportRelayPath() is `deviceNeedUpdate(version, RELAY_FIRMWARE_PIVOT)`
    for CameraType.TaiXinX5 ("X5"), or an unconditional true for every other
    device type. So an X5 on firmware at or past the pivot takes /live/<uid>.

    Both forms are returned regardless -- the ordering is an informed guess and
    the caller falls back to the second if the first will not play.
    """
    host = str(row.get("relay_ip") or "").strip()
    if not host:
        return []
    if ":" not in host:
        host = f"{host}:{RELAY_DEFAULT_PORT}"
    uid = str(row.get("uid") or "")
    if not uid:
        return []

    is_x5 = str(row.get("deviceType") or "") == "X5"
    old_form = version_lt(str(row.get("version") or ""), RELAY_FIRMWARE_PIVOT) \
        if is_x5 else True

    rtp = f"rtsp://{host}/rtp/{uid[-8:]}"
    live = f"rtsp://{host}/live/{uid}"
    return [rtp, live] if old_form else [live, rtp]


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
        return self._get("/v1/ipc", terminalFamilyId=user_id)["data"]["list"]

    def send_stun_addr(self, uid, ip, port):
        return self._get("/v1/ipc/send-stun-addr", appId=APP_ID, uid=uid,
                         publicIp=ip, publicPort=port,
                         privateIp=ip, privatePort=port)["data"]

    def get_stun_addr(self, uid):
        return self._get(f"/v1/ipc/stun-addr/{uid}")["data"]

    def notify(self, uid, event_type: int):
        """GET /v1/ipc/notify-live-event — event_type is a CameraEventType value
        (EVENT_START, EVENT_KEEPALIVE, EVENT_STOP, EVENT_RELAY, ...)."""
        self._get("/v1/ipc/notify-live-event",
                  appId=APP_ID, eventType=event_type, uid=uid)

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            BASE_URL + path, data=json.dumps(body).encode(),
            headers={**self._h, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def send_cmd(self, uid: str, cmd_type: str) -> dict:
        """POST /v1/cmd/send-cmd — a device control command (see CAMERA_CMD).

        Deliberately NOT part of rendezvous. The enum holds no live-view command;
        an earlier revision sent cmdType "20" here believing it meant "start
        live", when it is speakOff. Pass a CAMERA_CMD value to drive PTZ, the
        lights, the speaker or a restart.
        """
        if cmd_type not in CAMERA_CMD.values():
            raise ValueError(f"unknown cmdType {cmd_type!r}; see CAMERA_CMD")
        return self._post("/v1/cmd/send-cmd", {"cmdType": cmd_type, "uid": uid})


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


class RelayStream:
    """RTSP client for the vendor's forwarding relay.

    The app falls back to this when it cannot reach the camera directly:
    CameraNet.requestSendRelay fires notify-live-event(relay), then
    CameraControllerMove.setupRelayVideo plays the URL from
    CameraInfoModel.relayPath. See relay_urls() for the two URL forms.

    Transport is RTP interleaved over the RTSP TCP connection. That needs no
    second port and no inbound UDP, which is the whole point of using the relay.
    """

    def __init__(self, url: str, on_frame, on_audio, on_rx,
                 stop_event: threading.Event, tag: str,
                 user: str = None, password: str = None, timeout: float = 10.0):
        self.url = url
        self.tag = tag
        self._on_frame = on_frame
        self._on_audio = on_audio
        self._on_rx = on_rx
        self._stop = stop_event
        self._user = user
        self._password = password
        self._timeout = timeout

        self._sock = None
        self._buf = b""
        self._cseq = 0
        self._session = None
        self._session_timeout = 60.0
        self._auth = None               # cached Authorization header value
        # interleaved channel -> ("video"|"audio")
        self._channels: dict[int, str] = {}
        self._asm = RtpJpegReassembler(on_frame)

    # ---- low-level socket helpers ------------------------------------------

    def _recv_some(self) -> bool:
        try:
            chunk = self._sock.recv(65536)
        except socket.timeout:
            return True                 # idle, not an error
        except OSError:
            return False
        if not chunk:
            return False
        self._buf += chunk
        return True

    def _read_line_block(self) -> bytes:
        """Read up to and including a blank line (an RTSP message head)."""
        deadline = time.monotonic() + self._timeout
        while b"\r\n\r\n" not in self._buf:
            if self._stop.is_set() or not self._recv_some():
                raise ConnectionError("relay closed during header read")
            if time.monotonic() > deadline:
                # recv() timing out is not an error on its own, so without this
                # a server that goes mute would spin here forever.
                raise ConnectionError("relay went quiet mid-header")
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest
        return head

    def _read_exact(self, n: int) -> bytes:
        deadline = time.monotonic() + self._timeout
        while len(self._buf) < n:
            if self._stop.is_set() or not self._recv_some():
                raise ConnectionError("relay closed during body read")
            if time.monotonic() > deadline:
                raise ConnectionError("relay went quiet mid-body")
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    # ---- RTSP ---------------------------------------------------------------

    def _auth_header(self, method: str, uri: str, challenge: str) -> str:
        """Build an Authorization value for a WWW-Authenticate challenge."""
        scheme = challenge.split(None, 1)[0].lower()
        user = self._user or ""
        pw = self._password or ""
        if scheme == "basic":
            raw = base64.b64encode(f"{user}:{pw}".encode()).decode()
            return f"Basic {raw}"
        if scheme == "digest":
            fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
            realm = fields.get("realm", "")
            nonce = fields.get("nonce", "")
            def md5(x: str) -> str:
                return hashlib.md5(x.encode()).hexdigest()
            ha1 = md5(f"{user}:{realm}:{pw}")
            ha2 = md5(f"{method}:{uri}")
            resp = md5(f"{ha1}:{nonce}:{ha2}")
            return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
                    f'uri="{uri}", response="{resp}"')
        raise ValueError(f"unsupported auth scheme {scheme!r}")

    def _request(self, method: str, uri: str = None, headers: dict = None) -> tuple:
        """Send one RTSP request and return (status, headers, body)."""
        uri = uri or self.url
        headers = dict(headers or {})
        self._cseq += 1
        headers["CSeq"] = str(self._cseq)
        headers["User-Agent"] = "ziot-rtp-bridge"
        if self._session:
            headers["Session"] = self._session
        if self._auth:
            headers["Authorization"] = self._auth

        def send():
            lines = [f"{method} {uri} RTSP/1.0"]
            lines += [f"{k}: {v}" for k, v in headers.items()]
            self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        send()
        status, hdrs, body = self._read_response(method)

        if status == 401 and not self._auth and "www-authenticate" in hdrs:
            if self._user is None:
                raise PermissionError(
                    "relay demands authentication but no credentials are "
                    "configured (set relay_user/relay_pass in the config)")
            self._auth = self._auth_header(method, uri, hdrs["www-authenticate"])
            headers["Authorization"] = self._auth
            self._cseq += 1
            headers["CSeq"] = str(self._cseq)
            send()
            status, hdrs, body = self._read_response(method)
        return status, hdrs, body

    def _read_response(self, method: str = "request") -> tuple:
        """Read one RTSP response, skipping any interleaved media in front of it.

        `method` is only used in error text, but naming the stalled request
        matters: a relay that answers OPTIONS and then hangs on DESCRIBE is a
        publisher-wait (nobody is sending it media), which is a completely
        different diagnosis from one that never speaks at all.
        """
        deadline = time.monotonic() + self._timeout
        while True:
            while not self._buf:
                if self._stop.is_set() or not self._recv_some():
                    raise ConnectionError(f"relay closed while awaiting {method}")
                if time.monotonic() > deadline:
                    # A relay that accepts the connection but never answers and
                    # never closes would otherwise spin here: recv() timing out
                    # is reported as idle, not as failure.
                    raise ConnectionError(
                        f"relay did not answer {method} within {self._timeout:.0f}s")
            if self._buf[:1] == b"$":
                self._consume_interleaved()
                continue
            head = self._read_line_block().decode("utf8", "replace")
            lines = head.split("\r\n")
            try:
                status = int(lines[0].split()[1])
            except (IndexError, ValueError):
                raise ConnectionError(f"malformed RTSP status line: {lines[0]!r}")
            hdrs = {}
            for ln in lines[1:]:
                k, _, v = ln.partition(":")
                if k:
                    hdrs[k.strip().lower()] = v.strip()
            body = b""
            if "content-length" in hdrs:
                body = self._read_exact(int(hdrs["content-length"]))
            return status, hdrs, body

    def _consume_interleaved(self) -> None:
        """Read one `$<channel><len:2><rtp>` frame and dispatch it."""
        hdr = self._read_exact(4)
        channel = hdr[1]
        length = struct.unpack("!H", hdr[2:4])[0]
        packet = self._read_exact(length)
        kind = self._channels.get(channel)
        if not kind or len(packet) < 12:
            return
        self._on_rx()
        if kind == "video":
            self._asm.feed(packet)
        else:
            self._on_audio(ulaw_to_pcm16(packet[12:]))

    # ---- SDP ----------------------------------------------------------------

    def _parse_sdp(self, body: bytes) -> list:
        """Return [(kind, payload_type, control_url)] for the media we can use.

        Payload types are read, not assumed: the relay is free to renumber.
        """
        tracks = []
        kind = pt = None
        control = None
        for raw in body.decode("utf8", "replace").splitlines():
            line = raw.strip()
            if line.startswith("m="):
                if kind:
                    tracks.append((kind, pt, control))
                parts = line[2:].split()
                kind, pt, control = None, None, None
                media = parts[0]
                fmts = parts[3:]
                try:
                    pt = int(fmts[0])
                except (IndexError, ValueError):
                    pt = None
                if media == "video":
                    kind = "video"
                elif media == "audio":
                    kind = "audio"
                else:
                    kind = "other"
            elif line.startswith("a=control:") and kind:
                control = line[len("a=control:"):].strip()
        if kind:
            tracks.append((kind, pt, control))
        return [t for t in tracks if t[0] in ("video", "audio")]

    def _track_url(self, control: str) -> str:
        if not control or control == "*":
            return self.url
        if control.lower().startswith("rtsp://"):
            return control
        return self.url.rstrip("/") + "/" + control.lstrip("/")

    # ---- lifecycle ----------------------------------------------------------

    def open(self) -> bool:
        """Run OPTIONS/DESCRIBE/SETUP/PLAY. True once media should be flowing."""
        parts = urllib.parse.urlsplit(self.url)
        host = parts.hostname
        port = parts.port or RELAY_DEFAULT_PORT
        self._sock = socket.create_connection((host, port), timeout=self._timeout)
        self._sock.settimeout(self._timeout)

        status, _, _ = self._request("OPTIONS")
        if status != 200:
            log.warning("[%s] relay OPTIONS -> %s", self.tag, status)
            return False

        try:
            status, hdrs, body = self._request(
                "DESCRIBE", headers={"Accept": "application/sdp"})
        except ConnectionError as e:
            # OPTIONS succeeded to get here, so the relay is alive and talking.
            # A stall on DESCRIBE is ZLMediaKit waiting for a publisher: the
            # camera is not sending it media, and no client can conjure that.
            log.warning("[%s] relay is up but has no stream to serve "
                        "(OPTIONS answered, DESCRIBE did not: %s). The camera "
                        "is not publishing to it.", self.tag, e)
            return False
        if status != 200 or not body:
            log.warning("[%s] relay DESCRIBE -> %s (%d bytes) — relay reachable "
                        "but no media for this URL", self.tag, status, len(body))
            return False

        tracks = self._parse_sdp(body)
        if not tracks:
            log.warning("[%s] relay SDP carried no video or audio track", self.tag)
            return False

        channel = 0
        for kind, pt, control in tracks:
            if kind == "video" and pt is not None and pt != 26:
                log.warning("[%s] relay video payload type %d is not JPEG/RFC2435 "
                            "— frames will not decode", self.tag, pt)
            if kind == "audio" and pt is not None and pt != 0:
                log.warning("[%s] relay audio payload type %d is not PCMU "
                            "— skipping this track", self.tag, pt)
                continue
            transport = (f"RTP/AVP/TCP;unicast;interleaved={channel}-{channel + 1}")
            status, hdrs, _ = self._request(
                "SETUP", uri=self._track_url(control),
                headers={"Transport": transport})
            if status != 200:
                log.warning("[%s] relay SETUP %s -> %s", self.tag, kind, status)
                return False
            if "session" in hdrs:
                sess = hdrs["session"]
                self._session = sess.split(";")[0].strip()
                m = re.search(r"timeout=(\d+)", sess)
                if m:
                    self._session_timeout = float(m.group(1))
            self._channels[channel] = kind
            channel += 2

        if not self._channels:
            log.warning("[%s] relay offered nothing we can play", self.tag)
            return False

        status, _, _ = self._request("PLAY", headers={"Range": "npt=0.000-"})
        if status != 200:
            log.warning("[%s] relay PLAY -> %s", self.tag, status)
            return False

        log.info("[%s] relay playing %s (%s)", self.tag, self.url,
                 ", ".join(f"ch{c}={k}" for c, k in sorted(self._channels.items())))
        return True

    def pump(self) -> None:
        """Read interleaved media until stopped or the relay drops us."""
        keepalive_every = max(5.0, self._session_timeout / 2)
        last_keepalive = time.monotonic()
        self._sock.settimeout(1.0)
        try:
            while not self._stop.is_set():
                while self._buf and self._buf[:1] != b"$":
                    # An unsolicited response (our keepalive's reply, usually).
                    self._read_response()
                if self._buf[:1] == b"$":
                    self._consume_interleaved()
                elif not self._recv_some():
                    log.warning("[%s] relay connection closed", self.tag)
                    return
                if time.monotonic() - last_keepalive > keepalive_every:
                    last_keepalive = time.monotonic()
                    self._cseq += 1
                    self._sock.sendall(
                        f"OPTIONS {self.url} RTSP/1.0\r\nCSeq: {self._cseq}\r\n"
                        f"Session: {self._session}\r\n\r\n".encode())
        except (ConnectionError, OSError) as e:
            log.warning("[%s] relay stream ended: %s", self.tag, e)

    def close(self) -> None:
        if not self._sock:
            return
        try:
            if self._session:
                self._request("TEARDOWN")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
        self._sock = None


class ZiotCamera:
    def __init__(self, api: GPS555, cam: dict, bind_ip: str,
                 force_relay: bool = False, relay_user: str = None,
                 relay_pass: str = None):
        self.api = api
        self.uid = cam["uid"]
        self.bind_ip = bind_ip
        # Cloud-reported state from the device list (raw, as the app sees them).
        # Refreshed by the shared CloudState poller, not by this camera.
        self.cloud: dict = {}
        self._cloud_at = 0.0
        self._load_cloud(cam)
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
        # Endpoint selection + STUN freshness (mirrors DeviceStunItem)
        self._endpoint_kind = None      # "private" | "public"
        self._stun_seq = None
        self._stun_update = None
        # Relay fallback
        self._consec_failures = 0
        self._relay = None              # RelayStream while relayed
        self._relay_url = None
        self.force_relay = force_relay
        self._relay_user = relay_user
        self._relay_pass = relay_pass
        self._relay_last_direct_try = 0.0

    def _load_cloud(self, cam: dict) -> None:
        keys = ["onlineState", "mediaState", "connectionState", "natType",
                "relay_ip", "server_ip", "commTime", "signal", "power", "wifiSsid",
                # deviceType and version drive the relay URL form (relay_urls)
                "deviceType", "version"]
        # Rebind rather than mutate: HTTP handler threads read this unlocked.
        self.cloud = {k: cam.get(k) for k in keys}
        self._cloud_at = time.monotonic()

    def update_cloud(self, cam: dict) -> None:
        """Take a fresh device-list row from CloudState, logging transitions."""
        old = self.cloud
        self._load_cloud(cam)
        if old.get("onlineState") != self.cloud["onlineState"] or \
                old.get("mediaState") != self.cloud["mediaState"]:
            log.info("[%s] cloud state %s/%s -> %s/%s (relay %s)",
                     self.uid, old.get("onlineState"), old.get("mediaState"),
                     self.cloud["onlineState"], self.cloud["mediaState"],
                     self.cloud.get("relay_ip"))

    def _accept_stun(self, d: dict) -> bool:
        """Reject a STUN answer older than the last one we took.

        The app validates the same way (_isValidStunResponse, and its
        "stun 有效/无效: seqNo" logging). Only seqNo gates: updateTime is kept for
        diagnostics, because a server that never advances it would otherwise
        wedge us.
        """
        upd = d.get("updateTime")
        if upd is not None:
            self._stun_update = upd

        try:
            seq = int(d["seqNo"])
        except (KeyError, TypeError, ValueError):
            return True     # no usable seqNo: accept rather than stall

        if self._stun_seq is not None and seq < self._stun_seq:
            log.warning("[%s] stale stun rejected: seqNo %s < %s",
                        self.uid, seq, self._stun_seq)
            return False
        self._stun_seq = seq
        return True

    def _pick_endpoint(self, d: dict):
        """Choose the camera's LAN or public address, as DeviceStunItem.ipAddress does.

        Same /24 as our bind address -> the private pair, otherwise the public
        pair. The app logs this decision too; so do we, once per change.
        """
        priv_ip, priv_port = d.get("IpcPrivateIP"), d.get("IpcPrivatePort")
        pub_ip, pub_port = d.get("IpcPublicIP"), d.get("IpcPublicPort")

        def port(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        priv = (priv_ip, port(priv_port))
        pub = (pub_ip, port(pub_port))

        if all(priv) and same_subnet(self.bind_ip, priv_ip):
            return priv[0], priv[1], "private"
        if all(pub):
            return pub[0], pub[1], "public"
        # No usable public pair — the private one is all there is.
        if all(priv):
            return priv[0], priv[1], "private"
        raise KeyError("stun response carries no usable address")

    def _fetch_endpoint(self):
        """One STUN lookup: fetch, freshness-check, then select an address."""
        d = self.api.get_stun_addr(self.uid)
        if not self._accept_stun(d):
            return None
        return self._pick_endpoint(d)

    def _note_endpoint(self, kind: str) -> None:
        if kind != self._endpoint_kind:
            log.info("[%s] using %s endpoint (we bind %s, mask %s)",
                     self.uid, kind, self.bind_ip, STUN_SUBNET_MASK)
            self._endpoint_kind = kind

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

        # Register our port with the cloud. No send-cmd here: the CameraCMDType
        # enum has no live-view command, and the "20" this used to send is
        # speakOff. Rendezvous is send-stun-addr -> notify(start) -> hello.
        for _ in range(3):
            try:
                self.api.send_stun_addr(self.uid, self.bind_ip, port)
                break
            except Exception as e:
                log.warning("[%s] send-stun-addr: %s", self.uid, e)

        # Wake the camera
        try:
            self.api.notify(self.uid, EVENT_START)
        except Exception as e:
            log.warning("[%s] notify(start): %s", self.uid, e)

        # A full re-rendezvous restarts the STUN conversation, so an older seqNo
        # from the camera's side is expected rather than stale.
        self._stun_seq = None

        # Get camera address
        addr = None
        for _ in range(5):
            try:
                picked = self._fetch_endpoint()
                if picked:
                    ip, prt, kind = picked
                    self._note_endpoint(kind)
                    addr = (ip, prt)
                    break
            except Exception:
                pass
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

    @property
    def relayed(self) -> bool:
        return self._relay is not None

    @property
    def mode(self) -> str:
        """"relay", "direct", or "down" — down meaning no transport is open."""
        if self.relayed:
            return "relay"
        return "direct" if (self.sock and self.addr) else "down"

    def _mark_rx(self) -> None:
        self._last_rx = time.monotonic()

    def _on_relay_audio(self, pcm: bytes) -> None:
        self.stats["audio_pkts"] += 1
        self.audio.publish(pcm)

    def _start_relay(self) -> bool:
        """Open the vendor relay, trying both URL forms.

        Which form is right depends on a firmware gate whose direction we infer
        rather than know, so relay_urls() hands us both and we take whichever
        actually plays.
        """
        urls = relay_urls({**self.cloud, "uid": self.uid})
        if not urls:
            log.warning("[%s] no relay_ip in the device list — relay unavailable",
                        self.uid)
            return False
        try:
            self.api.notify(self.uid, EVENT_RELAY)
        except Exception as e:
            log.warning("[%s] notify(relay): %s", self.uid, e)

        for url in urls:
            stream = RelayStream(url, self._on_frame, self._on_relay_audio,
                                 self._mark_rx, self._stop, self.uid,
                                 user=self._relay_user, password=self._relay_pass)
            try:
                if stream.open():
                    self._relay = stream
                    self._relay_url = url
                    threading.Thread(target=self._relay_pump, args=(stream,),
                                     daemon=True).start()
                    return True
            except PermissionError as e:
                log.error("[%s] relay: %s", self.uid, e)
                stream.close()
                return False
            except Exception as e:
                log.warning("[%s] relay %s failed: %s", self.uid, url, e)
            stream.close()
        log.error("[%s] no relay URL form played (tried %s)",
                  self.uid, ", ".join(urls))
        return False

    def _relay_pump(self, stream: "RelayStream") -> None:
        try:
            stream.pump()
        finally:
            stream.close()
            if self._relay is stream:
                self._relay = None
                self._relay_url = None

    def _stop_relay(self) -> None:
        stream, self._relay = self._relay, None
        self._relay_url = None
        if stream:
            stream.close()

    def _try_return_direct(self) -> None:
        """While relayed, occasionally re-attempt the direct path.

        Direct is lower latency and does not depend on vendor infrastructure, so
        it is worth reclaiming. The relay is dropped before the trial, which
        costs a short gap; if direct then fails, the normal failure counter
        brings the relay straight back.
        """
        if self.force_relay:
            return          # told to stay on the relay; never probe direct
        now = time.monotonic()
        if now - self._relay_last_direct_try < RELAY_RETRY_DIRECT:
            return
        self._relay_last_direct_try = now
        log.info("[%s] relayed — retrying the direct path", self.uid)
        self._stop_relay()
        self._last_rx = 0.0
        try:
            regained = self._rendezvous()
        except Exception as e:
            log.error("[%s] direct retry failed: %s", self.uid, e)
            regained = False
        if regained:
            self._consec_failures = 0
            self._backoff = RECOVERY_INITIAL_BACKOFF
            log.info("[%s] back on the direct path", self.uid)
        else:
            self._consec_failures = RELAY_AFTER_FAILURES
            self._start_relay()

    def _open_transport(self) -> bool:
        """One attempt at whichever transport this camera is configured for.

        Never raises. `_rendezvous()` binds a socket, which throws for an
        address that is not (or is no longer) local — `--bind-ip` on a downed
        interface, say. Letting that escape would kill whichever thread called
        us: at startup the camera would never be registered, and from the
        recovery loop the thread would die silently and the camera would never
        retry again. A failed attempt is a False, not an exception.
        """
        try:
            return self._start_relay() if self.force_relay else self._rendezvous()
        except Exception as e:
            log.error("[%s] transport attempt failed: %s", self.uid, e)
            return False

    def start(self) -> bool:
        """Bring the camera up and start its threads.

        The threads start whether or not the first attempt succeeds: these
        cameras spend a lot of time cloud-offline, and a bridge that gave up at
        startup would stay down until someone noticed. `_recovery` retries on
        the usual backoff, so a camera that is merely offline right now joins in
        when it returns. The return value says whether the *first* attempt
        worked, for logging — it is not a reason to discard the camera.
        """
        try:
            ok = self._open_transport()
            if not ok:
                if self.force_relay:
                    log.error("[%s] --force-relay: no relay URL answered. "
                              "Serving anyway and retrying every %.0fs — the "
                              "direct path will NOT be tried while "
                              "--force-relay is set.",
                              self.uid, RECOVERY_DEAD_THRESHOLD)
                else:
                    log.warning("[%s] initial rendezvous failed — serving "
                                "anyway and retrying in the background",
                                self.uid)
        finally:
            # In a finally so the threads exist even if the first attempt blew
            # up unexpectedly. Registering a camera with no recovery thread
            # would put it on /health and then never retry it, which is worse
            # than dropping it: it would look present but be permanently dead.
            for fn in (self._keepalive, self._punch, self._receive,
                       self._fps_logger, self._recovery):
                threading.Thread(target=fn, daemon=True).start()
        return ok

    def _keepalive(self):
        while not self._stop.is_set():
            try:
                self.api.notify(self.uid, EVENT_KEEPALIVE)
            except Exception:
                pass
            self._stop.wait(KEEPALIVE_INTERVAL)

    def _resolve(self) -> None:
        try:
            picked = self._fetch_endpoint()
        except Exception:
            return
        if not picked:
            return                      # stale answer, keep the current address
        ip, prt, kind = picked
        self._note_endpoint(kind)
        addr = (ip, prt)
        if addr != self.addr:
            log.info("[%s] endpoint moved %s -> %s", self.uid, self.addr, addr)
            self.addr = addr
            self._endpoint_moves += 1
        self._last_resolve = time.monotonic()

    def _punch(self):
        tick = 0
        while not self._stop.is_set():
            if self.relayed or self.force_relay:
                # The relay owns the media. Punching would churn the STUN API
                # and log endpoint moves for a path we are not using — and
                # under --force-relay we must not touch the direct path at all.
                self._stop.wait(PUNCH_INTERVAL)
                continue
            tick += 1
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
                        # The app also sends this; keeps NAT/relay mappings warm
                        if tick % HEART_EVERY_N_PUNCHES == 0:
                            self.sock.sendto(HEART, self.addr)
            except Exception:
                pass
            self._stop.wait(PUNCH_INTERVAL)

    def _recovery(self):
        """Monitor stream health and trigger full re-rendezvous when dead."""
        while not self._stop.is_set():
            self._stop.wait(RECOVERY_DEAD_THRESHOLD)
            if self._stop.is_set():
                break
            try:
                self._recovery_tick()
            except Exception:
                # This thread is the only thing that will ever bring the camera
                # back; it must not die on an unexpected error.
                log.exception("[%s] recovery tick failed", self.uid)

    def _recovery_tick(self):
        """One pass of the recovery loop. `return` here means "done for now"."""
        if self.relayed:
            if not self.is_streaming:
                # Negotiated fine but no media is arriving. Don't sit on it
                # until the direct-retry timer comes round.
                log.warning("[%s] relay is silent — dropping it", self.uid)
                self._stop_relay()
                self._relay_last_direct_try = 0.0
                return
            self._try_return_direct()
            return

        if self.is_streaming:
            # Stream is alive — reset backoff
            self._backoff = RECOVERY_INITIAL_BACKOFF
            self._consec_failures = 0
            return

        # Stream is dead
        dead_for = time.monotonic() - self._last_rx if self._last_rx else float('inf')

        # Don't re-rendezvous too often
        since_last = time.monotonic() - self._last_re_rendezvous
        if since_last < self._backoff:
            return

        what = "relay" if self.force_relay else "re-rendezvous"
        log.warning("[%s] stream dead %.0fs — %s (backoff %.0fs)",
                    self.uid, dead_for, what, self._backoff)
        self._last_re_rendezvous = time.monotonic()
        self._re_rendezvous_count += 1

        if self._open_transport():
            # Reset frame tracker
            self._frame_times.clear()
            # After re-rendezvous, wait a bit before checking again
            self._stop.wait(self._backoff)
            if self.is_streaming:
                self._consec_failures = 0
            else:
                self._consec_failures += 1
            # Exponential backoff
            self._backoff = min(self._backoff * 2, RECOVERY_MAX_BACKOFF)
        else:
            log.error("[%s] %s failed", self.uid, what)
            self._consec_failures += 1
            self._stop.wait(self._backoff)
            self._backoff = min(self._backoff * 2, RECOVERY_MAX_BACKOFF)

        # The direct path is not coming back — try the vendor relay, which
        # is what the app does when it cannot reach the camera itself.
        if (self._consec_failures >= RELAY_AFTER_FAILURES
                and not self.relayed and not self.force_relay):
            log.warning("[%s] %d direct attempts failed — trying the relay",
                        self.uid, self._consec_failures)
            self._relay_last_direct_try = time.monotonic()
            if self._start_relay():
                self._frame_times.clear()

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
            if self.relayed:
                self._stop.wait(0.5)
                continue
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
            "mode": self.mode,
            "relay_url": self._relay_url,
            "endpoint_kind": self._endpoint_kind,
            "stun_seq": self._stun_seq,
            "stun_update": self._stun_update,
            "online_state": self.cloud.get("onlineState"),
            "media_state": self.cloud.get("mediaState"),
            # The app reduces these two flags to exactly these booleans.
            "online": cloud_is_on(self.cloud),
            "media_free": cloud_is_free(self.cloud),
            "cloud_relay": self.cloud.get("relay_ip"),
            "cloud_age_s": round(now - self._cloud_at, 1),
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
        self._stop_relay()
        try:
            # The app sends CameraEventType.stop when it tears a session down.
            # We used to send keepAlive here, which left the session hanging.
            self.api.notify(self.uid, EVENT_STOP)
        except Exception:
            pass
        with self._sock_lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None


class CloudState:
    """One device-list poller for the whole account.

    The device-list endpoint returns every camera on the account, so polling it
    from each ZiotCamera would issue N identical requests for the same payload
    and throw away all but one row of each. Cameras register here instead and
    are handed their own row.
    """

    def __init__(self, api: GPS555, user_id: int):
        self.api = api
        self.user_id = user_id
        self._cams: dict[str, "ZiotCamera"] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._fails = 0

    def register(self, cam: "ZiotCamera") -> None:
        with self._lock:
            self._cams[cam.uid] = cam

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            self._stop.wait(STATUS_INTERVAL)
            if self._stop.is_set():
                break
            self.poll()

    def poll(self) -> None:
        try:
            rows = self.api.list_cameras(self.user_id)
        except Exception as e:
            self._fails += 1
            # Stale flags served as if fresh are worse than no flags at all, so
            # say something once we've missed enough polls to matter.
            if self._fails == STATUS_FAIL_WARN:
                log.warning("cloud state refresh failing (%d in a row) — "
                            "online/media flags are now stale: %s", self._fails, e)
            else:
                log.debug("cloud state refresh: %s", e)
            return
        if self._fails >= STATUS_FAIL_WARN:
            log.info("cloud state refresh recovered after %d failures", self._fails)
        self._fails = 0
        by_uid = {r.get("uid"): r for r in rows}
        with self._lock:
            cams = list(self._cams.values())
        for cam in cams:
            row = by_uid.get(cam.uid)
            if row:
                cam.update_cloud(row)


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
                "online_state": c.cloud.get("onlineState"),
                "media_state": c.cloud.get("mediaState"),
                "online": cloud_is_on(c.cloud),
                "media_free": cloud_is_free(c.cloud),
                "mode": c.mode,
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
    ap.add_argument("--force-relay", action="store_true",
                    help="skip the direct path and stream via the vendor RTSP "
                         "relay (for exercising that path on demand)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    cfg = json.load(open(args.config))

    global PUNCH_INTERVAL
    PUNCH_INTERVAL = float(cfg.get("punch_interval", PUNCH_INTERVAL))
    if PUNCH_INTERVAL >= STARVED_THRESHOLD:
        log.warning("punch_interval %.1fs is not below STARVED_THRESHOLD %.1fs — "
                    "starved-stream detection will be sluggish",
                    PUNCH_INTERVAL, STARVED_THRESHOLD)

    api = GPS555(cfg["token"])
    cams = api.list_cameras(cfg["user_id"])

    if args.list_cameras:
        print(f"\n{'UID':<16}  {'Online':<6}  {'Media':<5}  {'Free':<5}  "
              f"{'State':<6}  {'NAT':<6}  {'Relay':<22}  WiFi")
        print("-" * 98)
        for c in cams:
            # str() everything: the API is not consistent about quoting these.
            # Online/Free are the app's own isOn/isFree readings of the two flags.
            print(f"{c['uid']:<16}  {str(c.get('onlineState','?')):<6}  "
                  f"{str(c.get('mediaState','?')):<5}  "
                  f"{('yes' if cloud_is_free(c) else 'no'):<5}  "
                  f"{str(c.get('connectionState','?')):<6}  "
                  f"{str(c.get('natType','?')):<6}  "
                  f"{str(c.get('relay_ip','')):<22}  {c.get('wifiSsid','')}")
        return

    wanted = set(cfg.get("cameras") or [])
    cams = [c for c in cams if not wanted or c["uid"] in wanted]
    only_online = bool(cfg.get("only_online"))
    if only_online:
        skipped = [c["uid"] for c in cams if not cloud_is_on(c)]
        cams = [c for c in cams if cloud_is_on(c)]
        if skipped:
            log.info("only_online: skipping %d offline camera(s), not retried "
                     "until restart: %s", len(skipped), ", ".join(skipped))
    if not cams:
        log.error("no cameras matched (allow-list: %s, only_online: %s)",
                  ", ".join(sorted(wanted)) if wanted else "none", only_online)
        return

    # Probe with the camera's LAN address so the default bind lands on its
    # subnet; the per-camera choice between LAN and public happens later, in
    # ZiotCamera._pick_endpoint.
    probe = api.get_stun_addr(cams[0]["uid"]).get("IpcPrivateIP")
    if args.bind_ip:
        bind_ip = args.bind_ip
    elif probe:
        bind_ip = local_ip_for(probe)
    else:
        bind_ip = local_ip_for("8.8.8.8")
        log.warning("no IpcPrivateIP from the broker — binding on %s by default "
                    "route; pass --bind-ip if that is the wrong interface", bind_ip)
    log.info("binding on %s (camera LAN %s), punch every %.1fs",
             bind_ip, probe or "unknown", PUNCH_INTERVAL)

    live: dict[str, ZiotCamera] = {}
    lock = threading.Lock()

    cloud = CloudState(api, cfg["user_id"])

    def boot(cam_rec):
        uid = cam_rec["uid"]
        try:
            z = ZiotCamera(api, cam_rec, bind_ip,
                           force_relay=args.force_relay,
                           relay_user=cfg.get("relay_user"),
                           relay_pass=cfg.get("relay_pass"))
        except Exception:
            # Nothing to register or recover if we could not even build it.
            log.exception("%s: could not be constructed — skipping", uid)
            with lock:
                cold.append(uid)
            return

        try:
            started = z.start()
        except Exception:
            # start() is not supposed to raise, but if it ever does, the camera
            # must still be registered: an unregistered camera is invisible on
            # /health and, with no threads and no CloudState entry, would never
            # be retried either.
            log.exception("%s: start() raised — keeping it for recovery", uid)
            started = False

        # Keep the camera either way: it retries in the background, and a
        # camera that is merely offline right now must still appear on /health
        # rather than vanishing from the bridge until someone restarts it.
        with lock:
            live[uid] = z
            if not started:
                cold.append(uid)
        cloud.register(z)

    cold: list[str] = []
    threads = [threading.Thread(target=boot, args=(c,)) for c in cams]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if cold:
        log.warning("%d of %d camera(s) did not come up yet (%s) — the bridge "
                    "is serving anyway and will keep retrying them",
                    len(cold), len(cams), ", ".join(sorted(cold)))
    if len(cold) == len(cams):
        log.warning("no camera is streaming yet. These cameras register with "
                    "the cloud but often never open a session; /health will "
                    "show mode=down until one does.")

    cloud.start()

    # 8085 is what the README, the go2rtc examples and the watchdog assume.
    port = args.port or cfg.get("port", 8085)
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
        cloud.stop()
        for z in live.values():
            z.stop()


if __name__ == "__main__":
    main()

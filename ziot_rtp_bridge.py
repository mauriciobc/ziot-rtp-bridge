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
import ipaddress
import json
import logging
import queue
import re
import socket
import struct
import threading
import time
import urllib.error
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

# /health reports this so a watchdog log line can say which bridge generation
# produced it. Field generation, not marketing: v3 added mode/online/media_free.
BRIDGE_VERSION = "4"
_STARTED_MONOTONIC = time.monotonic()

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
# Floor between two STUN lookups. get_stun_addr blocks for up to 10s and the
# punch loop asks for one on every tick without media, so an unfloored resolve
# stretches the 1s punch cadence to 10s and hits the STUN endpoint once a
# second per camera -- starving the NAT mapping the punches exist to keep warm.
RESOLVE_MIN_INTERVAL = 5.0
# Per-packet conditions log at most this often. At 15 fps a line per packet
# buries every other log the bridge writes, indefinitely.
LOG_THROTTLE_INTERVAL = 60.0
FPS_LOG_INTERVAL = 30           # log fps every 30s
RECOVERY_DEAD_THRESHOLD = 10.0  # start recovery after 10s dead
RECOVERY_MAX_BACKOFF = 60.0     # max wait between re-rendezvous attempts
RECOVERY_INITIAL_BACKOFF = 5.0  # first retry after 5s
STATUS_INTERVAL = 30            # refresh onlineState/mediaState from the cloud
# Battery cameras only come online for a few seconds; 30s polls miss the window.
STATUS_OFFLINE_INTERVAL = 5
STATUS_FAIL_WARN = 3            # consecutive refresh failures before warning
RELAY_AFTER_FAILURES = 3        # failed direct rendezvous before trying the relay
RELAY_RETRY_DIRECT = 120.0      # while relayed, retry a direct rendezvous this often
RELAY_FIRMWARE_PIVOT = "TXW817_A_V1.0.11.52"   # app's CameraInfoModel gate
RELAY_DEFAULT_PORT = 554
STUN_SUBNET_MASK = "255.255.255.0"   # the app's isSameSubnet() mask
# Linux ephemeral range; these cameras bind their RTP listen port here.
# A punch-only (no-token) camera that answers hellos is found by sweeping it
# from the existing socket -- rebinding would change our source port and
# drop a session the camera had already aimed at us.
LAN_SWEEP_LO = 32768
LAN_SWEEP_HI = 61000
LAN_SWEEP_RATE = 2000.0              # hellos per second during a LAN resweep
# Cloud HTTP timeouts. The device list can take its time -- a slow answer is
# fine there. notify() cannot: the session dies ~12s without a keepalive
# every 2s, so one 10s stall next to the 10s urlopen default is session
# death. send_stun_addr runs up to 3x per rendezvous; 3 slow calls must not
# stall boot for half a minute.
CLOUD_TIMEOUT = 10.0
NOTIFY_TIMEOUT = 3.0
STUN_ADDR_TIMEOUT = 5.0

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


def rtp_payload(pkt: bytes) -> bytes | None:
    """Return an RTP packet's payload, or None if the packet is malformed.

    The header is only 12 bytes when there are no CSRCs and no extension, which
    is true of what these cameras send but is not guaranteed of anything else —
    a relay may well add either. Slicing a fixed [12:] then feeds header bytes
    to the decoder as media. Padding is stripped too, since µ-law padding would
    otherwise be played as samples.
    """
    if len(pkt) < 12:
        return None
    off = 12 + 4 * (pkt[0] & 0x0f)          # CC: CSRC identifiers
    if pkt[0] & 0x10:                       # X: one header extension
        if len(pkt) < off + 4:
            return None
        off += 4 + 4 * struct.unpack("!H", pkt[off + 2:off + 4])[0]
    if len(pkt) < off:
        return None
    payload = pkt[off:]
    if pkt[0] & 0x20:                       # P: trailing padding
        # RFC 3550: the last octet counts the padding octets, itself included,
        # so 0 is invalid and so is anything longer than the payload. Such a
        # packet is malformed; keeping it would decode padding as media, which
        # is exactly what this function exists to prevent.
        if not payload:
            return None
        pad = payload[-1]
        if pad == 0 or pad > len(payload):
            return None
        payload = payload[:-pad]
    return payload


def ingest_rtp_media(kind: str, packet: bytes, asm, on_frame, on_audio,
                     on_rx) -> bool:
    """Run one accepted RTP packet through the media pipeline.

    This is the whole media path, shared by every transport: video goes
    through the JPEG reassembler, audio through µ-law decode, and liveness
    (`on_rx`) is refreshed only by packets that actually produced media. The
    transports differ only in how they decide `kind` — the direct path keys
    on payload type, the relay on its negotiated interleaved channel — so
    the per-kind work exists exactly once.
    """
    if kind == "video":
        if asm.feed(packet):
            on_rx()
            return True
        return False
    if kind == "audio":
        payload = rtp_payload(packet)
        if payload:
            on_audio(ulaw_to_pcm16(payload))
            on_rx()
            return True
        return False
    return False


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


class CloudError(Exception):
    """An error the cloud reported in the body of a 200 OK.

    This API does not use HTTP status codes for its own failures: a rejected
    token comes back as 200 with {"code": 401, "msg": "Erro comum"} and no
    "data" key, so the body is the only place the rejection appears. Reading
    ["data"] straight off that answer raises KeyError('data'), which says
    nothing about what actually went wrong.
    """

    def __init__(self, code: int, msg: str):
        super().__init__(f"cloud error {code}: {msg or 'no message'}")
        self.code = code
        self.msg = msg


def cloud_error_code(body: dict) -> int | None:
    """The error code in a cloud response, or None if it carried data.

    A response with "data" is a success by definition -- every caller here
    indexes it -- so this can only ever fire on an answer that was going to
    raise KeyError anyway.
    """
    if not isinstance(body, dict) or "data" in body:
        return None
    try:
        return int(body.get("code"))
    except (TypeError, ValueError):
        return None


def is_auth_failure(e: BaseException) -> bool:
    """True for a cloud rejection no amount of retrying will fix.

    The token is a JWT and they expire, so 401/403 is a config error dressed
    as a network error -- worth separating from a cloud outage, which is
    exactly the kind of thing retrying does fix. Both spellings count: the
    HTTP status, and the code this API actually uses, in the body.
    """
    if isinstance(e, CloudError):
        return e.code in (401, 403)
    return isinstance(e, urllib.error.HTTPError) and e.code in (401, 403)


def uid_ssrc(uid: str) -> int | None:
    """The SSRC this camera stamps on its RTP, or None if the UID is not the
    form the mapping assumes.

    The UID's last 8 decimal digits are reused as the SSRC's hex digits:
    141030191094 -> 0x30191094. This is the only per-camera identifier in the
    media path, and the only sound way to tell our camera's packets from
    anyone else's -- the source address cannot do it, since the address the
    STUN broker reports and the address the camera actually sends from
    routinely disagree.
    """
    if not uid or not uid.isascii() or not uid.isdigit() or len(uid) < 8:
        return None
    return int(uid[-8:], 16)


def is_rtp_media(payload: bytes, want_ssrc: int | None = None) -> bool:
    """True when `payload` is RTP v2 with a camera payload type (JPEG / PCMU).

    The vendor hello (`App send hello`) is 14 bytes and parses as RTP v1
    ssrc=0x2068656c if you only check length -- the probe used to treat that
    echo as a HIT. Version bits 0b10 and PT 0/26 are what the cameras send.
    """
    if len(payload) < 12:
        return False
    if (payload[0] & 0xC0) != 0x80:
        return False
    if (payload[1] & 0x7F) not in (0, 26):
        return False
    if want_ssrc is not None:
        return struct.unpack("!I", payload[8:12])[0] == want_ssrc
    return True


def jwt_user_id(token: str) -> int | None:
    """The user_id claim from an account JWT, or None if it is not a JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data["user_id"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


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
    return _flag(row, "onlineState") == "1"


def cloud_is_free(row: dict) -> bool:
    """CameraInfoModel::isFree -- mediaState == "0", null being false.

    This is the app's *only* use of mediaState. It never distinguishes 1 from 3,
    so neither do we: the flag is free/busy and nothing finer.
    """
    return _flag(row, "mediaState") == "0"


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

def parse_static_endpoints(raw: dict) -> dict:
    """Validate offline_endpoints {uid: ip[:port]} into {uid: (ip, port)}.

    Port may be omitted (stored as 0): the listen port is ephemeral and
    rotates, so an IP-only entry is a request to hello-sweep that host.
    Raises ValueError naming the bad entry. Strict on purpose: a bare port
    would punch 0.0.0.0, an out-of-range port dies inside sendto where the
    punch loop swallows it, and the socket is AF_INET so IPv6 can never
    send. Every one of those boots a bridge that reports mode=direct and
    streams nothing.
    """
    out = {}
    for uid, ep in (raw or {}).items():
        text_ep = str(ep).strip()
        ip_s, sep, port_s = text_ep.rpartition(":")
        if not sep:
            ip_s, port_s = text_ep, "0"
        try:
            port = int(port_s)
        except (TypeError, ValueError):
            port = -1
        try:
            addr = ipaddress.ip_address(ip_s.strip("[] "))
            ok_ip = isinstance(addr, ipaddress.IPv4Address)
        except ValueError:
            ok_ip = False
        if not ok_ip or not 0 <= port < 65536:
            raise ValueError(
                f"offline endpoint for {uid!r} must be an IPv4 address or "
                f"ip:port with port 1-65535, got {ep!r}")
        out[str(uid)] = (str(addr), port)
    return out



class GPS555:
    def __init__(self, token: str):
        self._h = {
            "Authorization": f"Bearer {token}",
            "language": "pt",
            "User-Agent": "Dart/3.10 (dart:io)",
        }

    def _get(self, path: str, timeout: float = CLOUD_TIMEOUT, **params) -> dict:
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode({k: str(v) for k, v in params.items()})
        req = urllib.request.Request(url, headers=self._h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    @staticmethod
    def _data(body: dict) -> dict:
        """The "data" payload, or a CloudError naming what the cloud said."""
        code = cloud_error_code(body)
        if code is not None:
            raise CloudError(code, str(body.get("msg", "")))
        return body["data"]

    def list_cameras(self, user_id: int) -> list[dict]:
        return self._data(self._get("/v1/ipc", terminalFamilyId=user_id))["list"]

    def send_stun_addr(self, uid, ip, port):
        return self._data(self._get("/v1/ipc/send-stun-addr",
                                    timeout=STUN_ADDR_TIMEOUT,
                                    appId=APP_ID,
                                    uid=uid, publicIp=ip, publicPort=port,
                                    privateIp=ip, privatePort=port))

    def get_stun_addr(self, uid):
        return self._data(self._get(f"/v1/ipc/stun-addr/{uid}"))

    def notify(self, uid, event_type: int):
        """GET /v1/ipc/notify-live-event — event_type is a CameraEventType value
        (EVENT_START, EVENT_KEEPALIVE, EVENT_STOP, EVENT_RELAY, ...)."""
        self._get("/v1/ipc/notify-live-event",
                  timeout=NOTIFY_TIMEOUT,
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


# Fragment-housekeeping bounds: never track more than _MAX_PENDING_TS
# timestamps at once, and when over, keep only the _KEEP_NEWEST_TS newest.
# At 15 fps a whole frame is 2-6 fragments, so 4 pending frames is already
# a badly stalled stream worth discarding from the front.
_MAX_PENDING_TS = 8
_KEEP_NEWEST_TS = 4


class RtpJpegReassembler:
    def __init__(self, emit):
        self._frags = defaultdict(dict)
        self._meta = {}
        self._emit = emit

    def reset(self):
        self._frags.clear()
        self._meta.clear()

    def feed(self, pkt: bytes) -> bool:
        """Store one structurally valid RFC 2435 fragment.

        Returns True when one fragment was accepted and stored (not when a
        complete JPEG was emitted). Returns False without touching `_frags`
        or `_meta` for malformed input.
        """
        p = rtp_payload(pkt)
        if p is None or len(p) < 8:
            return False
        marker = pkt[1] >> 7
        ts = struct.unpack(">I", pkt[4:8])[0]
        frag_off = struct.unpack(">I", b"\x00" + p[1:4])[0]
        jtype, q = p[4], p[5]
        width, height = p[6] * 8, p[7] * 8
        off, dri = 8, 0
        if jtype >= 64:
            if len(p) < 12:
                return False
            dri = struct.unpack(">H", p[8:10])[0]
            off = 12
        if q >= 128 and frag_off == 0:
            if len(p) < off + 4:
                return False
            qlen = struct.unpack(">H", p[off + 2:off + 4])[0]
            if len(p) < off + 4 + qlen:
                return False
            self._meta[ts] = (width, height, p[off + 4:off + 4 + qlen], jtype, dri)
            off += 4 + qlen
        self._frags[ts][frag_off] = p[off:]
        if marker and ts in self._meta:
            w, h, qt, jt, dri = self._meta.pop(ts)
            parts = self._frags.pop(ts)
            body = b"".join(parts[o] for o in sorted(parts))
            self._emit(build_jpeg_header(w, h, qt, jt, dri) + body + b"\xff\xd9")
        if len(self._frags) > _MAX_PENDING_TS:
            for old in sorted(self._frags)[:-_KEEP_NEWEST_TS]:
                self._frags.pop(old, None)
                self._meta.pop(old, None)
        return True


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
        self._challenge = None          # WWW-Authenticate from the last 401
        # interleaved channel -> ("video"|"audio")
        self._channels: dict[int, str] = {}
        self._asm = RtpJpegReassembler(on_frame)

    # ---- low-level socket helpers ------------------------------------------

    def _recv_some(self) -> bool:
        if self._sock is None:          # close() raced the pump thread
            return False
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
        """Send one RTSP request and return (status, headers, body).

        A cached challenge is re-derived per request, never replayed: a
        digest response covers exactly one method+URI, so replaying the
        DESCRIBE header on SETUP's track URI (and on PLAY) fails SETUP
        forever on any digest-authenticating relay.
        """
        uri = uri or self.url
        headers = dict(headers or {})
        self._cseq += 1
        headers["CSeq"] = str(self._cseq)
        headers["User-Agent"] = "ziot-rtp-bridge"
        if self._session:
            headers["Session"] = self._session
        if self._challenge:
            headers["Authorization"] = self._auth_header(
                method, uri, self._challenge)

        def send():
            lines = [f"{method} {uri} RTSP/1.0"]
            lines += [f"{k}: {v}" for k, v in headers.items()]
            self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        send()
        status, hdrs, body = self._read_response(method)

        if status == 401 and "www-authenticate" in hdrs:
            if self._user is None:
                raise PermissionError(
                    "relay demands authentication but no credentials are "
                    "configured (set relay_user/relay_pass in the config)")
            # Refresh on every 401, not just the first: a nonce can expire
            # mid-session, and the retry below then answers it.
            self._challenge = hdrs["www-authenticate"]
            headers["Authorization"] = self._auth_header(
                method, uri, self._challenge)
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
        ingest_rtp_media(kind, packet, self._asm, self._on_frame,
                         self._on_audio, self._on_rx)

    # ---- SDP ----------------------------------------------------------------

    # What we can actually decode: static payload type, and the a=rtpmap
    # encoding name for relays that assign a dynamic type instead.
    _DECODABLE = {"video": (26, "JPEG"), "audio": (0, "PCMU")}

    def _parse_sdp(self, body: bytes) -> list:
        """Return [(kind, payload_type, control_url)] for each media section.

        An `m=` line may offer several formats — `m=audio 0 RTP/AVP 8 0` offers
        PCMA *and* PCMU — so every format is considered and the one we can
        decode is chosen, rather than taking the first and giving up. A payload
        type is None when the section offers nothing we handle; the caller skips
        those. Malformed sections are dropped with a warning instead of raising.
        """
        sections, cur = [], None
        for raw in body.decode("utf8", "replace").splitlines():
            line = raw.strip()
            if line.startswith("m="):
                if cur:
                    sections.append(cur)
                cur = None
                parts = line[2:].split()
                if len(parts) < 4:
                    log.warning("[%s] relay SDP: ignoring malformed media line "
                                "%r", self.tag, line)
                    continue
                fmts = []
                for f in parts[3:]:
                    try:
                        fmts.append(int(f))
                    except ValueError:
                        pass        # a non-numeric format we cannot use anyway
                cur = {"kind": parts[0], "fmts": fmts, "rtpmap": {},
                       "control": None}
            elif cur is None:
                continue
            elif line.startswith("a=control:"):
                cur["control"] = line[len("a=control:"):].strip()
            elif line.startswith("a=rtpmap:"):
                # a=rtpmap:<pt> <encoding>/<clock>[/<channels>]
                rest = line[len("a=rtpmap:"):].split(None, 1)
                if len(rest) == 2:
                    try:
                        cur["rtpmap"][int(rest[0])] = \
                            rest[1].split("/")[0].strip().upper()
                    except ValueError:
                        pass
        if cur:
            sections.append(cur)

        out = []
        for sec in sections:
            if sec["kind"] not in self._DECODABLE:
                continue
            out.append((sec["kind"], self._choose_pt(sec), sec["control"]))
        return out

    def _choose_pt(self, sec: dict):
        """Pick a decodable payload type from one media section, or None."""
        static_pt, encoding = self._DECODABLE[sec["kind"]]
        if static_pt in sec["fmts"]:
            return static_pt
        for pt in sec["fmts"]:
            if sec["rtpmap"].get(pt) == encoding:
                return pt
        return None

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
            if pt is None:
                log.warning("[%s] relay offers no decodable %s format — "
                            "skipping that track", self.tag, kind)
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
        sock = self._sock               # close() may null it mid-pump
        if sock is None:
            return
        sock.settimeout(1.0)
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
                    if sock is not self._sock or self._sock is None:
                        return          # closed under us; never send on it
                    sock.sendall(
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
    def __init__(self, api: "GPS555 | None", cam: dict, bind_ip: str,
                 force_relay: bool = False, relay_user: str = None,
                 relay_pass: str = None, static_addr: tuple = None):
        self.api = api
        self._static_addr = static_addr  # offline mode: (ip, port) from the probe
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
        self.stats = {"frames": 0, "audio_pkts": 0, "rx_errors": 0,
                      "foreign_ssrc": 0, "rx_datagrams": 0}
        self._last_rx = 0.0
        # Media identity. None means this UID carries no usable SSRC, so the
        # direct path can only check which socket a datagram arrived on.
        self._ssrc = uid_ssrc(self.uid)
        self._learned_ssrc = None  # learned from first valid RTP packet in offline mode
        self._media_source = None       # where media actually comes from
        self._log_throttle: dict = {}
        # FPS tracking
        self._frame_times: deque = deque(maxlen=120)
        self._last_fps_log = time.monotonic()
        # Endpoint move tracking
        self._endpoint_moves = 0
        self._last_resolve = float("-inf")
        # Recovery tracking
        self._re_rendezvous_count = 0
        self._backoff = RECOVERY_INITIAL_BACKOFF
        self._last_re_rendezvous = 0.0
        self._wake_now = False          # set when cloud onlineState goes 0→1
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
        if self._ssrc is None:
            log.warning("[%s] UID is not the 8+ decimal digit form the SSRC "
                        "mapping assumes -- direct media cannot be "
                        "identity-checked", self.uid)

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
        # Battery cameras sleep with onlineState=0. Rebinding while they are
        # down rotates our local port; when they next check in, media is aimed
        # at a socket we already closed. Kick a full rendezvous the moment the
        # cloud says they are back, without waiting out a 60s backoff.
        if not cloud_is_on(old) and cloud_is_on(self.cloud) and not self.is_streaming:
            log.info("[%s] came online — waking session", self.uid)
            self._wake_now = True
            self._backoff = 0.0
            self._last_re_rendezvous = 0.0

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
        """Where to punch: STUN when we have the cloud, else the LAN target.

        Punch-only mode has no broker. Prefer the address media is already
        arriving from -- `_resolve` used to reset that back to a stale
        offline_endpoints port every 5s of silence and punch the wrong
        place forever. When a token is present, take the STUN port (it
        rotates) but pin the configured LAN IP if we have one.
        """
        if self.api is None:
            if self._media_source:
                return self._media_source[0], self._media_source[1], "static"
            if self._static_addr is not None:
                return self._static_addr[0], self._static_addr[1], "static"
            return None
        d = self.api.get_stun_addr(self.uid)
        if not self._accept_stun(d):
            return None
        ip, prt, kind = self._pick_endpoint(d)
        if self._static_addr and self._static_addr[0]:
            # Pin the configured LAN IP; take the camera's private port
            # (the one that rotates) rather than the public mapping.
            try:
                priv = int(d.get("IpcPrivatePort"))
            except (TypeError, ValueError):
                priv = 0
            if priv:
                prt = priv
            ip = self._static_addr[0]
            kind = "static-lan"
        return ip, prt, kind

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

        if self.api is None:
            # Punch-only: no broker to register with and nothing to wake.
            # A port of 0 means "IP known, sweep for the listen port".
            if self._static_addr is None:
                try:
                    sock.close()
                except Exception:
                    pass
                return False
            # Install the socket before anything is sent. The camera, if it
            # answers at all, sends RTP back to the source port of the hello,
            # so the socket that sweeps must be the socket that receives — a
            # probe socket that found the port and then closed it would be a
            # new source port afterwards, and the reply would miss us. The
            # receive thread (started by start() once this returns) reads the
            # reply, sets `_media_source`, and `_punch_dest` follows it — no
            # port number ever needs to be correlated with a hello.
            with self._sock_lock:
                self.sock = sock
                self.addr = self._static_addr
            self._note_endpoint("static")
            if self._static_addr[1] == 0:
                log.info("[%s] sweeping %s for the listen port (we are "
                         "%s:%d) [offline, no token]",
                         self.uid, self._static_addr[0], self.bind_ip, port)
                self._lan_resweep()
            else:
                log.info("[%s] camera at %s:%d (we are %s:%d) "
                         "[offline, no token]",
                         self.uid, self._static_addr[0], self._static_addr[1],
                         self.bind_ip, port)
            self._last_re_rendezvous = time.monotonic()
            return True

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

        if not addr and self._static_addr:
            addr = self._static_addr
            self._note_endpoint("static")
            log.warning("[%s] no address from broker — punching configured "
                        "%s:%d", self.uid, addr[0], addr[1])
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
        self._last_re_rendezvous = time.monotonic()
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

    def _log_throttled(self, key: str, level: int, msg: str, *args, **kw) -> None:
        """Log at most once per LOG_THROTTLE_INTERVAL per key.

        Both callers sit in the per-packet path, where the condition being
        reported either holds for one packet or holds for every packet.
        """
        now = time.monotonic()
        if now - self._log_throttle.get(key, float("-inf")) < LOG_THROTTLE_INTERVAL:
            return
        self._log_throttle[key] = now
        log.log(level, msg, *args, **kw)

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
            if self.api is not None:
                try:
                    self.api.notify(self.uid, EVENT_KEEPALIVE)
                except Exception:
                    pass
            self._stop.wait(KEEPALIVE_INTERVAL)

    def _punch_dest(self):
        """Where hellos go.

        With the cloud, the STUN port is where the camera listens -- media
        may arrive from a different source, but punching that source does
        not retarget a session that has already rebound. Punch-only has no
        STUN, so follow wherever RTP is actually coming from.
        """
        if self.api is None:
            return self._media_source or self.addr
        return self.addr

    def _resolve(self) -> None:
        """Re-ask the broker where the camera is, no more often than
        RESOLVE_MIN_INTERVAL -- the lookup blocks, and the punch loop asks for
        one on every tick where media is absent."""
        now = time.monotonic()
        if now - self._last_resolve < RESOLVE_MIN_INTERVAL:
            return
        self._last_resolve = now
        try:
            picked = self._fetch_endpoint()
        except Exception:
            return
        if not picked:
            return                      # stale answer, keep the current address
        ip, prt, kind = picked
        self._note_endpoint(kind)
        addr = (ip, prt)
        moved = False
        old = None
        with self._sock_lock:
            if addr != self.addr:
                old = self.addr
                self.addr = addr
                self._endpoint_moves += 1
                moved = True
        if moved:
            log.info("[%s] endpoint moved %s -> %s", self.uid, old, addr)

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
                # Starved is about where to punch, not how often: the resolve
                # refreshes the endpoint, then this tick's hello goes out
                # through the one send below, like every other tick.
                self._resolve()
            try:
                dest = self._punch_dest()
                with self._sock_lock:
                    if self.sock and dest and dest[1]:
                        self.sock.sendto(PUNCH, dest)
                        # The app also sends this; keeps NAT/relay mappings warm
                        if tick % HEART_EVERY_N_PUNCHES == 0:
                            self.sock.sendto(HEART, dest)
            except Exception:
                pass
            self._stop.wait(PUNCH_INTERVAL)

    def _recovery(self):
        """Monitor stream health and trigger full re-rendezvous when dead."""
        while not self._stop.is_set():
            try:
                self._recovery_tick()
            except Exception:
                # This thread is the only thing that will ever bring the camera
                # back; it must not die on an unexpected error.
                log.exception("[%s] recovery tick failed", self.uid)
            self._stop.wait(1.0)

    def _reannounce(self) -> None:
        """Keep the local UDP port, re-register it, and send notify(start).

        Sleeping cameras check in with the vendor for a few seconds. If we
        rebind in that window, they send RTP at a port we just closed.
        """
        if self.api is None:
            return
        with self._sock_lock:
            sock = self.sock
        if sock is None:
            return
        try:
            port = sock.getsockname()[1]
        except OSError:
            return
        try:
            self.api.send_stun_addr(self.uid, self.bind_ip, port)
        except Exception as e:
            log.warning("[%s] send-stun-addr: %s", self.uid, e)
        try:
            self.api.notify(self.uid, EVENT_START)
        except Exception as e:
            log.warning("[%s] notify(start): %s", self.uid, e)
        self._last_resolve = float("-inf")
        self._resolve()

    def _lan_resweep(self) -> None:
        """Hello-sweep the camera LAN IP from the existing socket.

        Punch-only must never rebind: the camera, if it answers at all, sends
        RTP back to the source port of the hello. A new socket would be a
        different source and the reply would miss us. Whatever `_media_source`
        the sweep produces is set by the receive path as packets arrive —
        during recovery the receive thread is already running; during the
        first rendezvous the replies sit in the socket buffer until
        `start()` starts it. No reply is ever correlated with a hello:
        knowing the port number is not needed, only the packets are.
        """
        ip = (self._static_addr or (None, 0))[0]
        if not ip:
            return
        with self._sock_lock:
            sock = self.sock
        if not sock:
            return
        gap = 1.0 / max(LAN_SWEEP_RATE, 1)
        for port in range(LAN_SWEEP_LO, LAN_SWEEP_HI + 1):
            if self._stop.is_set() or self.is_streaming:
                return
            with self._sock_lock:
                if self.sock is not sock:
                    return
            try:
                sock.sendto(PUNCH, (ip, port))
            except OSError:
                return
            if gap:
                time.sleep(gap)

    def _record_attempt(self, ok: bool) -> None:
        """Account one recovery attempt.

        A streaming attempt resets the failure count; anything else counts
        and doubles the backoff. Resetting the backoff to its initial value
        is deliberately NOT here: the top-of-tick is_streaming check owns
        that, so one attempt cannot both succeed and reset in the same tick.
        """
        if ok:
            self._consec_failures = 0
        else:
            self._consec_failures += 1
        # The floor is what keeps a wake from killing the gate for good:
        # update_cloud sets _backoff to 0.0 on a 0->1 online flip (the
        # one-shot bypass is _last_re_rendezvous = 0.0, not the backoff),
        # and min(0 * 2, MAX) would stay 0 forever — recovery would then
        # hammer a full rendezvous every tick.
        self._backoff = min(max(self._backoff, RECOVERY_INITIAL_BACKOFF) * 2,
                            RECOVERY_MAX_BACKOFF)

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
        wake = self._wake_now
        self._wake_now = False

        # Don't re-rendezvous too often. A 0→1 online flip bypasses this so
        # a battery camera's short awake window is not spent sitting in backoff.
        since_last = time.monotonic() - self._last_re_rendezvous
        if not wake:
            if since_last < self._backoff:
                return
            # A 2–3s gap is a STUN port move, not a dead camera. Rebinding
            # that would drop the session the punch loop is about to retarget.
            if dead_for < RECOVERY_DEAD_THRESHOLD:
                return

        # Punch-only: keep the socket, sweep the LAN. Re-rendezvous would
        # rebind and there is no relay without a device-list relay_ip. A
        # sweep that produced media resets the backoff at once -- there is
        # no transport attempt here whose success the next tick could
        # rediscover, so the reset cannot be left to it.
        if self.api is None:
            log.warning("[%s] stream dead %.0fs — LAN resweep (backoff %.0fs)",
                        self.uid, dead_for, self._backoff)
            self._last_re_rendezvous = time.monotonic()
            self._re_rendezvous_count += 1
            self._lan_resweep()
            if self.is_streaming:
                self._consec_failures = 0
                self._backoff = RECOVERY_INITIAL_BACKOFF
            else:
                self._record_attempt(False)
            return

        # Cloud says the camera is asleep. Rebinding here is how a 60s backoff
        # turns into 176 closed sockets and a miss when it next checks in.
        # The reannounce interval grows instead of sitting at the initial 5s:
        # a sleeping camera checks in rarely, and re-registering its port
        # every 5s for hours is cloud chatter with nothing to catch. The
        # awake window is the cloud poller's job (STATUS_OFFLINE_INTERVAL):
        # a 0->1 flip there kicks the wake below, which resets this backoff
        # and bypasses the gate entirely.
        if not wake and not cloud_is_on(self.cloud) and self.sock is not None:
            log.warning("[%s] cloud-offline %.0fs — reannounce, no rebind "
                        "(next in %.0fs)", self.uid, dead_for, self._backoff)
            self._last_re_rendezvous = time.monotonic()
            self._re_rendezvous_count += 1
            self._reannounce()
            self._backoff = min(max(self._backoff, RECOVERY_INITIAL_BACKOFF)
                                * 2, RECOVERY_MAX_BACKOFF)
            return

        what = "relay" if self.force_relay else "re-rendezvous"
        log.warning("[%s] stream dead %.0fs — %s (backoff %.0fs)",
                    self.uid, dead_for, what, self._backoff)
        self._last_re_rendezvous = time.monotonic()
        self._re_rendezvous_count += 1

        ok = self._open_transport()
        if ok:
            # Reset frame tracker
            self._frame_times.clear()
        else:
            log.error("[%s] %s failed", self.uid, what)
        # Either way the attempt costs one backoff, after which the attempt
        # is accounted exactly once: streaming resets the count, failure
        # counts and doubles.
        self._stop.wait(self._backoff)
        self._record_attempt(ok and self.is_streaming)

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

    def _on_direct_audio(self, pcm: bytes) -> None:
        self.stats["audio_pkts"] += 1
        self.audio.publish(pcm)

    def _handle_direct_packet(self, sock: socket.socket, source: tuple[str, int], packet: bytes, asm: RtpJpegReassembler) -> bool:
        """Accept one datagram from the direct socket.

        Identity is the RTP SSRC, not the source address. The camera's real
        sending address and the endpoint the STUN broker reports routinely
        disagree -- it rebinds a UDP port per session, our /24 subnet test
        picks the public pair on a wider LAN, and symmetric NAT maps us
        differently -- so matching on the address would drop every packet of a
        stream that is working, permanently.
        """
        with self._sock_lock:
            if self.sock is not sock:
                return False
            addr = self.addr
        if len(packet) < 12:
            return False
        # RTP v2 only — the same gate is_rtp_media applies. Everything below,
        # SSRC learning included, must never see a non-RTP datagram: a hello
        # echo (14 bytes that parse as RTP-ish) would otherwise be remembered
        # as the camera's SSRC, after which the camera's real packets are
        # dropped as foreign forever.
        if packet[0] >> 6 != 2:
            return False
        # Payload type decides the kind here; the relay decides by channel.
        # Both feed the same pipeline (ingest_rtp_media). The kind gate runs
        # before SSRC learning for the same reason: it is the second thing a
        # datagram must prove before any state is learned from it.
        kind = {26: "video", 0: "audio"}.get(packet[1] & 0x7f)
        if kind is None:
            return False
        if self._ssrc is not None:
            ssrc = struct.unpack("!I", packet[8:12])[0]
            # In offline mode without a token, learn the SSRC from the first
            # packet that passed the gates above rather than strictly
            # matching the UID-mapped value, which the camera may not use
            # when operating punch-only.
            if self.api is None and self._learned_ssrc is None:
                self._learned_ssrc = ssrc
            elif ssrc != self._ssrc:
                # If we have a learned SSRC (from offline mode), use that instead
                if self.api is None and ssrc == self._learned_ssrc:
                    pass  # Accept packet with learned SSRC
                else:
                    self.stats["foreign_ssrc"] += 1
                    self._log_throttled(
                        "foreign_ssrc", logging.WARNING,
                        "[%s] dropping RTP with ssrc 0x%08x, expected 0x%08x "
                        "(%d so far)", self.uid, ssrc, self._ssrc,
                        self.stats["foreign_ssrc"])
                    return False
        if source != addr and source != self._media_source:
            log.info("[%s] media arriving from %s while the broker says %s",
                     self.uid, source, addr)
        self._media_source = source
        return ingest_rtp_media(kind, packet, asm, self._on_frame,
                                self._on_direct_audio, self._mark_rx)

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
                data, source = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                # Socket was closed (re-rendezvous in progress)
                self._stop.wait(0.5)
                continue
            # Counted before any filtering, so "nothing is on the wire" and
            # "packets arrive but we reject them" cannot look alike on /health.
            # Everything below this line can drop a packet for a good reason;
            # this is the only number that says one showed up at all.
            self.stats["rx_datagrams"] += 1
            try:
                self._handle_direct_packet(sock, source, data, asm)
            except Exception:
                self.stats["rx_errors"] += 1
                self._log_throttled(
                    "rx_error", logging.ERROR,
                    "[%s] direct RTP packet failed (%d so far)",
                    self.uid, self.stats["rx_errors"], exc_info=True)

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
                # "0s ago" for a camera that has never sent anything reads as
                # "a packet just landed", which is the opposite of the truth
                # and the worst case to misreport. Say so instead.
                last_rx = (f"{time.monotonic() - self._last_rx:.0f}s ago"
                           if self._last_rx else "never")
                log.warning("[%s] NO STREAM — last rx %s, %d datagrams in, "
                            "%d moves, %d re-rendezvous",
                            self.uid, last_rx, self.stats["rx_datagrams"],
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
            "rx_datagrams": self.stats["rx_datagrams"],
            "rx_errors": self.stats["rx_errors"],
            "foreign_ssrc": self.stats["foreign_ssrc"],
            "media_source": (f"{self._media_source[0]}:{self._media_source[1]}"
                             if self._media_source else None),
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
            if self.api is not None:
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

    def __init__(self, api: GPS555, user_id: int,
                 boot: "BootState | None" = None):
        self.api = api
        self.user_id = user_id
        self.boot = boot
        self._cams: dict[str, "ZiotCamera"] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._fails = 0
        self._auth_failed = False

    def register(self, cam: "ZiotCamera") -> None:
        with self._lock:
            self._cams[cam.uid] = cam

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self):
        self.poll()
        while not self._stop.is_set():
            wait = STATUS_INTERVAL
            with self._lock:
                cams = list(self._cams.values())
            if any(not cloud_is_on(c.cloud) and not c.is_streaming for c in cams):
                wait = STATUS_OFFLINE_INTERVAL
            self._stop.wait(wait)
            if self._stop.is_set():
                break
            self.poll()

    def poll(self) -> None:
        try:
            rows = self.api.list_cameras(self.user_id)
        except Exception as e:
            self._fails += 1
            if is_auth_failure(e) and not self._auth_failed:
                # A revoked token after boot: no retry will fix it, and the
                # retry_boot path that parks "failed" only covers startup.
                # Latch so this logs once; a new token needs a restart anyway.
                self._auth_failed = True
                log.error("cloud rejected the token (%s) — serving from "
                          "last-known state; update \"token\" and restart", e)
                if self.boot is not None:
                    self.boot.set("failed", "the cloud rejected the token")
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
        if self._auth_failed:
            # A 401 we parked on has stopped happening. Latching it for the
            # process lifetime would leave /health reporting "failed" -- and
            # the watchdog shouting -- for a bridge that is demonstrably
            # working again. Only ever undo a park we made ourselves.
            self._auth_failed = False
            log.info("cloud accepted the token again — clearing the parked "
                     "auth failure")
            if self.boot is not None:
                self.boot.set("ready", None)
        by_uid = {r.get("uid"): r for r in rows}
        with self._lock:
            cams = list(self._cams.values())
        for cam in cams:
            row = by_uid.get(cam.uid)
            if row:
                cam.update_cloud(row)


class BootState:
    """Why the bridge is not serving cameras yet.

    /health answers from the moment the socket is bound, so a monitor can tell
    "still coming up" and "the cloud is unreachable" apart from "the process is
    dead" -- which a bridge that binds only after a successful device-list
    fetch cannot, because it is not listening yet.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._phase = "starting"
        self._detail = None
        self._attempts = 0

    def set(self, phase: str, detail: str | None, attempts: int = 0) -> None:
        with self._lock:
            self._phase = phase
            self._detail = detail
            self._attempts = attempts

    def snapshot(self) -> dict:
        with self._lock:
            out = {"phase": self._phase}
            if self._detail:
                out["detail"] = self._detail
            if self._attempts:
                out["attempts"] = self._attempts
            return out


def print_camera_table(cams: list[dict]) -> None:
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


def make_handler(cameras: dict, boot: "BootState", lock: threading.Lock,
                 http_token: str | None = None):
    def snapshot() -> list:
        # The retry-boot path registers cameras while these threads serve:
        # iterating the shared map directly can raise "dictionary changed
        # size during iteration" mid-/health. Copy under the lock instead.
        with lock:
            return list(cameras.values())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        # Applied to the connection socket: a client that stops reading pins
        # its handler thread forever on a write without this, and each
        # MJPEG/WAV client holds a thread. A healthy viewer never stalls
        # 30 s; a stalled one gets a TimeoutError, which _pump treats as a
        # hangup.
        timeout = 30

        def log_message(self, *a):
            pass

        def _authorized(self, query: str) -> bool:
            if not http_token:
                return True
            if self.headers.get("Authorization", "") == f"Bearer {http_token}":
                return True
            # ?token= too: <img src> and <audio src> cannot set headers.
            got = urllib.parse.parse_qs(query).get("token", [None])[0]
            return got == http_token

        def do_GET(self):
            split = urllib.parse.urlsplit(self.path)
            path = split.path.strip("/")
            if path == "health":
                # Token-free on purpose: the watchdog polls it unauthenticated
                # from cron and a 401 there reads as "bridge dead".
                self._health()
                return
            if not self._authorized(split.query):
                self.send_error(401)
                return
            if path in ("", "cameras"):
                self._index()
            else:
                # One route table; the slice length comes from the prefix,
                # so the per-route offsets can never drift apart.
                for prefix, fn in (("view/", self._view), ("cam/", self._mjpeg),
                                   ("audio/", self._wav)):
                    if path.startswith(prefix):
                        uid = path[len(prefix):]
                        if uid in cameras:
                            fn(cameras[uid])
                        else:
                            self.send_error(404)
                        return
                self.send_error(404)

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _index(self):
            cams = snapshot()
            self._send(json.dumps([{
                "uid": c.uid,
                "view": f"/view/{c.uid}",
                "video": f"/cam/{c.uid}",
                "audio": f"/audio/{c.uid}",
                "online_state": c.cloud.get("onlineState"),
                "media_state": c.cloud.get("mediaState"),
                "online": cloud_is_on(c.cloud),
                "media_free": cloud_is_free(c.cloud),
                "mode": c.mode,
                "streaming": c.is_streaming,
                "fps": round(c.fps, 1),
                "stats": c.stats,
            } for c in cams], indent=2).encode(),
                "application/json")

        def _health(self):
            # `status` stays exactly "ok"/"degraded": the watchdog reserves
            # "error" for "could not reach the bridge at all", and a new value
            # here would be indistinguishable from that. The reason lives in
            # `boot` instead, which old readers simply ignore.
            cams = snapshot()
            data = {
                "status": "ok" if any(c.is_streaming for c in cams) else "degraded",
                "version": BRIDGE_VERSION,
                "uptime_s": round(time.monotonic() - _STARTED_MONOTONIC),
                "boot": boot.snapshot(),
                "cameras": [c.health() for c in cams],
            }
            self._send(json.dumps(data, indent=2).encode(), "application/json")

        def _view(self, cam: ZiotCamera):
            uid = cam.uid
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

        def _pump(self, cam: ZiotCamera, q: queue.Queue, label: str,
                  write) -> None:
            """Stream one Fanout to this client until it goes away.

            The idle policy is shared because it is one policy: three 10s
            gaps without media end the stream. A client hanging up
            mid-frame is the normal way to stop watching, so only that --
            and nothing else -- is swallowed here."""
            idle = 0
            try:
                while True:
                    try:
                        item = q.get(timeout=10)
                    except queue.Empty:
                        idle += 1
                        if idle >= 3:
                            log.warning("[%s] %s idle timeout", cam.uid,
                                        label)
                            break
                        continue
                    idle = 0
                    write(item)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except TimeoutError:
                # The handler's 30s connection timeout fired on a write: the
                # client stopped reading. Same as a hangup, not an error.
                log.info("[%s] %s client stalled past the write timeout",
                         cam.uid, label)

        def _mjpeg(self, cam: ZiotCamera):
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            with cam.video.subscribe() as q:
                self._pump(cam, q, "viewer", self._write_jpeg_part)

        def _write_jpeg_part(self, frame: bytes) -> None:
            self.wfile.write(
                b"--frame\r\nContent-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode()
                + b"\r\n\r\n" + frame + b"\r\n")

        def _wav(self, cam: ZiotCamera):
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(wav_header())
            self.wfile.flush()
            with cam.audio.subscribe() as q:
                self._pump(cam, q, "audio viewer", self.wfile.write)

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
    ap.add_argument("--offline", action="store_true",
                    help="roster from offline_endpoints instead of the device "
                         "list. A token in the config is still used for "
                         "notify/keepalive (required on TXW817). Without a "
                         "token, punch the LAN only")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    try:
        with open(args.config) as fh:
            cfg = json.load(fh)
    except OSError as e:
        # The file is not there or not readable. The image ships no config, so
        # this is what a forgotten -v looks like, and under
        # --restart unless-stopped a bare traceback here just loops.
        log.error("cannot read config %s: %s -- mount it into the container "
                  "with  -v /host/path/ziot_config.json:%s:ro",
                  args.config, e, args.config)
        raise SystemExit(2)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # Read fine, parsed badly. Naming the mount here would send whoever is
        # reading the logs to check a mount that is already correct; the file
        # itself is what needs editing, and the parser already said where.
        log.error("config %s is not valid JSON: %s -- the file was read, so "
                  "the mount is fine; fix the file at that position",
                  args.config, e)
        raise SystemExit(2)

    global PUNCH_INTERVAL
    default_interval = PUNCH_INTERVAL
    raw = cfg.get("punch_interval", default_interval)
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        interval = None
    # A bad interval is not worth refusing to boot over: exiting here puts
    # every camera in the fleet dark in a restart loop. Clamp loudly instead.
    # (nan and inf fail the range test, so this covers them too.)
    if interval is None or not 0 < interval < STARVED_THRESHOLD:
        log.error("punch_interval %r must be a finite number greater than 0 "
                  "and less than %g -- using %g instead",
                  raw, STARVED_THRESHOLD, default_interval)
        interval = default_interval
    PUNCH_INTERVAL = interval

    offline = bool(args.offline or cfg.get("offline"))
    static: dict = {}
    if offline:
        try:
            static = parse_static_endpoints(cfg.get("offline_endpoints"))
        except ValueError as e:
            ap.error(str(e))
        if not static:
            ap.error("offline mode needs offline_endpoints "
                     "{uid: ip} or {uid: ip:port} in the config")
        if args.list_cameras:
            ap.error("--list-cameras needs the cloud; drop --offline")
        # These cameras ignore unsolicited hellos and drop the session ~12s
        # after the last notify(keepAlive). A token in the same config is
        # used for signalling (send-stun-addr / notify / keepalive) so
        # --offline actually streams; without one we punch the LAN only.
        token = cfg.get("token")
        api = GPS555(token) if token else None
    else:
        api = GPS555(cfg["token"])

    # The device list needs an account id. The config may carry it, or the
    # JWT's user_id claim does. Resolved once, here, because every use site
    # below needs the resolved value, not the raw config key: with a
    # token-only config, indexing cfg["user_id"] raised KeyError, which the
    # boot retry loop caught and re-attempted forever as a fake cloud outage.
    user_id = cfg.get("user_id")
    if user_id is None and cfg.get("token"):
        user_id = jwt_user_id(cfg["token"])

    if args.list_cameras:
        # One-shot and interactive: no fleet to strand and nobody watching
        # /health, so let a cloud failure surface as it always has. But a
        # missing user_id is not a cloud failure -- retrying cannot invent
        # one -- so it is a config error like an unreadable file.
        if user_id is None:
            log.error("no user_id in %s and the token carries none -- add "
                      "\"user_id\" (or use a token that has one); the device "
                      "list cannot be fetched without it", args.config)
            raise SystemExit(2)
        print_camera_table(api.list_cameras(user_id))
        return

    boot = BootState()
    live: dict[str, ZiotCamera] = {}
    lock = threading.Lock()
    if api is None:
        cloud = None
    elif user_id is None:
        if not offline:
            # Same two-tier rule as the rejected token at startup: nothing is
            # serving yet and no retry can help, so this is a config error.
            log.error("no user_id in %s and the token carries none -- add "
                      "\"user_id\" (or use a token that has one); the device "
                      "list cannot be fetched without it", args.config)
            raise SystemExit(2)
        # --offline needs no roster from the cloud; the token still signals.
        cloud = None
    else:
        cloud = CloudState(api, user_id, boot)
    stopping = threading.Event()

    def bring_up(cams) -> bool:
        """Filter one device list down to our cameras and start them.

        False means the list held nothing for us. That is a settled answer
        rather than a transient one, so the caller stops instead of retrying.
        """
        wanted = set(cfg.get("cameras") or [])
        cams = [c for c in cams if not wanted or c["uid"] in wanted]
        only_online = bool(cfg.get("only_online"))
        if offline and only_online:
            log.warning("only_online needs cloud flags; ignoring in offline mode")
            only_online = False
        if only_online:
            skipped = [c["uid"] for c in cams if not cloud_is_on(c)]
            cams = [c for c in cams if cloud_is_on(c)]
            if skipped:
                log.info("only_online: skipping %d offline camera(s), not retried "
                         "until restart: %s", len(skipped), ", ".join(skipped))
        if not cams:
            log.error("no cameras matched (allow-list: %s, only_online: %s)",
                      ", ".join(sorted(wanted)) if wanted else "none", only_online)
            boot.set("no-cameras",
                     "the device list held no camera we were asked for")
            return False

        # Probe with the camera's LAN address so the default bind lands on its
        # subnet; the per-camera choice between LAN and public happens later, in
        # ZiotCamera._pick_endpoint.
        probe = None
        if offline:
            # No broker to ask: the static endpoints ARE the camera LAN.
            probe = next(iter(static.values()))[0]
        else:
            try:
                probe = api.get_stun_addr(cams[0]["uid"]).get("IpcPrivateIP")
            except Exception as e:
                # The other unguarded cloud call that used to abort the whole boot.
                # The default-route fallback below is exactly the right answer here.
                log.warning("could not ask the broker where %s is (%s) — falling "
                            "back to the default route", cams[0]["uid"], e)
        if args.bind_ip:
            bind_ip = args.bind_ip
        elif probe:
            bind_ip = local_ip_for(probe)
        else:
            bind_ip = local_ip_for("8.8.8.8")
            if offline:
                log.warning("no --bind-ip given — binding on %s by default "
                            "route; pass --bind-ip if that is the wrong "
                            "interface", bind_ip)
            else:
                log.warning("no IpcPrivateIP from the broker — binding on %s by default "
                            "route; pass --bind-ip if that is the wrong interface", bind_ip)
        log.info("binding on %s (camera LAN %s), punch every %.1fs",
                 bind_ip, probe or "unknown", PUNCH_INTERVAL)

        cold: list[str] = []

        def boot_one(cam_rec):
            uid = cam_rec["uid"]
            try:
                z = ZiotCamera(api, cam_rec, bind_ip,
                               force_relay=args.force_relay,
                               relay_user=cfg.get("relay_user"),
                               relay_pass=cfg.get("relay_pass"),
                               static_addr=static.get(uid))
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
            if cloud is not None:
                cloud.register(z)

        threads = [threading.Thread(target=boot_one, args=(c,)) for c in cams]
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

        if cloud is not None:
            cloud.start()
        boot.set("ready", None)
        return True

    def retry_boot():
        """Wait out a cloud outage instead of dying into a restart loop.

        Exiting here and letting Docker restart us is the same retry loop with
        a worse duty cycle: every camera dark in between, and no /health
        answering to say why.
        """
        backoff = RECOVERY_INITIAL_BACKOFF
        attempt = 1
        while not stopping.is_set():
            stopping.wait(backoff)
            if stopping.is_set():
                return
            attempt += 1
            try:
                cams = api.list_cameras(user_id)
            except Exception as e:
                if is_auth_failure(e):
                    # We are already serving, so park and say so on /health
                    # rather than exiting into a loop that cannot help.
                    log.error("cloud rejected the token (%s) — check \"token\" "
                              "in %s; it is a JWT and they expire", e, args.config)
                    boot.set("failed", "the cloud rejected the token")
                    return
                boot.set("retrying", f"device list unavailable: {e}", attempt)
                log.warning("device list still unavailable (attempt %d): %s — "
                            "retrying in %.0fs", attempt, e, backoff)
                backoff = min(backoff * 2, RECOVERY_MAX_BACKOFF)
                continue
            log.info("reached the cloud after %d attempts", attempt)
            bring_up(cams)
            return

    # 8085 is what the README, the go2rtc examples and the watchdog assume.
    port = args.port or cfg.get("port", 8085)
    # Bind before bring_up: BootState exists precisely so /health can answer
    # "starting" while cameras are still rendezvousing, and the watchdog's
    # boot-phase logic depends on that -- a bridge that binds only after a
    # successful device list reads as dead during boot instead.
    http_host = cfg.get("http_host")
    if http_host is not None and not (isinstance(http_host, str) and http_host):
        log.error("http_host %r must be an interface address string -- "
                  "using 0.0.0.0", http_host)
        http_host = None
    http_host = http_host or "0.0.0.0"
    http_token = cfg.get("http_token")
    if http_token is not None and \
            not (isinstance(http_token, str) and http_token):
        log.error("http_token %r must be a non-empty string -- ignoring it; "
                  "media routes stay unauthenticated", http_token)
        http_token = None
    if http_token:
        log.info("media routes require the configured http_token; /health "
                 "stays open for the watchdog")
    server = ThreadingHTTPServer((http_host, port),
                                 make_handler(live, boot, lock, http_token))
    # A stalled viewer holds a thread per connection; Ctrl-C must not wait
    # on open MJPEG streams to exit.
    server.daemon_threads = True
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()
    log.info("serving on http://%s:%d/", http_host, port)

    if offline:
        cams = [{"uid": uid} for uid in static]
        if api is None:
            log.info("offline mode: %d LAN endpoint(s), punch-only (no token; "
                     "these cameras need notify+keepalive to stream)",
                     len(static))
        else:
            log.info("offline mode: %d LAN endpoint(s), using token for "
                     "notify/keepalive", len(static))
    else:
        try:
            cams = api.list_cameras(user_id)
        except Exception as e:
            if is_auth_failure(e):
                # Nothing is serving yet and no token un-expires itself, so this is
                # a config error in the same sense as an unreadable config file.
                log.error("cloud rejected the token (%s) — check \"token\" in %s; "
                          "it is a JWT and they expire", e, args.config)
                raise SystemExit(2)
            log.warning("device list unavailable at startup (%s) — serving /health "
                        "and retrying in the background", e)
            boot.set("retrying", f"device list unavailable: {e}", 1)
            cams = None

    if cams is not None:
        if not bring_up(cams):
            # Settled, not transient -- and deliberately exit 0: the roster
            # simply held nothing for us. See bring_up.
            return
    else:
        threading.Thread(target=retry_boot, daemon=True).start()

    with lock:
        serving = list(live)
    for uid in serving:
        log.info("  view  http://localhost:%d/view/%s", port, uid)
        log.info("  video http://localhost:%d/cam/%s", port, uid)
        log.info("  audio http://localhost:%d/audio/%s", port, uid)
    log.info("  health http://localhost:%d/health", port)
    try:
        # serve_forever runs in its own thread now; the main thread just
        # waits for it (Ctrl-C interrupts the join like it interrupted the
        # old inline serve_forever).
        serve_thread.join()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        stopping.set()
        server.shutdown()
        server.server_close()       # release the port before main returns
        if cloud is not None:
            cloud.stop()
        with lock:
            stopping_cams = list(live.values())
        for z in stopping_cams:
            z.stop()


if __name__ == "__main__":
    main()

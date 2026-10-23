#!/usr/bin/env python3
"""
ziot_fake_cloud.py -- local stand-in for the gps555.net cloud
=============================================================
A local stand-in for the vendor cloud, in two parts (see
DECOMPILATION_REPORT.md §8-§11):

  * the HTTP API (ipc.gps555.net) is ANSWERED, with response shapes taken
    from a live app capture: device list, send-stun-addr, stun-addr,
    notify-live-event, device detail, package record. Point the bridge at
    it with "api_base" in ziot_config.json and the rendezvous never leaves
    the LAN. Register cameras with --camera UID=IP[:PORT].
  * everything else (signalling 8882, STUN 13478, websocket 7080) only
    LOGS every byte the camera sends, so the camera->cloud protocol can be
    recovered from its own registration and keepalive traffic. These
    listeners answer nothing; what a valid answer looks like is unknown.

It also builds and writes the BLE provisioning frame (--provision) that
tells a camera to join WiFi and register with this host.

Endpoints impersonated (from DECOMPILATION_REPORT.md §4.1):

    ipc.gps555.net:80      HTTP API   (answered)
    sig.gps555.net:8882    signalling (logged only)
    stun.gps555.net:13478  STUN-like  (UDP, logged only)
    ws.gps555.net:7080     status websocket (logged only)

Point the router's DNS for *.gps555.net at this host and the camera's
own traffic lands here. Ports below 1024 need privileges: run under
sudo, or once: sysctl -w net.ipv4.ip_unprivileged_port_start=0

Usage:
    python3 ziot_fake_cloud.py [--http PORT] [--sig PORT] [--stun PORT]
                               [--ws PORT] [--dns PORT] [--log FILE]
                               [--camera UID=IP[:PORT]]
    python3 ziot_fake_cloud.py --provision --ssid S --pwd P --ble-mac MAC
"""
import argparse
import json
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

VENDOR_DOMAINS = ("ipc.gps555.net", "sig.gps555.net", "stun.gps555.net",
                  "stunaddr.gps555.net", "ws.gps555.net", "gps555.net")

# The addresses the vendor domains resolve to (2026-10-23). A camera
# reaches the cloud through these, so a static route in the router can
# point them at this host -- the camera then talks to us without ever
# changing its own configuration.
VENDOR_IPS = ("120.26.121.165",     # ipc.gps555.net
              "47.110.77.193",      # sig.gps555.net
              "47.238.125.111",    # stun.gps555.net, ws.gps555.net
              "8.130.23.234")      # stunaddr.gps555.net

_log_lock = threading.Lock()
_log_file = None


def log(kind: str, peer: tuple, data: bytes) -> None:
    """One line per datagram/PDU: hex plus printable ASCII."""
    stamp = time.strftime("%H:%M:%S")
    text = "".join(chr(b) if 32 <= b < 127 else "." for b in data[:96])
    hexed = data[:48].hex()
    line = f"{stamp} {kind:<7} {peer[0]}:{peer[1]} {len(data):>5}B  {text}  [{hexed}]"
    with _log_lock:
        print(line, flush=True)
        if _log_file:
            _log_file.write(line + "\n")
            _log_file.flush()


class StreamHandler(socketserver.BaseRequestHandler):
    """TCP: log each PDU and answer the vendor's HTTP API.

    The API answers are ground truth taken from a live app session against
    ipc.gps555.net (captured 2026-08-29, see DECOMPILATION_REPORT.md
    section 11): every response is {"requestId": ..., "code": 200, "msg":
    "OK", "data": ...}, and the endpoint set is exactly the six the
    bridge touches plus the device detail and package record.

    On the signalling port (8882) the peer is the camera, not the app, and
    its protocol is not the HTTP API -- it is not known yet. What is known
    is that the camera must be told where to send its media: the viewer
    registered that address through send-stun-addr. So a camera that asks
    for it by uid is answered with it, in the JSON shape the vendor uses
    elsewhere, and everything else is only logged.
    """

    def handle(self):
        peer = self.request.getpeername()
        buf = b""
        self.request.settimeout(30.0)
        while True:
            try:
                data = self.request.recv(65535)
            except socket.timeout:
                return
            except OSError:
                return
            if not data:
                return
            buf += data
            log(self.server.kind, peer, data)
            # Answer as soon as the headers are complete; the body of a
            # POST is small and arrives with the headers.
            while b"\r\n\r\n" in buf:
                head, _, rest = buf.partition(b"\r\n\r\n")
                request_line = head.split(b"\r\n")[0].decode("latin-1",
                                                              "replace")
                parts = request_line.split()
                if len(parts) < 2:
                    return
                method, target = parts[0], parts[1]
                path = target.split("?", 1)[0]
                query = target.split("?", 1)[1] if "?" in target else ""
                # The body length is the header's word, not "whatever
                # arrived with the headers": a POST split across two TCP
                # segments would otherwise be answered with half a body.
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        try:
                            length = int(line.split(b":", 1)[1].strip())
                        except ValueError:
                            length = 0
                if len(rest) < length:
                    buf = head + b"\r\n\r\n" + rest      # wait for more
                    break
                body, buf = rest[:length], rest[length:]
                if self.server.api is None:
                    return
                payload = self.server.api.respond(method, path, query, body)
                if payload is None:
                    return
                self.request.sendall(payload)
                if b"connection: close" in head.lower():
                    return


def _http_response(status: str, payload: bytes,
                   content_type: str = "application/json; charset=utf-8",
                   extra: list | None = None) -> bytes:
    """An HTTP response with the vendor's header set.

    Taken from the captured app session (DECOMPILATION_REPORT.md section
    11): nginx's own header block, plus the security headers it adds.
    Reproducing them keeps the client on the same footing as against the
    vendor -- in particular the no-cache headers, so a stale device list is
    never served from a cache.
    """
    stamp = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    lines = [
        f"HTTP/1.1 {status}",
        "Server: nginx/1.24.0 (Ubuntu)",
        f"Date: {stamp}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(payload)}",
        "Connection: keep-alive",
    ]
    if content_type.startswith("application/json"):
        lines += [
            "Access-Control-Allow-Origin: *",
            "Cache-Control: no-cache, no-store, max-age=0, must-revalidate",
            "Expires: Thu, 01 Jan 1970 00:00:00 GMT",
            f"Last-Modified: {stamp}",
            "X-Content-Type-Options: nosniff",
            "X-Xss-Protection: 1; mode=block",
        ]
    lines += extra or []
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + payload


class HttpApi:
    """The vendor's camera API, answered locally.

    Only what the rendezvous needs: the device list, the device detail,
    send-stun-addr, stun-addr, notify-live-event, the package record and
    the share list. The camera-side answers are the part that matters
    for breaking free of the cloud -- the camera asks the broker for its
    own address, and this is what it must be told.
    """

    def __init__(self):
        self.cameras: dict[str, dict] = {}
        # Where each camera's media should be delivered: the viewer that
        # registered through send-stun-addr.
        self.viewers: dict[str, dict] = {}
        self.seq = 0

    def add_camera(self, uid: str, ip: str, port: int = 0, pwd: str = "123456"):
        """Register a camera and the address it should be told about."""
        now = time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
        self.cameras[uid] = {
            # The field set and order are the camera's own, taken from a
            # live app session against the vendor (DECOMPILATION_REPORT.md
            # section 11). The app's CameraInfoModel.fromJson reads them by
            # name, so the names must match; the order keeps the record
            # comparable with the vendor's byte for byte.
            "uid": uid,
            "name": "",
            "email": "",
            "pwd": pwd,
            "deviceType": "X5",
            "version": "TXW817_A_V1.0.12.32",
            "familyId": 1,
            "terminalFamilyId": 3132031,
            "natType": "xbH7",
            "wifiSsid": "",
            "connectionState": "BR",
            "interpolation": "30w",
            "remark": "",
            "startTime": stamp,
            "createdAt": stamp,
            "updatedAt": stamp,
            "createBy": 0,
            "updateBy": 3132031,
            "commTime": stamp,
            "status": "",
            "relay_ip": "",
            "sd_total": "0.00GB",
            "sd_available": "0.00MB",
            "power": "100",
            "signal": "100%",
            "online_time": str(int(now)),
            "server_ip": "",
            "event_sensitivity": "1",
            "resolution": "",
            "onlineState": "1",
            "sdState": "0",
            "sdPolicy": "1",
            "infraredLight": "0",
            "lampLight": "",
            "vflip": "0",
            "hflip": "0",
            "led": "1",
            "mediaState": "0",
            "ringing": "",
            "ringingTime": "",
            "charged": "",
            "isShowRemoteService": False,
            "isRemoteServiceExpired": False,
            "remoteServiceEndTime": "0001-01-01T00:00:00Z",
            "unReadEventCount": 0,
            # What the broker hands back in stun-addr.
            "_ip": ip,
            "_port": port,
        }
        return self.cameras[uid]

    def _camera_row(self, uid: str) -> dict:
        row = dict(self.cameras.get(uid) or {})
        for key in ("_ip", "_port"):
            row.pop(key, None)
        return row

    def _stun(self, uid: str, receive_ts: bool = True) -> dict:
        """The camera's address, as the capture shows it.

        The two endpoints answer with slightly different shapes, and the
        difference is in the capture: send-stun-addr carries `receiveTs`,
        stun-addr carries `updateTime` instead.

        What comes back is the CAMERA's address, not the viewer's: in the
        capture the viewer registers 192.168.18.30:55795 and is answered
        with 192.168.18.75:53693, the camera's private address and the
        port its media comes from.
        """
        cam = self.cameras.get(uid) or {}
        self.seq += 1
        out = {
            "IpcPublicIP": cam.get("_ip", ""),
            "IpcPublicPort": cam.get("_port", 0),
            "IpcPrivateIP": cam.get("_ip", ""),
            "IpcPrivatePort": cam.get("_port", 0),
        }
        if receive_ts:
            out["receiveTs"] = int(time.time())
        out["seqNo"] = self.seq
        return out

    def _register_viewer(self, uid: str, one: dict) -> None:
        """Remember where the viewer wants the media delivered.

        The viewer's address is what the camera is told over the signalling
        connection, so it has to be kept: it is the one piece of state the
        broker cannot invent. In the capture the app registers
        publicIp=177.221.120.116, publicPort=64550, privateIp=192.168.18.30,
        privatePort=55795 -- the address its own media socket listens on.
        """
        try:
            public_port = int(one.get("publicPort") or 0)
            private_port = int(one.get("privatePort") or 0)
        except ValueError:
            public_port = private_port = 0
        self.viewers[uid] = {
            "publicIp": one.get("publicIp", ""),
            "publicPort": public_port,
            "privateIp": one.get("privateIp", ""),
            "privatePort": private_port,
        }
        log("viewer", ("-", 0),
            f"uid={uid} -> {one.get('privateIp')}:{private_port}".encode())

    def viewer_for(self, uid: str) -> dict:
        """The viewer registered for a camera, or an empty dict."""
        return dict(self.viewers.get(uid) or {})

    def respond(self, method: str, path: str, query: str,
                body: bytes) -> bytes | None:
        """Answer one HTTP request; returns the raw response bytes.

        The shapes come from a live app session against the vendor
        (DECOMPILATION_REPORT.md section 11): the envelope is
        {"requestId", "code", "msg", "data"}, the device list is
        {count, pageIndex, pageSize, list, showAd} and the vendor's
        trailing-slash quirk (a 301 to the same path without it) is
        reproduced, because the app follows it.
        """
        params = urllib.parse.parse_qs(query)
        one = {k: v[0] for k, v in params.items()}
        data = None
        msg = "OK"

        # /api/v1/ipc/?... draws a 301 to /api/v1/ipc?... in the vendor's
        # nginx; the app retries without the slash. Answering the same
        # way keeps the client on the path it expects.
        if path.endswith("/") and path.strip("/"):
            bare = path.rstrip("/")
            location = bare + (("?" + query) if query else "")
            body_html = (f'<a href="{location}">Moved Permanently</a>.\n\n'
                         ).encode()
            return _http_response(
                "301 Moved Permanently", body_html,
                content_type="text/html; charset=utf-8",
                extra=[f"Location: {location}"])

        if path == "/api/v1/ipc" and method == "GET":
            rows = [self._camera_row(uid) for uid in self.cameras]
            # The vendor answers "count": 0 even when the list holds a
            # camera -- it does not populate the field. Matching that
            # keeps the record comparable with the capture; the app and
            # the bridge both read "list" and never look at "count".
            data = {"count": 0, "pageIndex": 1, "pageSize": 1000,
                    "list": rows, "showAd": False}
        elif path == "/api/v1/ipc" and method == "POST":
            # The camera registers itself here.
            try:
                cam = json.loads(body or b"{}")
            except ValueError:
                cam = {}
            uid = str(cam.get("uid") or "")
            if uid:
                ip = cam.get("ip") or cam.get("IpcPrivateIP") or ""
                port = int(cam.get("port") or cam.get("IpcPrivatePort") or 0)
                self.add_camera(uid, ip, port, cam.get("pwd") or "123456")
            data = "ok"
        elif path == "/api/v1/ipc/send-stun-addr":
            uid = one.get("uid", "")
            # The viewer registers where it wants the media delivered; the
            # camera is told that address over the signalling connection.
            self._register_viewer(uid, one)
            data = self._stun(uid)
        elif path.startswith("/api/v1/ipc/stun-addr/"):
            uid = path.rsplit("/", 1)[-1]
            stun = self._stun(uid, receive_ts=False)
            stun["updateTime"] = time.strftime("%Y-%m-%d %H:%M:%S")
            data = stun
        elif path == "/api/v1/ipc/notify-live-event":
            uid = one.get("uid", "")
            event = one.get("eventType", "")
            log("notify", ("-", 0),
                f"uid={uid} event={event}".encode())
            data = "ok"
        elif path.startswith("/api/v1/ipc/viewer/"):
            # The camera's side of the rendezvous: where the media should
            # be delivered. The camera is told this over the signalling
            # connection; the path here exists so the address can be read
            # back and compared with what the camera receives.
            uid = path.rsplit("/", 1)[-1]
            viewer = self.viewer_for(uid)
            data = {
                "uid": uid,
                "publicIp": viewer.get("publicIp", ""),
                "publicPort": viewer.get("publicPort", 0),
                "privateIp": viewer.get("privateIp", ""),
                "privatePort": viewer.get("privatePort", 0),
            }
        elif path.startswith("/api/v1/ipc/"):
            uid = path.rsplit("/", 1)[-1]
            data = self._camera_row(uid)
        elif path == "/api/v1/share-list":
            data = []
        elif path == "/api/v1/cloud-service-record/current-package":
            uid = one.get("uid", "")
            data = {"uid": uid, "packageStatus": "3", "expiredDuration": "",
                    "packageInfo": []}
            # The vendor answers this one in Chinese, not "OK".
            msg = "查询成功"
        elif path == "/api/v1/cmd/send-cmd":
            data = "ok"
        else:
            data = None

        if data is None:
            payload = json.dumps({"requestId": "", "code": 404,
                                  "msg": "not found", "data": None}).encode()
        else:
            payload = json.dumps({"requestId": str(uuid.uuid4()), "code": 200,
                                  "msg": msg, "data": data}).encode()
        return _http_response("200 OK", payload)


class StreamServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, kind, api: "HttpApi | None" = None):
        self.kind = kind
        self.api = api
        super().__init__(addr, StreamHandler)


def _stun_xor_addr(ip: str, port: int, cookie: bytes, txid: bytes) -> bytes:
    """The address as RFC 5389's XOR-MAPPED-ADDRESS encodes it."""
    try:
        raw = socket.inet_aton(ip)
    except OSError:
        raw = b"\x00" * 4
    mask = cookie + txid[:4]
    xored = bytes(a ^ b for a, b in zip(raw, mask))
    return b"\x00\x01" + struct.pack("!H", port ^ 0x2112) + xored


def stun_binding_success(request: bytes, seen_ip: str, peer_port: int,
                         own_ip: str, server_port: int) -> bytes:
    """Build the vendor's STUN Binding Success for a Binding Request.

    From the capture (DECOMPILATION_REPORT.md section 11): the request is
    a bare 20-byte Binding Request (`0001 0000 <magic> <txid>`) and the
    answer is

        0101 0024 <magic> <txid>
        0001 0008 0001 <port> <ip>        MAPPED-ADDRESS: the peer as seen
        0004 0008 0001 <port> <ip>        this server's own address
        8020 0008 0001 <port> <ip>        the peer again, XOR-obfuscated

    The 0x8020 attribute is the vendor's own (XOR-MAPPED-ADDRESS lives at
    0x0020; they use the comprehension-optional 0x8020 range). Its port
    and address are XORed with the magic cookie and the top half of the
    transaction id, exactly like RFC 5389's XOR-MAPPED-ADDRESS.

    `seen_ip` and `peer_port` are the pair the peer is asking about: the
    address its request came from and the port it came from. `own_ip` is
    this server's address, which the peer learns at the same time.
    """
    if len(request) < 20:
        return b""
    msgtype = struct.unpack("!H", request[:2])[0]
    if msgtype != 0x0001:            # not a Binding Request
        return b""
    cookie = request[4:8]
    txid = request[8:20]

    def attr(atype: int, value: bytes) -> bytes:
        return struct.pack("!HH", atype, len(value)) + value

    def addr_field(ip: str, port: int) -> bytes:
        try:
            raw = socket.inet_aton(ip)
        except OSError:
            raw = b"\x00" * 4
        return b"\x00\x01" + struct.pack("!H", port) + raw

    body = b"".join([
        attr(0x0001, addr_field(seen_ip, peer_port)),
        attr(0x0004, addr_field(own_ip, server_port)),
        # The vendor's 0x8020 carries the port the peer's traffic arrives
        # on, XOR-obfuscated. On the vendor's side that is the NAT-mapped
        # port, which the server cannot know; here the peer is on the LAN
        # with us, so its source port is that port.
        attr(0x8020, _stun_xor_addr(seen_ip, peer_port, cookie, txid)),
    ])
    return struct.pack("!HH", 0x0101, len(body)) + cookie + txid + body


class StunHandler(socketserver.BaseRequestHandler):
    """Answer a STUN Binding Request the way the vendor's server does.

    The camera asks this server to tell it its own address -- that is what
    lets it register and be reached. Answering it is not optional for a
    camera that has to talk to our broker instead of the vendor's.
    """

    def handle(self):
        data, sock = self.request
        log("stun", self.client_address, data)
        reply = stun_binding_success(data, self.client_address[0],
                                     self.client_address[1],
                                     self.server.own_ip,
                                     self.server.server_address[1])
        if reply:
            sock.sendto(reply, self.client_address)
            log("stun", self.client_address, b"-> " + reply[:24].hex().encode())


class StunServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, own_ip: str = ""):
        self.own_ip = own_ip
        super().__init__(addr, StunHandler)


# --- DNS hijack + forward -------------------------------------------------
#
# The router's DHCP will hand clients this host as their DNS server. Every
# query for a vendor domain is answered locally (camera and app then connect
# to US); everything else is relayed to the real upstream unchanged.

def _dns_name(query: bytes) -> str | None:
    """Query name from a raw DNS packet, or None if malformed."""
    if len(query) < 12:
        return None
    labels = []
    i = 12
    try:
        while True:
            n = query[i]
            if n == 0:
                break
            if n & 0xC0:          # compression pointer: only in answers
                return None
            labels.append(query[i + 1:i + 1 + n].decode("ascii", "replace"))
            i += 1 + n
            if len(labels) > 8:
                return None
    except IndexError:
        return None
    return ".".join(labels) or None


def _dns_answer(query: bytes, ip: str) -> bytes:
    """Minimal A-record answer echoing the question section.

    The question section is the name *plus* QTYPE and QCLASS. Leaving
    those four bytes off makes the resolver read the answer's name pointer
    as the query type, and every answer is then rejected as malformed --
    dig reports it as "Question section mismatch: got .../TYPE49164/IN",
    and 49164 is 0xC00C, the compression pointer of the record below.

    Only an A query gets an A record. A query for anything else (AAAA,
    most of all) is answered with no records, which is what a resolver
    expects and what keeps the camera from caching a bogus answer.
    """
    name_end = 12
    while query[name_end]:
        name_end += 1 + query[name_end]
    name_end += 1              # the root label ends the name
    if name_end + 4 > len(query):
        raise ValueError("truncated question section")
    qtype = int.from_bytes(query[name_end:name_end + 2], "big")
    question = query[12:name_end + 4]     # name + QTYPE + QCLASS
    if qtype != 1:
        # Not an A query: answer with no records, which is what a resolver
        # expects and what keeps the camera from caching a bogus answer.
        # ANCOUNT is 0.
        header = (query[:2] + b"\x81\x80"
                  + b"\x00\x01\x00\x00\x00\x00\x00\x00")
        return header + question
    # ANCOUNT is 1: the record below is the answer, not trailing bytes.
    header = (query[:2] + b"\x81\x80"
              + b"\x00\x01\x00\x01\x00\x00\x00\x00")
    rr = (b"\xc0\x0c"                # name pointer to the question
          b"\x00\x01\x00\x01"        # type A, class IN
          b"\x00\x00\x00\x3c"        # TTL 60s
          b"\x00\x04" + socket.inet_aton(ip))
    return header + question + rr


class DnsHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        name = _dns_name(data)
        if name is None:
            return
        if name == "gps555.net" or name.endswith("." + "gps555.net"):
            answer = _dns_answer(data, self.server.my_ip)
            sock.sendto(answer, self.client_address)
            log("dns", self.client_address,
                f"{name} -> {self.server.my_ip}".encode())
            return
        # Forward untouched: same transaction id, real upstream.
        upstream = self.server.upstream
        if isinstance(upstream, str):
            upstream = (upstream, 53)
        try:
            up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            up.settimeout(5)
            up.sendto(data, upstream)
            reply, _ = up.recvfrom(65535)
            sock.sendto(reply, self.client_address)
            log("dnsfw", self.client_address, name.encode())
        except OSError:
            pass


class DnsServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, my_ip, upstream):
        self.my_ip = my_ip
        self.upstream = upstream
        super().__init__(addr, DnsHandler)


# --- BLE provisioning frame ------------------------------------------------
#
# From the vendor app's Dart code (Blutter, DECOMPILATION_REPORT.md §8): the
# app provisions over BLE with one frame format (CustomBleProtocolPacket),
# written in 20-byte chunks to the write characteristic:
#
#     86 86 | LEN16 | SEQ16 | DLEN16 | payload | XOR8
#     LEN16 = 5 + DLEN (seq + dlen + payload + xor); all multi-byte BE
#     XOR8  = xor of every preceding byte
#
# wifiConnect payload (cmd 0x00,0x01):
#     00 01 | <len><ssid utf-8> | <len><pwd utf-8> | <len><sigAddr utf-8> |
#     <tz minutes, negative biased +0x8000, big-endian> | [regCode 4B BE]
#
# The sigAddr is the signalling server the camera registers with -- the app
# sends `sig.gps555.net:8882`; we send our own address so the camera talks
# only to the local broker. The camera answers on the notify characteristic
# in exactly two notifications:
#     86 86 | LEN16 | SEQ16 | DLEN16 | 80 01 00 <len> <uid> | XOR8 | 0x0D


def _u8(n: int) -> bytes:
    return bytes([n])


def _be16(n: int) -> bytes:
    return bytes([(n >> 8) & 0xFF, n & 0xFF])


def _xor(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return x


def _lp(s: str) -> bytes:
    """One length-prefixed UTF-8 string."""
    raw = s.encode("utf-8")
    if len(raw) > 255:
        raise ValueError("field too long for u8 length prefix")
    return _u8(len(raw)) + raw


def wifi_connect_frame(ssid: str, pwd: str, sig_addr: str, tz_minutes: int,
                       seq: int = 1, reg_code: int = 0) -> bytes:
    # Negative minutes are biased +0x8000, not two's complement
    # (custom_ble_protocol_packet.dart 0x53ec64/0x53ecc4).
    t = tz_minutes + 0x8000 if tz_minutes < 0 else tz_minutes
    payload = (b"\x00\x01" + _lp(ssid) + _lp(pwd) + _lp(sig_addr) + _be16(t))
    if reg_code:
        payload += _be16(reg_code >> 16) + _be16(reg_code & 0xFFFF)
    body = _be16(seq) + _be16(len(payload)) + payload
    head = b"\x86\x86" + _be16(len(body) + 1)
    return head + body + _u8(_xor(head + body))


def chunk20(frame: bytes) -> list:
    """splitSend: 20-byte writes (MTU 1024 is requested first; 20 is the
    fallback the vendor app computes for)."""
    return [frame[i:i + 20] for i in range(0, len(frame), 20)]


def parse_provision_response(data: bytes) -> dict:
    """readBluetoothWriteResult + dealSubData: the camera's answer frame."""
    if len(data) < 10 or data[0] != 0x86 or data[1] != 0x86:
        raise ValueError("packet header != 0x86")
    dlen = (data[6] << 8) | data[7]
    if len(data) < 8 + dlen + 2:
        raise ValueError("packet truncated")
    body = data[:8 + dlen]
    if _xor(body) != data[8 + dlen]:
        raise ValueError("checksum error")
    if data[9 + dlen] != 0x0D:
        raise ValueError("invalid end symbol")
    sub = data[8:8 + dlen]
    if (sub[0] << 8 | sub[1]) != 0x8001:
        raise ValueError(f"invalid subByte header 0x{sub[0]:02x}{sub[1]:02x}")
    status = sub[2]
    uid_len = sub[3]
    uid = sub[4:4 + uid_len].decode("utf-8", "replace")
    return {"result": status == 0, "uid": uid}


def claim_vendor_ips(args) -> int:
    """Claim the vendor's addresses on this host and print the router routes.

    The camera reaches the cloud through fixed addresses (VENDOR_IPS). If
    this host owns those addresses and the router sends them here, the
    camera talks to the local broker instead of the vendor -- without the
    camera's own configuration being touched, and without DNS being
    involved at all.

    The address has to be local here so the kernel accepts the packet and
    the reply carries the vendor address as its source: the camera must see
    the vendor's address come back, or it drops the connection.

    The router needs one static route per address (TP-Link Deco: More >
    Advanced > Static Routing; destination, mask 255.255.255.255, gateway
    this host).
    """
    own = _detect_lan_ip()
    print(f"this host: {own}")
    print("\nAdd these static routes in the router "
          "(More > Advanced > Static Routing):\n")
    for ip in VENDOR_IPS:
        print(f"  destination {ip}  mask 255.255.255.255  gateway {own}")
    print()

    if args.claim_print_only:
        return 0

    failed = []
    for ip in VENDOR_IPS:
        cmd = ["sudo", "-n", "ip", "addr", "add", f"{ip}/32",
               "dev", args.claim_iface]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as e:
            print(f"  {ip}: could not run ip ({e})", file=sys.stderr)
            failed.append(ip)
            continue
        if done.returncode == 0:
            print(f"  {ip}: claimed on {args.claim_iface}")
            continue
        msg = (done.stderr or done.stdout).strip()
        # "Address already assigned" means the claim is in place -- the
        # command is meant to be re-runnable.
        if "already assigned" in msg:
            print(f"  {ip}: already claimed on {args.claim_iface}")
            continue
        detail = msg.splitlines()
        print(f"  {ip}: {detail[-1] if detail else 'failed'}",
              file=sys.stderr)
        failed.append(ip)

    print("\ncheck:", flush=True)
    try:
        shown = subprocess.run(["ip", "-brief", "addr", "show",
                                args.claim_iface],
                               capture_output=True, text=True).stdout
        for line in shown.splitlines():
            print("  " + line)
    except OSError:
        pass

    if failed:
        print(f"\n{len(failed)} address(es) not claimed: {', '.join(failed)}",
              file=sys.stderr)
        return 1
    return 0


def serve(kind: str, addr: tuple,
          api: "HttpApi | None" = None,
          own_ip: str = "") -> socketserver.BaseServer:
    srv = StunServer(addr, own_ip) if kind == "stun" \
        else StreamServer(addr, kind, api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"listening {kind} on {addr[0]}:{addr[1]}", flush=True)
    return srv


def _wait_forever() -> None:
    """Sleep until Ctrl-C. Module-level so tests can stub it."""
    threading.Event().wait()


def _local_tz_minutes() -> int:
    if time.daylight and time.localtime().tm_isdst:
        return -time.altzone // 60
    return -time.timezone // 60


def _detect_lan_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 53))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def _load_env(path: str = ".env") -> dict:
    """KEY=VALUE pairs from a .env file; comments and blanks ignored."""
    out = {}
    try:
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def ble_provision(args) -> int:
    """Build the wifiConnect frame and write it to the camera over BLE.

    Talks raw ATT over L2CAP CID 4 rather than going through a GATT client:
    bluetoothctl and btgatt-client both stall on these cameras, and the
    GATT layer holds the link so a raw connect is refused outright. Raw
    ATT also lands inside the pairing window, which is open for a few
    seconds at boot -- the connect itself is the trigger.

    Sequence: Exchange MTU, subscribe the notify characteristic (CCCD
    0x000b), then write the frame in 20-byte chunks to the command
    characteristic (0x0008 -- a write there is what makes the camera
    answer by notification). See DECOMPILATION_REPORT.md section 9 and 10.

    Credentials come from --flags or from .env (WIFI_SSID/WIFI_PWD/BLE_MAC).
    """
    env = _load_env()
    ssid = args.ssid or env.get("WIFI_SSID")
    pwd = args.pwd if args.pwd is not None else env.get("WIFI_PWD")
    ble_mac = args.ble_mac or env.get("BLE_MAC")
    if not (ssid and pwd is not None and ble_mac):
        print("--provision needs --ssid, --pwd and --ble-mac "
              "(or WIFI_SSID/WIFI_PWD/BLE_MAC in .env; see .env.example)",
              file=sys.stderr)
        return 2
    args.ssid, args.pwd, args.ble_mac = ssid, pwd, ble_mac
    sig_addr = args.sig_addr or f"{_detect_lan_ip()}:8882"
    tz = args.tz if args.tz is not None else _local_tz_minutes()
    frame = wifi_connect_frame(ssid, pwd, sig_addr, tz)
    chunks = chunk20(frame)
    print(f"frame: {frame.hex()}", flush=True)
    print(f"{len(chunks)} chunk(s) -> char 0x{ATT_WRITE_HANDLE:04x} on "
          f"{ble_mac}, sig-addr {sig_addr}, tz {tz} min", flush=True)

    for attempt in range(1, 4):
        result = _att_attempt(ble_mac, chunks)
        if result == "sent":
            break
        print(f"attempt {attempt}: {result} -- reconnecting", flush=True)
        time.sleep(2)
    else:
        print("link never stayed up long enough to complete the writes; "
              "reboot the camera into pairing mode and rerun", flush=True)
        return 1

    # The camera answers in exactly two notifications; drain for a while.
    print("written; waiting for the uid reply (or check `ip neigh` for the "
          "join and the broker log for its registration)", flush=True)
    return 0


def _read_line(proc, timeout: float):
    """One stdout line or None after `timeout` seconds (select-based)."""
    import select
    r, _, _ = select.select([proc.stdout], [], [], timeout)
    if not r:
        return None
    return proc.stdout.readline()


# --- raw ATT over L2CAP CID 4 ---------------------------------------------
#
# A GATT client (bluetoothctl, btgatt-client) holds the link and stalls on
# these cameras, so we speak ATT directly. The socket is SOCK_SEQPACKET:
# one recv() is exactly one PDU, and a short recv() DISCARDS the rest of
# the datagram. Reading a PDU byte by byte therefore truncates every
# multi-byte PDU to its first byte -- an MTU response reads as 0x03 and a
# notification as 0x1b with no handle and no value. Always recv() a whole
# PDU.
#
# PDU layouts used here (all multi-byte fields little-endian, unlike the
# vendor frame inside the value, which is big-endian):
#     Exchange MTU Request   02 | mtu(2)
#     Write Request          12 | handle(2) | value...
#     Write Response         13
#     Error Response         01 | request opcode | handle(2) | error code
#     Handle Value Notify    1B | handle(2) | value...

ATT_ERROR_RESPONSE = 0x01
ATT_MTU_REQUEST = 0x02
ATT_WRITE_REQUEST = 0x12
ATT_WRITE_RESPONSE = 0x13
ATT_NOTIFY = 0x1B

ATT_WRITE_HANDLE = 0x0008   # the command channel: a write here is what
ATT_CCCD_HANDLE = 0x000B    # makes the camera answer by notification
ATT_MAX_PDU = 512
ATT_DRAIN_S = 15.0         # how long to listen for the camera's reply


def _att_read(sock, timeout: float) -> bytes:
    """One whole ATT PDU, or b"" on timeout."""
    sock.settimeout(timeout)
    try:
        return sock.recv(ATT_MAX_PDU)
    except socket.timeout:
        return b""


def _att_is_peer_mtu_request(pdu: bytes) -> bool:
    """True for an Exchange MTU Request the *camera* sent us.

    These cameras emit `02 17 00` in the middle of our handshake, where a
    write response is expected. It must be skipped, not taken as the
    response -- and NOT answered: replying with an MTU Response (0x03)
    makes the camera answer `01 03 .. .. 06` (Request Not Supported),
    which then lands in place of the next write's response.
    """
    return len(pdu) >= 3 and pdu[0] == ATT_MTU_REQUEST


def _att_request(sock, opcode: int, payload: bytes, timeout: float,
                 notes: list) -> bytes:
    """Send one request and return its response PDU (or b"" on timeout).

    A notification can arrive before the response it was triggered by, so
    PDUs that are not the response are collected into `notes` as
    (handle, value) instead of being mistaken for the response. The
    camera's own MTU request is answered and skipped the same way.
    """
    sock.send(bytes([opcode]) + payload)
    end = time.time() + timeout
    while time.time() < end:
        pdu = _att_read(sock, max(end - time.time(), 0.05))
        if not pdu:
            continue
        if pdu[0] == ATT_NOTIFY and len(pdu) >= 3:
            notes.append((struct.unpack("<H", pdu[1:3])[0], pdu[3:]))
            continue
        if _att_is_peer_mtu_request(pdu):
            continue
        return pdu
    return b""


def _att_attempt(ble_mac: str, chunks: list) -> str:
    """One connect/write cycle over raw ATT. Returns 'sent' or a reason."""
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                         socket.BTPROTO_L2CAP)
    sock.bind(("00:00:00:00:00:00", 0, 4, 1))
    sock.settimeout(3.0)
    try:
        sock.connect((ble_mac, 0, 4, 1))
    except OSError as e:
        sock.close()
        return f"connect failed ({e})"

    notes: list = []
    try:
        mtu = _att_request(sock, ATT_MTU_REQUEST, struct.pack("<H", 247),
                           2.0, notes)
        print(f"mtu: {mtu.hex() if mtu else 'silent'}", flush=True)

        # Subscribe the notify characteristic before writing, so the
        # camera's reply cannot be missed. The CCCD value is 2 bytes.
        cccd = _att_request(
            sock, ATT_WRITE_REQUEST,
            struct.pack("<H", ATT_CCCD_HANDLE) + b"\x01\x00", 2.0, notes)
        print(f"cccd: {cccd.hex() if cccd else 'silent'}", flush=True)
        if cccd and cccd[0] == ATT_ERROR_RESPONSE:
            return "notify subscription rejected"

        for i, chunk in enumerate(chunks):
            rsp = _att_request(
                sock, ATT_WRITE_REQUEST,
                struct.pack("<H", ATT_WRITE_HANDLE) + chunk, 2.0, notes)
            print(f"chunk {i}: {rsp.hex() if rsp else 'silent'}", flush=True)
            if rsp and rsp[0] == ATT_ERROR_RESPONSE:
                return f"write {i} rejected"

        # Drain the camera's answer.
        end = time.time() + ATT_DRAIN_S
        while time.time() < end:
            pdu = _att_read(sock, 1.0)
            if pdu and pdu[0] == ATT_NOTIFY and len(pdu) >= 3:
                notes.append((struct.unpack("<H", pdu[1:3])[0], pdu[3:]))
            elif pdu and _att_is_peer_mtu_request(pdu):
                print(f"camera MTU request (ignored): {pdu.hex()}", flush=True)
            elif pdu:
                print(f"event: {pdu.hex()}", flush=True)

        buf = b""
        for handle, value in notes:
            buf += value
            print(f"notify {handle:#06x}: {value.hex()}", flush=True)
        if buf:
            try:
                print(f"reply: {parse_provision_response(buf)}", flush=True)
            except (ValueError, IndexError) as e:
                print(f"reply not parseable: {e}", flush=True)
        return "sent"
    except OSError as e:
        return f"link failed ({e})"
    finally:
        sock.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--http", type=int, default=80, help="HTTP API port (0=off)")
    ap.add_argument("--sig", type=int, default=8882, help="signalling port (0=off)")
    ap.add_argument("--stun", type=int, default=13478, help="STUN UDP port (0=off)")
    ap.add_argument("--ws", type=int, default=7080, help="websocket port (0=off)")
    ap.add_argument("--dns", type=int, default=53,
                    help="DNS hijack port, 0=off (needs root; answer "
                         "*.gps555.net with --bind-ip, forward the rest)")
    ap.add_argument("--bind-ip", default=None,
                    help="IP handed out for vendor domains (default: the "
                         "address of the default route)")
    ap.add_argument("--upstream-dns", default=None,
                    help="real DNS server for non-vendor queries "
                         "(default: first nameserver in /etc/resolv.conf)")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--log", default=None, help="also write lines to this file")
    ap.add_argument("--claim-ips", action="store_true",
                    help="claim the vendor's addresses on this host so a "
                         "static route in the router can point them here, "
                         "and print the route entries to add. Needs "
                         "privileges for 'ip addr add'")
    ap.add_argument("--claim-iface", default="wlan0",
                    help="interface to claim the vendor addresses on")
    ap.add_argument("--claim-print-only", action="store_true",
                    help="print the route entries without touching the host")
    ap.add_argument("--provision", action="store_true",
                    help="skip the listeners: build the BLE wifiConnect "
                         "frame, write it to the camera in 20-byte chunks "
                         "over raw ATT, and print its reply. Requires "
                         "--ssid/--pwd/--ble-mac; --sig-addr defaults to "
                         "this host's LAN address")
    ap.add_argument("--ssid", default=None)
    ap.add_argument("--pwd", default=None)
    ap.add_argument("--ble-mac", default=None, dest="ble_mac",
                    help="camera's BLE MAC (the advertising address)")
    ap.add_argument("--tz", type=int, default=None,
                    help="timezone offset in minutes (default: local)")
    ap.add_argument("--sig-addr", default=None, dest="sig_addr",
                    help="signalling server for the camera to register with "
                         "(default: <LAN IP>:8882 -- the local broker)")
    ap.add_argument("--camera", action="append", default=[], metavar="UID=IP",
                    help="a camera the API should know about, and the "
                         "address it is told to send media to. Repeatable. "
                         "A camera that registers itself over HTTP does not "
                         "need this")
    args = ap.parse_args()

    if args.provision:
        return ble_provision(args)

    if args.claim_ips:
        return claim_vendor_ips(args)

    global _log_file
    if args.log:
        _log_file = open(args.log, "a", buffering=1)

    my_ip = args.bind_ip
    if my_ip is None:
        # The address the default route uses: what LAN peers can reach.
        # The STUN server hands it to the camera as its own address.
        my_ip = _detect_lan_ip()
    if args.dns:
        if args.upstream_dns:
            upstream = args.upstream_dns
        else:
            upstream = None
            for line in open("/etc/resolv.conf"):
                if line.startswith("nameserver"):
                    upstream = line.split()[1]
                    break
            upstream = upstream or "1.1.1.1"

    api = HttpApi()
    for spec in args.camera:
        uid, _, ip = spec.partition("=")
        if not uid or not ip:
            print(f"--camera {spec!r} must be UID=IP", file=sys.stderr)
            return 2
        ip, _, port = ip.partition(":")
        api.add_camera(uid.strip(), ip.strip(),
                       int(port) if port.strip() else 0)
        print(f"camera {uid.strip()} -> {ip.strip()}"
              f"{':' + port.strip() if port.strip() else ''}", flush=True)

    servers = []
    for kind, port in (("http", args.http), ("sig", args.sig),
                       ("ws", args.ws), ("stun", args.stun)):
        if port:
            try:
                servers.append(serve(kind, (args.bind, port),
                                      api if kind == "http" else None,
                                      my_ip or _detect_lan_ip()))
            except OSError as e:
                print(f"cannot bind {kind} port {port}: {e} -- ports below "
                      "1024 need root (see module docstring)", file=sys.stderr)
    if args.dns:
        try:
            srv = DnsServer((args.bind, args.dns), my_ip, upstream)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            servers.append(srv)
            print(f"listening dns on {args.bind}:{args.dns} -- "
                  f"*.gps555.net -> {my_ip}, rest -> {upstream}", flush=True)
        except OSError as e:
            print(f"cannot bind dns port {args.dns}: {e} -- port 53 needs "
                  "root", file=sys.stderr)
    if not servers:
        print("nothing to listen on", file=sys.stderr)
        return 1
    print(f"impersonating {', '.join(VENDOR_DOMAINS)} -- waiting for the "
          "camera's traffic", flush=True)
    try:
        _wait_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

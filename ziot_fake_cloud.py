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
    sig.gps555.net:8882    signalling (answered: the vendor's frames,
                           recovered from the firmware -- see
                           ziot-fw/SIGNALLING_PROTOCOL.md. The `0x8000`
                           registration answer arms the media target and
                           the unsolicited `0x8005` starts the push)
    stun.gps555.net:13478  STUN       (BINDING answered as the vendor's
                           server does)
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
import hashlib
import json
import os
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

# socket.IP_PKTINFO only exists from Python 3.12; the Docker image runs
# 3.11, where touching it raised AttributeError and crash-looped the
# broker (2026-10-25 cutover). 8 is its value on Linux, the only platform
# the STUN reply path targets; elsewhere setsockopt fails with OSError,
# which the callers already treat as "no destination address".
IP_PKTINFO = getattr(socket, "IP_PKTINFO", 8)

VENDOR_DOMAINS = ("ipc.gps555.net", "sig.gps555.net", "stun.gps555.net",
                  "stunaddr.gps555.net", "ws.gps555.net", "gps555.net")

# The addresses the vendor domains resolve to (2026-10-23). A camera
# reaches the cloud through these, so a static route in the router can
# point them at this host -- the camera then talks to us without ever
# changing its own configuration.
VENDOR_IPS = ("120.26.121.165",     # ipc.gps555.net
              "47.110.77.193",      # sig.gps555.net (the older answer)
              "47.97.71.82",       # sig.gps555.net (the current answer)
              "47.238.125.111",    # stun.gps555.net, ws.gps555.net
              "8.130.23.234")      # stunaddr.gps555.net

_log_lock = threading.Lock()
_log_file = None


def log(kind: str, peer: tuple, data: bytes) -> None:
    """One line per datagram/PDU: hex plus printable ASCII."""
    stamp = time.strftime("%H:%M:%S")
    # The ASCII column is trimmed so the hex column stays readable, but
    # the HEX is complete: the frames are longer than the column, and
    # truncating them is what hid the port slots of the 0x8000 answer.
    text = "".join(chr(b) if 32 <= b < 127 else "." for b in data[:96])
    hexed = data.hex()
    line = (f"{stamp} {kind:<7} {peer[0]}:{peer[1]} {len(data):>5}B  "
            f"{text}  hex={hexed}")
    with _log_lock:
        print(line, flush=True)
        if _log_file:
            _log_file.write(line + "\n")
            _log_file.flush()


def _looks_http(head: bytes) -> bool:
    """True when the request head is plainly an HTTP request.

    The signalling and websocket ports carry the camera's own protocol, so
    the broker must not answer them with HTTP. A request line of
    `METHOD /target HTTP/1.x` is unmistakable, though, and those are our
    own health checks -- and the browser request that proves a router
    route works.
    """
    line = head.split(b"\r\n", 1)[0].split()
    if len(line) < 3:
        return False
    return (line[0] in (b"GET", b"POST", b"HEAD", b"OPTIONS", b"PUT",
                        b"DELETE")
            and line[1].startswith(b"/")
            and line[2].startswith(b"HTTP/"))


def signalling_frame(cmd: int, seq: int, uid: bytes, body: bytes,
                     length: int | None = None, pad: bool = True) -> bytes:
    """Build a signalling frame in the camera's own format.

    Read off the wire (2026-10-23, both cameras, live):

        86 86 | cmd(2) | len(2) | seq(2) | uidLen(2) | uid | body | xor | 0x0D
              0001     002c     0019..    000c        ...    ...

    `cmd` is 0x0001 for the camera's registration (hello) to the
    signalling server; `len` is the length of what follows the header --
    the uid plus the body (12 + 32 = 44 = 0x002c here); `seq` is the
    camera's own counter and increments on every retransmission; `uidLen`
    is the uid's length.

    The field order is the BLE frame's: magic, LEN, SEQ, then the
    payload's own length. Everything is big-endian, the XOR covers every
    byte before it, and the 0x0D end symbol closes the frame.

    `pad=False` builds the variable-length form -- the frame ends after
    the body -- which is what a long body needs: the punch instruction
    with a real address does not fit in a 32-byte body area.
    """
    body = body[:32] if pad else body
    if pad:
        body = body.ljust(32, b"\x00")
    if length is None:
        # The length counts what follows the header: the uid and the
        # padded body (12 + 32 = 44 = 0x002c in the camera's frames).
        length = len(uid) + len(body)
    head = (b"\x86\x86" + struct.pack("!HHH", cmd, length, seq)
            + struct.pack("!H", len(uid)) + uid)
    return head + body + bytes([_xor(head + body)]) + b"\x0d"


# The camera registers with cmd 0x0001 (uid + firmware version) and
# retransmits it about once a second, bumping `seq`, until the server
# answers. The firmware names what the server sends --
#
#     cSeq:%d, cmd:%d, ack:%d          an acknowledgement
#     seq:%d, stun port:%d stun ip:%s  where the camera should punch
#     req cmd:%04X, seq:%d len:%d      a request to the camera
#
# -- but these are printf *format* strings, not the wire bytes. The proof
# is the sibling string `cmd:%04X, len:%d, uidLen:%d`: it describes the
# frame header we read straight off the wire (86 86 | 0001 | 002c | ...),
# which is binary u16s, so these `%d`/`%s` strings are log templates for
# already-parsed fields -- the reply body is binary, not the rendered
# text. (The rendered text was tried first: the camera ignored every one
# of 2000+ such answers and never stopped retransmitting cmd 0x0001.) So
# the bodies below are packed integers, and the search is over how the
# fields sit and under which command id. _answer_camera watches for the
# camera to stop retransmitting / send a new command -- the only signal
# that an answer landed.
ACK_HOLD_S = 6.0   # how long one candidate answer is held before the next

# The camera's own connection cycle, measured live on 2026-10-23 (both
# cameras): register, burst its four handshake commands, then go SILENT
# for 40-90 s while it waits for the server's push, then register again on
# a (new or re-opened) connection. A connection that closes before the
# push has a place to land is the bug the two constants below fix: their
# old values (a 10 s timeout and no cleanup) made every push write into a
# closed socket -- the log's 22 `Bad file descriptor` failures.
STREAM_IDLE_S = 75.0   # a camera connection is closed after this much
                       # silence -- just above the longest observed idle gap
# The camera's session dies in about twelve seconds without a message
# every two seconds -- the same interval the app's keepalive uses.
KEEPALIVE_INTERVAL = 2.0

# The `0x8005` media trigger waits this long after the register that
# armed it: a fire that lands within the handshake breaks the session
# (2026-10-24, live), and the vendor only ever triggers once a viewer
# has asked for the stream -- minutes into a settled session. One
# pending fire per camera, at most.
RELAY_START_DELAY_S = 5.0

# How long the direct chain waits for the camera to report its P2P
# socket bound (mediaState 3) before the pending play is dropped.
# Sending 0x8004 on this timer, without that report, is what the
# firmware rejects as "p2p is not finish" and what stopped a view that
# was still coming up (§16.3). The play goes out only after the report.
P2P_PLAY_FALLBACK_S = 6.0

# At most one 0x8101 per this many seconds for an unchanged viewer pair.
# The bridge retries every 5/10/20 s while it sees no media, and each
# retry re-asks for a 0x8101; a burst of them on a camera that is live
# or coming up has taken it off the network until power-cycled
# (`SIGNALLING_PROTOCOL.md` §18.7). The vendor app sends one a session.
P2P_RESEND_MIN_S = 30.0

# How long after that report the play waits. The camera's freshest RTP
# port (its 0x0003) lands just after the report, and the viewer's hello
# has to reach that port before the play -- PlayLiveview closes the
# socket the camera was waiting on (§13.3).
P2P_PLAY_SETTLE_S = 1.5

# The sig socket is a TCP stream, so the camera coalesces queued frames
# into one read (fifteen 0x0002 pings and a 0x0004 arrived as one
# 389-byte chunk, 2026-10-24 19:40:11) and a frame can straddle two
# reads. split_signalling() cuts the complete frames out of that stream;
# a length field that would make a frame larger than this is a corrupt
# header rather than a frame that will never close, so the splitter
# resynchronises on the next magic instead of buffering forever.
SIG_FRAME_MAX = 4096


# The account token the local cloud issues. The camera never sees a
# token -- its firmware has no HTTP client and no "Authorization" string
# at all (checked in the firmware, 2026-10-23) -- so the credential is
# only between the app (or the bridge) and this broker. Minting one
# locally is what makes the local cloud a real cloud: the client
# authenticates, and the broker checks the token it issued.
#
# The vendor's own token is a JWT signed HS256; the payload carries the
# account's e-mail and the expiry. This one is the same shape, so the
# client code (which reads the user id out of the payload) needs no
# change, and the signature is ours.
LOCAL_TOKEN_SECRET = b"ziot-local-cloud"


def mint_local_token(user_id: int = 3132031,
                     email: str = "mauriciobc@gmail.com",
                     ttl: int = 30 * 24 * 3600) -> str:
    """Mint a token this broker accepts, in the vendor's JWT shape.

    The vendor's own token is an HS256 JWT whose payload carries the
    account's e-mail, the user id, the family id, the role, the language
    and the expiry (read from a live token on 2026-10-23). This one has
    the same claims -- so the client code, which reads the user id out
    of the payload, needs no change -- and the signature is ours.
    """
    import base64
    import hashlib
    import hmac

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    now = int(time.time())
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"},
                             separators=(",", ":")).encode())
    payload = b64(json.dumps({
        "email": email,
        "exp": now + ttl,
        "family_id": 0,
        "lang": "pt",
        "orig_iat": now,
        "role_id": 2,
        "user_id": user_id,
        "username": email,
    }, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = hmac.new(LOCAL_TOKEN_SECRET, signing_input,
                          hashlib.sha256).digest()
    return f"{header}.{payload}.{b64(signature)}"


def local_token_valid(token: str) -> bool:
    """True when the token is one this broker issued and it has not expired."""
    import base64
    import hashlib
    import hmac
    try:
        header, payload, signature = token.split(".")
    except ValueError:
        return False
    signing_input = f"{header}.{payload}".encode()
    want = hmac.new(LOCAL_TOKEN_SECRET, signing_input, hashlib.sha256).digest()
    try:
        got = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except Exception:
        return False
    if not hmac.compare_digest(want, got):
        return False
    try:
        pad = "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload + pad))
    except Exception:
        return False
    return int(claims.get("exp", 0)) > time.time()


# Tokens presented here that this broker did not issue: warned about
# once each, so a bridge still carrying the vendor's own JWT is noticed
# in the log without a line on every request.
_warned_tokens: set[str] = set()


def _warn_bad_token(token: str) -> None:
    """Log the first use of a token this broker did not mint."""
    if token in _warned_tokens:
        return
    _warned_tokens.add(token)
    log("auth", ("-", 0), (
        f"token not issued by this broker (len={len(token)}); a locally "
        "minted one is printed by `python3 ziot_fake_cloud.py --mint-token`"
    ).encode())


def _ack_candidates(uid: bytes, seq: int, req_len: int,
                    viewer: dict | None = None):
    """(name, frame) pairs to try as the answer to one camera frame.

    All bodies are binary (see the note above). Acknowledgements come
    first and most-likely-first -- the camera will not move on until its
    registration is acked -- then the punch instruction, which only
    matters once the ack lands. `seq` is the camera's own counter, echoed
    back so the answer refers to the outstanding request. Each body is
    tried with the uid echoed and without, since it is not known whether
    the ack carries it.
    """
    out = []
    # Acknowledgement bodies, most-plausible first. The firmware logs
    # (cSeq, cmd, ack) for an ack, so try those as big-endian u16 in the
    # plausible subsets; an empty body and a single status byte cover the
    # terser encodings.
    ack_bodies = [
        ("empty",         b""),
        ("stat0",         b"\x00"),
        ("cmd_ack0",      struct.pack("!HH", 1, 0)),
        ("cseq_cmd_ack0", struct.pack("!HHH", seq, 1, 0)),
        ("stat1",         b"\x01"),
        ("cmd",           struct.pack("!H", 1)),
        ("cmd_ack1",      struct.pack("!HH", 1, 1)),
        ("cseq_cmd_ack1", struct.pack("!HHH", seq, 1, 1)),
    ]
    # The response-bit form (0x8001) first, then a plain echo of the
    # received command (0x0001).
    for rcmd in (0x8001, 0x0001):
        for bname, body in ack_bodies:
            for who, tag in ((uid, "uid"), (b"", "nouid")):
                out.append((f"ack-{rcmd:04x}-{tag}-{bname}",
                            signalling_frame(rcmd, seq, who, body, pad=False)))
    # Same acks padded to the camera's own 44-byte layout (uid + 32), in
    # case the camera wants a fixed-size frame back.
    for rcmd in (0x8001, 0x0001):
        out.append((f"ack-{rcmd:04x}-uid-padded",
                    signalling_frame(rcmd, seq, uid,
                                     struct.pack("!HHH", seq, 1, 0), pad=True)))
    # Punch / stun push: tell the camera where to send its media. The
    # viewer address the app registered through send-stun-addr is that
    # target. The firmware logs (seq, port, ip); pack the port as a u16
    # and the ip as its four octets. Only useful once the ack lands.
    if viewer:
        ip = viewer.get("privateIp") or viewer.get("publicIp") or ""
        try:
            port = int(viewer.get("privatePort")
                       or viewer.get("publicPort") or 0)
        except (TypeError, ValueError):
            port = 0
        octets = ip.split(".")
        ipb = (bytes(int(o) for o in octets)
               if len(octets) == 4
               and all(o.isdigit() and int(o) < 256 for o in octets)
               else b"")
        if ipb and port:
            for rcmd in (0x0002, 0x8002):
                out.append((f"punch-{rcmd:04x}-seq_port_ip",
                            signalling_frame(rcmd, seq, uid,
                                             struct.pack("!HH", seq, port)
                                             + ipb, pad=False)))
                out.append((f"punch-{rcmd:04x}-port_ip",
                            signalling_frame(rcmd, seq, uid,
                                             struct.pack("!H", port) + ipb,
                                             pad=False)))
        # The camera's own body is a NUL-padded 32-byte *string* (its
        # firmware version), so the answer's body is most likely a
        # string too -- and "ip:port" is the one form of the address
        # that fits in 32 bytes.
        if ip and port:
            addr = f"{ip}:{port}".encode()
            out.append(("addr-string-padded",
                        signalling_frame(1, seq, uid, addr, pad=True)))
            out.append(("addr-string",
                        signalling_frame(1, seq, uid, addr, pad=False)))
            out.append(("addr-string-8001-padded",
                        signalling_frame(0x8001, seq, uid, addr, pad=True)))
    return out


def vendor_registration_answer(uid: str, seq: int, relay_ip: str,
                                relay_port: int, relay_port2: int,
                                stun_host: str, stun_port: int,
                                sig_ip: str, sig_tail: int,
                                lead: int | None = None) -> bytes:
    """The vendor's own answer to a camera's registration, byte for byte.

    Captured from the vendor's signalling server on 2026-10-23: the
    camera's registration frame sent to sig.gps555.net, and the answer
    logged verbatim. `SignallingFrameTests` rebuilds that capture
    exactly.

    The answer is a frame with the response bit set in the command
    (`0x8000`), the camera's own sequence number, the uid echoed, and a
    body of **94 bytes** for the camera's dispatcher
    (`ziot-fw/SIGNALLING_PROTOCOL.md` §5) -- flat fixed-width fields,
    not type-tagged ones:

        u16 seq                             the camera's seq, echoed
        relay ip  (20-byte ASCII) + u16 relay port + u16 relay port
        stun host (32-byte ASCII) + u16 stun port
        sig  ip  (32-byte ASCII) + u16 sig port

    (Each ASCII field is a NUL-padded fixed-width slot; 2 + 24 + 34 + 34
    = 94. The frame's `len` field is uid + body = 106 = 0x6a.)

    The body's first u16 is an echo of the request's sequence number
    (§5 reads it and logs it as `seq:%d`; the vendor's answer capture
    carries `00 01` both in the frame header and at body offset 0 --
    and the handler at `0x1801b55a` logs body[0] with the same `seq:`
    label). Mismatched answers are the prime suspect for a camera that
    keeps retransmitting its register, so `lead=None` now echoes the
    request's seq.

    The apparent "tags" in the capture (0x02, 0x34) are the relay
    port's high byte and the '4' of "47.97.71.82".
    """
    def slot(value: str, width: int) -> bytes:
        """A NUL-padded fixed-width ASCII slot."""
        return value.encode().ljust(width, b"\x00")[:width]

    body = struct.pack("!H", seq if lead is None else lead)
    body += slot(relay_ip, 20) + struct.pack("!HH", relay_port, relay_port2)
    body += slot(stun_host, 32) + struct.pack("!H", stun_port)
    body += slot(sig_ip, 32) + struct.pack("!H", sig_tail)
    return signalling_frame(0x8000, seq, uid.encode(), body, pad=False)


def vendor_time_answer(uid: str, seq: int, now: float | None = None) -> bytes:
    """The vendor's answer to the camera's `cmd 0x0021`, byte for byte.

    Captured live from sig.gps555.net on 2026-10-23: the camera asks
    with cmd 0x0021 and a one-byte counter, and the server answers with
    the same command plus the response bit (`0x8021`), the camera's own
    sequence number, the uid, and a **big-endian u32 holding the
    server's clock** (the capture was 21 seconds old when it arrived).

    The camera sends this after it has registered, so answering it is
    what keeps the session alive.
    """
    if now is None:
        now = time.time()
    body = struct.pack("!I", int(now))
    return signalling_frame(0x8021, seq, uid.encode(), body, pad=False)


def vendor_pullvideo_answer(uid: str, seq: int, cseq: int | None = None) -> bytes:
    """The vendor's answer to the camera's `cmd 0x0002` (pullVideo).

    Captured live from sig.gps555.net on 2026-10-23: the camera's own
    pullVideo frame sent to the vendor, and the answer logged verbatim.
    The answer is the command with the response bit set (`0x8001`), the
    camera's sequence number, the uid, and a five-byte body:

        cSeq:2 | cmd:2 | ack:1

    **`cSeq` is the camera's own sequence number** -- its latest, not
    necessarily the one of the frame being answered: the camera counts
    up once per frame, and the answer can lag. In all 1134 acks of the
    2 h vendor wiretap the body's first u16 equals the camera's latest
    sequence number for that command (535 of them `00 03 00 02 00` and
    535 `00 04 00 04 00`, the counter usually sitting at 3 and 4, with a
    tail of `00 04 00 02 00`, `00 05 00 04 00`, `00 08 00 02 00`).
    `cmd` is the request's own id.

    The frame's own header sequence echoes the frame that is being
    answered; the two can differ, as in the captured answer below,
    whose header says seq 2 and whose body says cSeq 3.

    An ack whose cSeq does not match the camera's counter is an ack of
    nothing. Ours were constant, and the camera answered them by
    re-asking: thirty `0x0002`s in a burst on 2026-10-24.
    """
    if cseq is None:
        cseq = seq
    body = struct.pack("!HH", cseq, 0x0002) + b"\x00"
    return signalling_frame(0x8001, seq, uid.encode(), body, pad=False)


def vendor_pushvideo_answer(uid: str, seq: int, url: str = "") -> bytes:
    """The vendor's answer to the camera's `cmd 0x0020` (pushVideo).

    Captured live from sig.gps555.net on 2026-10-23: the camera asks to
    push its video and the server answers with the same command plus the
    response bit and a five-byte body:

        00 <cmd:2> <status:1>

    The vendor's answer carried status 3 -- "no URL for this camera" --
    because the camera was not registered with it. The app's own map for
    this message has the keys `cmd` and `url`, so a server that *has* a
    URL puts it in the body.

    With a URL the body is the same shape with the status that says
    "here it is" and the URL after it; the app reads `url` and plays it.
    """
    if not url:
        body = b"\x00" + struct.pack("!H", 0x20) + b"\x03"
    else:
        body = b"\x00" + struct.pack("!H", 0x20) + b"\x01" + url.encode()
    return signalling_frame(0x8001, seq, uid.encode(), body, pad=False)


def vendor_status_answer(uid: str, seq: int, value: int,
                        cseq: int | None = None) -> bytes:
    """The vendor's answer to the camera's status queries (cmd 0x0004).

    Captured live from sig.gps555.net on 2026-10-23: the answer is the
    command with the response bit set (`0x8001`) and a five-byte body
    that is an ack of the camera's request:

        cSeq:2 | cmd:2 | ack:1

    `cSeq` is the camera's own sequence number (see
    `vendor_pullvideo_answer`), `cmd` is the request's id -- 4 for the
    status query -- and `ack` is 0. The capture's `00 04 00 04 00` is
    the camera's `0x0004` with seq 4 being answered.
    """
    if cseq is None:
        cseq = seq
    body = struct.pack("!HH", cseq, value) + b"\x00"
    return signalling_frame(0x8001, seq, uid.encode(), body, pad=False)


def vendor_relay_trigger(uid: str, seq: int) -> bytes:
    """The `cmd 0x8005` frame that makes the camera publish.

    Recovered by disassembling the camera's firmware (DECOMPILATION_REPORT
    section 16). The dispatcher `CUSTOM_TASK_CmdProHandle` routes
    `0x8005` to a handler that logs `CUSTOM_TASK_RelayPro` and
    `rtsp_p2p_start`, sets `dev->0xc8 = 2` and starts the relay task --
    and **reads nothing from the body**. It is a pure trigger, and it is
    what makes the camera connect to the relay address the `0x8000`
    answer armed.

    In the vendor's flow the app's `notify-live-event?eventType=5`
    (`CameraEventType.relay`) makes the cloud send exactly this frame. So
    the whole media trigger over the signalling connection is two
    frames: the `0x8000` that arms the target, and this one that starts
    the push.

    The body is empty and the length counts the uid alone, exactly like
    the other frames.
    """
    return signalling_frame(0x8005, seq, uid.encode(), b"", pad=False)


def vendor_p2p_conn_req(uid: str, seq: int, pub_ip: str, pub_port: int,
                        priv_ip: str, priv_port: int,
                        seq_no: int = 0) -> bytes:
    """The server's cmd 0x8101 -- deliver the viewer's address pairs.

    Firmware: CUSTOM_TASK_P2PConnReq, handler 0x1801ba22
    (SIGNALLING_PROTOCOL.md §13.1). Body is 64 bytes: 20 the handler
    never reads, then pair A (IP, 20 B ASCII NUL-padded + u16 BE port)
    and pair B, laid out the same way. The camera picks pair B when
    pair A equals its own STUN-learned public IP and pair B shares its
    local IP's /24 (the app's DeviceStunItem subnet rule, from the
    camera's side), else pair A; it stores the pick at dev+0xd1 and
    starts its P2P thread, bound to the RTP port, waiting for the
    viewer's first datagram.
    The 20 bytes the handler never reads carry the registration number
    (§14.1): the camera echoes it back in its `0x0003`, which is how
    the cloud knows the camera has answered *this* registration.
    """
    body = (struct.pack("!H", seq_no) + b"\x00" * 18
            + pub_ip.encode().ljust(20, b"\x00") + struct.pack("!H", pub_port)
            + priv_ip.encode().ljust(20, b"\x00") + struct.pack("!H", priv_port))
    return signalling_frame(0x8101, seq, uid.encode(), body, pad=False)


def vendor_play_ctrl(uid: str, seq: int, value: int) -> bytes:
    """The server's cmd 0x8004 -- app play control (§13.3).

    Body is 22 bytes: 20 filler the handler only hex-dumps, plus the
    u16 BE command it reads at body offset 20: 1 = PlayLiveview
    (valid only once the camera's P2P thread is bound, flag 1), 2 =
    PlayPause, 3 = stop.
    """
    body = b"\x00" * 20 + struct.pack("!H", value)
    return signalling_frame(0x8004, seq, uid.encode(), body, pad=False)


def vendor_heartbeat(uid: str, seq: int) -> bytes:
    """The server's cmd 0x8002 -- the live-view heartbeat (§13.4).

    Stamps the camera's liveness tick (`dev->0x34`), the only thing that
    keeps a playing live view alive once the P2P thread has exited: the
    camera stops the view 36 s after the last stamp ("live heartbeat
    timeout, stop live"). The firmware's handler reads nothing from the
    body, so the frame is the bare header + uid, exactly like the
    camera's own ping. The vendor's cloud sends one for each app
    `notify-live-event?eventType=0` (inferred, §13.4 -- the 2 h wiretap
    held no 0x8002 because no viewer was attached), so the broker sends
    one per notify(keepAlive) the bridge makes -- every 2 s, comfortably
    under the camera's 36 s.
    """
    return signalling_frame(0x8002, seq, uid.encode(), b"", pad=False)


def vendor_config_answer(uid: str, seq: int) -> bytes:
    """The vendor's answer to the camera's cmd 0x1601.

    Captured live from sig.gps555.net on 2026-10-23: the answer is the
    command with the response bit set (`0x8601`) and a 43-byte body --
    37 NUL bytes followed by three big-endian u16s:

        offset 37..38  27 10 = 10000   the relay port the vendor uses
        offset 40..41  00 0f = 15      ?
        offset 42..43  00 0f = 15      ?

    (The byte that looked like a varying timestamp after those three is
    the frame's own XOR check, which changes with the sequence number.)

    The broker used to send 35 NUL bytes instead of 37, so the two
    ports sat two bytes earlier than the camera expects. The layout
    above is what the vendor actually sends, reproduced byte for byte.
    """
    body = b"\x00" * 37 + struct.pack("!HHH", 10000, 15, 15)
    return signalling_frame(0x8601, seq, uid.encode(), body, pad=False)


def parse_status_report(body: bytes) -> dict:
    """The camera's status report (cmd 0x0004) as {id: value}.

    §13.7: the body is a count byte, then that many 4-byte entries
    `00 id 00 value`. A single report reads `01 00 03 00 VV`; a full
    one (argument 8) carries ids 1-7 and 9. Wire id 3 is the media
    state:

    | value | meaning |
    |---|---|
    | 0 | stopped |
    | 1 | direct live |
    | 2 | relay live |
    | **3** | **P2P socket bound, waiting for the viewer** |
    | 4 | the other StopPlay branch |
    """
    if len(body) < 1:
        return {}
    count = body[0]
    out = {}
    for i in range(count):
        off = 1 + 4 * i
        if off + 4 > len(body):
            break
        ident, value = body[off + 1], body[off + 3]
        out[ident] = value
    return out


def parse_local_addr_report(body: bytes) -> dict:
    """The camera's own address report (cmd 0x0003), §12.1.

    The body is 47 bytes:

    | offset | size | field |
    |---|---|---|
    | 0 | 20 | public IP, ASCII, NUL-padded (the STUN result) |
    | 20 | 2 | public port, u16 BE |
    | 22 | 20 | local IP, ASCII |
    | 42 | 2 | local port = the RTP port, u16 BE |
    | 44 | 2 | the registration number the camera is answering |
    | 46 | 1 | a caller-supplied byte |

    Returns the fields it can read; missing ones are absent.
    """
    if len(body) < 44:
        return {}
    out = {
        "public_ip": body[:20].rstrip(b"\x00").decode("utf-8", "replace"),
        "local_ip": body[22:42].rstrip(b"\x00").decode("utf-8", "replace"),
    }
    out["public_port"], = struct.unpack("!H", body[20:22])
    out["local_port"], = struct.unpack("!H", body[42:44])
    if len(body) >= 46:
        out["seq_no"], = struct.unpack("!H", body[44:46])
    return out


def decode_signalling(data: bytes) -> dict:
    """Decode a camera->server signalling frame.

    Read off the wire (2026-10-23, cameras 140979857781 and
    141030191094): 56 bytes --

        86 86 | cmd(2) | len(2) | seq(2) | uidLen(2) | uid(12) | body(32) | xor | 0x0D
              0001     002c     0019..    000c        ...        ...

    `len` (0x002c = 44) is the uid plus the body; `seq` is the camera's
    counter and increments on every retransmission -- the camera
    retransmits its registration about once a second until it is
    answered, and the body never changes: it is the firmware version
    `TXW817_A_V1.0.12.32`, NUL-padded to 32 bytes.
    """
    out = {"bytes": len(data)}
    if len(data) < 10 or data[:2] != b"\x86\x86":
        out["hex"] = data[:64].hex()
        return out
    cmd, length, seq, uid_len = struct.unpack("!HHHH", data[2:10])
    out.update({"cmd": f"0x{cmd:04X}", "len": length, "seq": seq,
                "uid_len": uid_len})
    uid = data[10:10 + uid_len]
    out["uid"] = uid.decode("utf-8", "replace")
    body = data[10 + uid_len:]
    if body and body[-1] == 0x0D:
        body = body[:-1]
    if body:
        out["xor_ok"] = _xor(data[:len(data) - 2]) == data[-2]
    out["body"] = body.decode("utf-8", "replace").rstrip("\x00")
    return out


def split_signalling(data: bytes) -> tuple[list[bytes], bytes]:
    """Cut a sig stream chunk into complete frames, plus the tail.

    The sig socket is a TCP stream, not a datagram socket: the camera
    coalesces back-to-back frames into one read -- fifteen queued
    0x0002 pings and a 0x0004 arrived as one 389-byte chunk (2026-10-24
    19:40:11) -- and a frame can straddle two reads. Decoding such a
    chunk as one frame failed the XOR check and was answered once, so
    fourteen pings went unanswered and the camera went silent (the
    documented silent-probe limbo). Every complete frame has to be cut
    out and answered on its own.

    The wire shape is signalling_frame's: magic 86 86, the header (cmd,
    len, seq, uidLen), then `len` bytes -- the uid plus the body -- then
    the XOR byte and the 0x0D end symbol. `len` counts everything
    between the header and the XOR.

    Returns (frames, tail): the complete frames in order, and whatever
    cannot be decoded yet. A frame that straddles the read boundary
    waits in the tail for the next read; garbage before, between or
    after frames is dropped, a lone 0x86 at the end is kept because the
    next read may complete it into a magic, and a length field that
    would build a frame beyond SIG_FRAME_MAX is treated as a corrupt
    header rather than a frame that will never close. A candidate frame
    must also carry the end symbol and a correct XOR, so a magic-shaped
    accident in the garbage is never emitted -- the scanner then tries
    the next magic instead, which is how a stray 0x86 sitting right
    before a real one is survived.
    """
    frames: list[bytes] = []
    pos = 0
    n = len(data)
    while True:
        # The first magic from pos that parses to a complete, valid
        # frame. A false magic (garbage that happens to start 86 86, or
        # a stray 0x86 ahead of a real one) can only fail three ways --
        # header short, frame overlong or incomplete, body not
        # XOR-checked -- and every failure moves on to the next magic
        # rather than consuming.
        scan = data.find(b"\x86\x86", pos)
        end = 0
        while scan >= 0:
            if n - scan < 10:
                break   # not even a header: it may complete later
            _cmd, length, _seq, _uid_len = struct.unpack(
                "!HHHH", data[scan + 2:scan + 10])
            end = scan + 10 + length + 2
            if 12 + length > SIG_FRAME_MAX or end > n:
                scan = data.find(b"\x86\x86", scan + 1)
                continue
            frame = data[scan:end]
            if frame[-1] != 0x0D or _xor(frame[:-2]) != frame[-2]:
                scan = data.find(b"\x86\x86", scan + 1)
                continue
            break
        if scan < 0:
            if pos >= n:
                return frames, b""
            last = data.rfind(b"\x86\x86", pos)
            if last < 0:
                # No whole magic anywhere: a lone 0x86 at the very end
                # may be the first byte of a magic the boundary split.
                return frames, (data[-1:] if data[-1:] == b"\x86" else b"")
            # Wait from the last magic: everything before it is already
            # judged, and what follows may be a frame's first half.
            return frames, data[last:]
        if n - scan < 10:
            return frames, data[scan:]
        frames.append(data[scan:end])
        pos = end
        if pos >= n:
            return frames, b""


def arm_target(viewer: dict, relay_ip: str, relay_port: int,
               own_ip: str) -> tuple[str, int]:
    """The (ip, port) the camera should be armed to send its media to.

    The camera pushes plain UDP RTP to the address its `0x8000`
    registration answer carries in the relay slot
    (`ziot-fw/SIGNALLING_PROTOCOL.md` §5-§6): after the start signal it
    opens a UDP RTP socket and `sendto`s JPEG (RTP/AVP 26) + PCMU
    (RTP/AVP 0) there. The receiver must therefore be a UDP RTP sink.

    Best-known first:

    * the **viewer** registered through send-stun-addr -- for the
      bridge that IS its UDP media socket, so the media lands in the
      very socket the bridge reads (§9.3: the bridge's UDP media
      address in the relay slot, skipping an RTSP relay for ingest);
    * the configured RTSP relay port (`--relay-port`) -- kept only as
      an explicit experiment target, since the camera never speaks
      RTSP on this leg (no ANNOUNCE/RECORD exists in the firmware);
    * this host, with port 0, so the register still gets answered when
      nothing is known yet (an unanswered register retransmits
      forever).
    """
    ip = str(viewer.get("privateIp") or "").strip()
    try:
        port = int(viewer.get("privatePort") or 0)
    except (TypeError, ValueError):
        port = 0
    if not ip or not port:
        ip = ip or str(viewer.get("publicIp") or "").strip()
        try:
            port = int(viewer.get("publicPort") or 0)
        except (TypeError, ValueError):
            port = 0
    if not port:
        # No destination registered or configured: arm this host, so the
        # camera's session settles and a later re-registration re-arms
        # with a real target once one is known.
        ip = ip or relay_ip
        port = port or relay_port
    return (ip or own_ip), max(0, port)


class StreamHandler(socketserver.BaseRequestHandler):
    """TCP: log each PDU and answer the vendor's HTTP API.

    The API answers are ground truth taken from a live app session against
    ipc.gps555.net (captured 2026-08-29, see DECOMPILATION_REPORT.md
    section 11): every response is {"requestId": ..., "code": 200, "msg":
    "OK", "data": ...}, and the endpoint set is exactly the six the
    bridge touches plus the device detail and package record.

    On the signalling port (8882) the peer is the camera, and its
    protocol is the one recovered from the stock firmware
    (`ziot-fw/SIGNALLING_PROTOCOL.md`): the camera registers with
    `0x0001`, the broker answers with the vendor's `0x8000` (which arms
    where the media goes), the camera's handshake (`0x0021` clock,
    `0x0002` pullVideo, `0x0004` status, `0x1601` config) is answered
    with the vendor's captured answers, and the unsolicited `0x8005`
    starts the media push to the armed target. Everything else is only
    logged.
    """

    def _vendor_answer(self, uid: str, seq: int, peer_ip: str,
                       api) -> tuple[bytes, tuple[str, int]]:
        """The registration answer: the vendor's `0x8000`, pointed here.

        The vendor's answer tells the camera three things: where to send
        its media (the relay slot), which STUN server to ask for its own
        address, and where the signalling server is. The relay slot now
        carries the target chosen by arm_target() -- and the STUN host
        stays the vendor's own name, which the broker's DNS + STUN server
        answer. The camera copies the relay ip + port and the STUN host +
        port out of the frame at fixed offsets and sets its session state
        to 3: that is the ARM -- it does not connect yet (`0x8000` alone
        never made any camera publish; the vendor's own capture proves
        it). Once the `0x8005` start signal arrives (§6) the camera opens
        a UDP RTP socket and sendto's JPEG (RTP/AVP 26) + PCMU (RTP/AVP
        0) to the armed ip:port, the same media its own RTSP path serves
        -- so the port below belongs to a **UDP RTP sink**, not to an
        RTSP server waiting for an ANNOUNCE the camera will never send.

        Returns the frame and the (ip, port) it armed, so the caller
        knows whether there is anywhere worth starting the push to.
        """
        own = self.server.own_ip or peer_ip
        viewer = api.viewer_for(uid) if api else {}
        relay_ip, relay_port = arm_target(
            viewer, self.server.relay_ip, self.server.relay_port, own)
        # The vendor's frame carries two ports for the relay (554 and
        # 10000 in the capture); the camera's dispatcher reads the
        # SECOND one (offset 0x18) as its send target. Both carry the
        # sink's port here, so the armed target is the same whichever
        # slot is read (the capture's both slots rebuild byte for byte).
        frame = vendor_registration_answer(
            uid, seq, relay_ip, relay_port, relay_port,
            "stun.gps555.net", 13478, own, 8882)
        return frame, (relay_ip, relay_port)

    def _answer_camera(self, peer, decoded, raw: bytes = b"") -> None:
        """Answer a camera's signalling frame.

        The camera registers with cmd 0x0001 (uid + firmware version) and
        keeps sending until it is answered. The answer is the vendor's
        own: the frame captured live from sig.gps555.net, with every
        address in it pointed at this host, so the camera registers
        here, calls our STUN server and sends its media to us.
        """
        uid = decoded.get("uid", "")
        if not uid or "cmd" not in decoded:
            return
        seq = decoded.get("seq", 0)
        cmd = decoded.get("cmd", "")
        api = self.server.api
        if api is not None:
            if uid not in api.cameras:
                # The camera just told us it exists and where it is: that
                # is what the vendor's cloud learns from the same
                # message.
                api.add_camera(uid, peer[0])
                api.save_cameras()
                log("register", peer, f"uid={uid}".encode())
            # Remember the connection: the broker pushes to the camera
            # over it (the "start live" instruction).
            api.camera_links[uid] = self.request
            # The sequence number of the request, so an answer can echo
            # it (the vendor's does).
            api.last_seq[uid] = seq
            # The camera's session is short: it needs a message every
            # couple of seconds or it drops (and re-registers about
            # forty seconds later). The vendor keeps it alive with the
            # go-ahead; the camera's own traffic is the natural clock.
            # A camera that just asked to pull its video is answered
            # below, so it does not also get the periodic push.
            if cmd != "0x0002" and api.keepalive_due(uid):
                api.keepalive(uid)
        # The (ip, port) a registration answer armed -- ("", 0) for every
        # other command, since only the arm can start a push.
        armed = ("", 0)
        # Any command the dispatch below does not know leaves reply None
        # and falls through to the candidate sweep.
        reply = None
        if cmd == "0x0001":
            # The registration answer IS the ack of the register: the
            # vendor's `0x8000` carrying the media target. It goes out
            # even without a real target (port 0), because a camera
            # whose register is left unanswered retransmits it forever
            # instead of settling into a session.
            reply, armed = self._vendor_answer(uid, seq, peer[0], api)
        elif cmd == "0x0021":
            # The camera asks for the server's clock; the vendor answers
            # with a u32 timestamp. This is what keeps the session alive.
            reply = vendor_time_answer(uid, seq)
        elif cmd == "0x0003":
            # The camera's own address report (CUSTOM_TASK_ReportLocalAddr,
            # SIGNALLING_PROTOCOL.md §12.1): the STUN public pair at body
            # 0/20 and the local (RTP) pair at 22/42. The vendor's
            # stun-addr endpoint serves from exactly this. Its arrival
            # also proves the camera's STUN succeeded -- the missing
            # reply attribute (§12.3) is what used to end every session
            # at ~6 s.
            if raw:
                uid_len = decoded.get("uid_len", 12)
                body3 = raw[10 + uid_len:-2]
                report = parse_local_addr_report(body3)
                if report:
                    pub_ip = report["public_ip"]
                    pub_port = report["public_port"]
                    loc_ip = report["local_ip"]
                    loc_port = report["local_port"]
                    log("stun-report", peer,
                        f"uid={uid} public={pub_ip}:{pub_port} "
                        f"local={loc_ip}:{loc_port}".encode())
                    if api is not None:
                        # The camera's local pair is its RTP port -- the
                        # one the P2P socket binds (§13.2), and the one
                        # the viewer's hello has to reach.
                        api.set_camera_address(uid, loc_ip, loc_port)
                        # Every distinct port is kept: the camera may
                        # answer the 0x8101 from one and redraw (§18.6).
                        api.note_p2p_port(uid, loc_port)
                        # Its *public* pair is what the vendor's
                        # stun-addr serves as IpcPublic* (§14.1: the
                        # public pair is the camera's own STUN mapping,
                        # not the viewer's). It was parsed and thrown
                        # away until now.
                        api.set_camera_public(uid, pub_ip, pub_port)
                        # The registration number this report answers
                        # (§14.1): the camera echoes the N the cloud put
                        # in the 0x8101, so the record can show the
                        # viewer's own number once the camera has
                        # reported.
                        if report.get("seq_no"):
                            api.set_reported_seq(uid, report["seq_no"])
                        # Only the report that echoes the 0x8101's seq
                        # names the P2P socket; a redraw must not move
                        # what stun-addr serves (§18.8).
                        api.note_p2p_answer(uid, report.get("seq_no", 0),
                                            pub_ip, pub_port,
                                            loc_ip, loc_port)
            return
        elif cmd == "0x0002":
            # pullVideo: the camera asking to send its media. The
            # vendor's "go ahead" is what starts the session.
            reply = vendor_pullvideo_answer(uid, seq)
        elif cmd == "0x0004":
            # A status query; the answer echoes the queried value.
            reply = vendor_status_answer(uid, seq, 4)
            if raw:
                # The camera's own status report (§13.7): a count byte,
                # then 4-byte entries `00 id 00 value`. Wire id 3 is the
                # media state, and **3 is the one that matters**: "P2P
                # socket bound, waiting for the viewer" -- which is
                # exactly the precondition PlayLiveview checks
                # (§13.3: it acts only when peerToPeerFlag == 1, else
                # "p2p is not finish"). So the camera tells us when it
                # is ready; the play must wait for that, not for a
                # timer.
                uid_len = decoded.get("uid_len", 12)
                body4 = raw[10 + uid_len:-2]
                state = parse_status_report(body4)
                if state.get(3) is not None and api is not None:
                    api.set_media_state(uid, state[3])
                    if state[3] == 3:
                        api.p2p_socket_bound(uid)
        elif cmd == "0x1601":
            # the camera asking for its configuration: the ports to use.
            reply = vendor_config_answer(uid, seq)
        elif cmd == "0x0020":
            # pushVideo: the camera asking the server where to push its
            # video. The app's map for this message has `cmd` and `url`.
            reply = vendor_pushvideo_answer(uid, seq,
                                            api.push_url(uid) if api else "")
        # Any other command id is still unknown: reply stays None and
        # the candidate sweep below answers it, in turn.
        if reply is not None:
            log("sig-ack", peer, f"vendor-answer {reply.hex()}".encode())
            if api is not None:
                api.send_to(uid, reply)
            else:
                try:
                    self.request.sendall(reply)
                except OSError:
                    pass
            # The `0x8000` answer only ARMS the media target; the
            # unsolicited `0x8005` is what starts the relay push
            # (SIGNALLING_PROTOCOL.md §6). Firing it inside the
            # handshake was tried live and broke it (reconnect-loop, no
            # media), so it is OFF unless --relay-start opts in -- and
            # even then it waits RELAY_START_DELAY_S, lands once, and
            # only on a camera that is still connected.
            #
            # The *direct* chain (0x8101 then 0x8004) is a different
            # thing and is not the relay: it is what the app's own
            # live view does, and it is the only chain that has ever
            # produced frames (2026-10-24 16:14). It must not be
            # gated behind the relay flag -- doing so silently
            # disarmed the direct path and left the cameras on the
            # network, registering, and sending no media at all.
            if cmd == "0x0001" and armed[1] and api is not None:
                api.schedule_p2p_live(uid, force=True)
                # The relay trigger rides the arm as well: the camera
                # sets its relay-start flag when the 0x8005 arrives, and
                # the armed address is where it would push. It is a
                # no-op unless --relay-start asks for it.
                api.schedule_relay_trigger(uid)
            return
        # A command we have no vendor answer for. The protocol is known
        # now (§4: 0x8001 is the ack, body `cSeq/cmd/ack`), so the
        # answer is the vendor's own shape with the camera's sequence
        # number and its command id. The old candidate sweep sent the
        # camera frames the vendor never sends -- including a `0x8001`
        # with **no body at all**, which the camera's `cSeq/cmd/ack`
        # parser cannot read. The sweep is kept behind `ack_sweep` for
        # experiments; it is off by default.
        if not self.server.ack_sweep:
            cmd_id = int(decoded.get("cmd", "0x0"), 16)
            reply = signalling_frame(0x8001, seq, uid.encode(),
                                     b"\x00" + struct.pack("!HH", seq,
                                                           cmd_id) + b"\x00",
                                     pad=False)
            log("sig-ack", peer, f"vendor-ack {reply.hex()}".encode())
            api = self.server.api
            if api is not None:
                api.send_to(uid, reply)
            else:
                try:
                    self.request.sendall(reply)
                except OSError:
                    pass
            return
        candidates = _ack_candidates(uid.encode(), seq,
                                     decoded.get("len", 0),
                                     api.viewer_for(uid) if api else None)
        if raw:
            # A plain echo: the same frame back. The camera's `len`
            # field says how long the frame is, so the answer has to be
            # the same length -- and the simplest way to be that is to
            # send back what came in.
            candidates.insert(0, ("echo", raw))
        state = self.server.ack_index.get(uid, {"index": 0, "count": 0,
                                                 "name": "", "since": 0.0})
        # Hold each candidate for a while: the camera retransmits about
        # once a second, and its reaction to a given answer (stops
        # retransmitting, sends another command) is the only signal that
        # the answer was understood.
        now = time.monotonic()
        if now - state["since"] > ACK_HOLD_S:
            state["index"] = (state["index"] + 1) % len(candidates)
            state["since"] = now
            state["count"] = 0
        name, reply = candidates[state["index"] % len(candidates)]
        state["count"] += 1
        state["name"] = name
        self.server.ack_index[uid] = state
        # Every camera frame is tagged with the candidate that was in
        # force when it arrived, so a reaction is attributable to one
        # answer rather than to a whole rotation.
        log("sig-ack", peer, f"{name} {reply.hex()}".encode())
        log("sig-tag", peer, f"{name} seq={decoded.get('seq')} "
                             f"cmd={decoded.get('cmd')}".encode())
        api = self.server.api
        if api is not None:
            api.send_to(uid, reply)
        else:
            try:
                self.request.sendall(reply)
            except OSError:
                pass

    def handle(self):
        peer = self.request.getpeername()
        buf = b""
        # The sig stream's undecoded tail: a frame that straddles two
        # reads waits here until its second half arrives (the camera's
        # TCP stream coalesces and splits frames freely).
        sig_buf = b""
        # Per-kind idle timeout. A camera connection goes quiet for
        # 40-90 s while it waits for the server push (measured live,
        # both cameras), so it is held open for just over that: a
        # connection that closes before the push can land is the bug
        # that made every push write into a socket already handed back
        # -- the log's 22 `Bad file descriptor` failures. The HTTP
        # ports keep a short one: a client that idles there reconnects
        # freely, and every idle connection holds a thread.
        self.request.settimeout(STREAM_IDLE_S if self.server.kind == "sig"
                                else 10.0)
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
            if self.server.kind == "http":
                # The whole request head: the app's token header is
                # longer than the log's ASCII column shows, and it is
                # the one thing the broker cannot invent.
                head_end = data.find(b"\r\n\r\n")
                if 0 < head_end < 4096:
                    for line in data[:head_end].split(b"\r\n")[1:]:
                        name, _, value = line.partition(b":")
                        if name.strip().lower() in (b"token", b"authorization"):
                            log("auth", peer, value.strip())
            if self.server.kind == "sig" and not _looks_http(data):
                # The stream carries whole frames back to back, and a
                # frame can straddle two reads: cut the complete frames
                # out and answer each on its own. Decoding a coalesced
                # chunk as one frame once answered fifteen queued pings
                # with a single bogus ack (xor_ok false) and the camera
                # went silent from then on (2026-10-24 19:40:11).
                sig_buf += data
                frames, sig_buf = split_signalling(sig_buf)
                for frame in frames:
                    decoded = decode_signalling(frame)
                    if len(decoded) > 1:
                        log(self.server.kind, peer,
                            ("decoded " + json.dumps(decoded)).encode())
                        self._answer_camera(peer, decoded, frame)
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
                # The account's token: the app sends it in a header, and
                # it is the one thing the broker cannot invent. It is
                # logged so a real cloud can be reached with it.
                token = ""
                for line in head.split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.strip().lower() in (b"token", b"authorization"):
                        value = value.strip()
                        if value.lower().startswith(b"bearer "):
                            value = value[7:].strip()
                        token = value.decode("latin-1", "replace")
                        log("auth", peer, value)
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
                    # Signalling and websocket ports: the peer is the camera
                    # and its protocol is not HTTP. Answer only what is
                    # plainly an HTTP request (our own health checks, and
                    # the browser test that proves a router route works),
                    # and leave everything else untouched.
                    if _looks_http(head):
                        self.request.sendall(
                            _http_response("200 OK",
                                           json.dumps({"code": 200,
                                                       "msg": "OK",
                                                       "data": {
                                                               "port": "signalling",
                                                               "kind": self.server.kind,
                                                           }}).encode()))
                    return
                payload = self.server.api.respond(method, path, query, body,
                                                 token)
                if payload is None:
                    return
                self.request.sendall(payload)
                if b"connection: close" in head.lower():
                    return

    def finish(self):
        """Drop the camera links bound to this socket.

        finish() is part of the handler lifecycle (setup / try: handle
        / finally: finish), so every way this connection ends -- idle
        timeout, camera reset, mid-frame nonsense, a clean close --
        comes through here. Only links whose socket IS this one are
        dropped: a replacement connection from the same camera, or a
        new tenant of this socket, must survive this close. Leaving
        dead links registered is what made 22 pushes end in `Bad file
        descriptor` in one morning.
        """
        api = getattr(self.server, "api", None)
        if api is not None:
            for uid, conn in list(api.camera_links.items()):
                if conn is self.request:
                    api.camera_links.pop(uid, None)
                    # A dead sig link also ends any live view (the 36 s
                    # heartbeat check only runs while the sig link is
                    # up, §13.4, and the camera itself reports 0 on
                    # stop) -- but the camera can no longer tell us, so
                    # the row would keep the last `1`/`3` forever. The
                    # app refuses to open a camera whose `mediaState` is
                    # not "0" (`设备繁忙`, SIGNALLING_PROTOCOL.md
                    # §17.1), so a stale busy row locks the app out.
                    # Reset it to idle, which is what a gone camera is.
                    api.set_media_state(uid, 0)
        super().finish()


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

    def __init__(self, viewer_file: str = "", camera_file: str = "",
                 relay_host: str = "", relay_port: int = 0,
                 relay_start: bool = False,
                 sig_host: str = "", sig_port: int = 8882):
        self.cameras: dict[str, dict] = {}
        self.camera_file = camera_file
        # Where each camera's media should be delivered: the viewer that
        # registered through send-stun-addr.
        self.viewers: dict[str, dict] = {}
        self.viewer_file = viewer_file
        # A local RTSP relay's address, for sink experiments and the
        # device list's `relay_ip`. The camera never speaks RTSP back:
        # its media leg is plain UDP RTP to the armed address
        # (SIGNALLING_PROTOCOL.md §6), so the registration answer arms
        # the REGISTERED VIEWER (the bridge's UDP media socket) as
        # arm_target() decides, and this slot only fills when no viewer
        # has registered.
        self.relay_host = relay_host
        self.relay_port = relay_port
        # The `0x8005` media trigger, OFF by default: firing it
        # unsolicited or on the keepalive beat broke the camera's
        # handshake live (2026-10-24). --relay-start opts into it, and
        # even then the fire waits RELAY_START_DELAY_S and lands at most
        # once: one pending fire per camera, in this dict (uid ->
        # monotonic due time), sent by the keepalive worker's clock.
        self.relay_start = relay_start
        self.relay_trigger_at: dict[str, float] = {}
        # The signalling connection of each camera, so the broker can push
        # a message to it (the "start live" instruction) when a viewer
        # asks for the video.
        self.camera_links: dict[str, object] = {}
        # Cameras whose live heartbeat (0x8002) could not be sent on the
        # last beat: a drop is logged once per outage, not once per beat,
        # because the bridge keeps sending every 2 s whether or not the
        # camera is listening.
        self.heartbeat_dropped: set[str] = set()
        # The last sequence number seen from each camera: an answer
        # echoes the sequence of the request it answers.
        self.last_seq: dict[str, int] = {}
        # When each camera was last kept alive.
        self.last_keepalive: dict[str, float] = {}
        # The direct live-view chain (§13): the 0x8004 PlayLiveview fire
        # scheduled after each camera's 0x8101, once per camera at a
        # time.
        self.p2p_fire_at: dict[str, float] = {}
        # The cameras that have reported their P2P socket bound (their
        # own media state 3, §13.7): the play for these is triggered by
        # the camera, not by the fallback timer.
        self.p2p_ready: dict[str, float] = {}
        # The RTP ports the camera reported since the last 0x8101, in
        # order: the hello has to reach each of them (§18.6).
        self.p2p_ports: dict[str, list[int]] = {}
        # The viewer pair carried by each camera's last 0x8101, so a
        # pending chain is only reused while the pair is unchanged.
        self.p2p_sent_pair: dict[str, tuple] = {}
        # The HEADER seq of each camera's last 0x8101: the 0x0003 that
        # answers it echoes this number as its seq_no (§14.1, §18.8).
        self.p2p_sent_seq: dict[str, int] = {}
        # (public_ip, public_port, local_ip, local_port) of the 0x0003
        # that answered the last 0x8101 -- the camera's P2P socket,
        # which `stun-addr` keeps serving through a later redraw.
        self.p2p_answer: dict[str, tuple] = {}
        # Monotonic time of each camera's last 0x8101, for the resend
        # floor (P2P_RESEND_MIN_S).
        self.p2p_sent_at: dict[str, float] = {}
        # One writer at a time per camera socket: the frames must not
        # interleave. Every send to a camera goes through send_to().
        self.push_lock = threading.Lock()
        # The keepalive worker's thread and its stop event.
        self._keepalive_thread = None
        self._keepalive_stop = threading.Event()
        self.seq = 0
        # The registration number per camera (§14.1). Each
        # `send-stun-addr` takes the next N for that camera and answers
        # with it; the camera echoes it back in its `0x0003`, and only
        # then does `stun-addr` show it. One shared counter -- which is
        # what this was -- never shows the viewer's own number, and the
        # app waits for exactly that.
        self.seq_no: dict[str, int] = {}
        # The N the camera has reported answering.
        self.reported_seq: dict[str, int] = {}
        # The N handed to the camera in the last `0x8101`, waiting for
        # the camera to echo it back in its `0x0003`.
        self.pending_seq: dict[str, int] = {}
        # This broker's own signalling address, as `get-sig-ip` serves it
        # (§17.5): the app saves it as `icpSigAddress` and provisions new
        # cameras with it over BLE (the wifiConnect frame's `urlAndPort`).
        self.sig_host = sig_host
        self.sig_port = sig_port

    def sig_address(self) -> str:
        """`host:port` of this broker's signalling server, or "" if the
        sig plane is off."""
        if not self.sig_port or not self.sig_host:
            return ""
        return f"{self.sig_host}:{self.sig_port}"

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
            # The vendor's record carries this too (the capture shows
            # showAd: true), and it sits here in the vendor's order.
            "showAd": True,
            "unReadEventCount": 0,
            # What the broker hands back in stun-addr.
            "_ip": ip,
            "_port": port,
        }
        return self.cameras[uid]

    def push_url(self, uid: str) -> str:
        """The URL the camera should push its video to.

        The app's `CameraInfoModel.relayPath` builds one of two forms
        (see the bridge's `relay_urls()`): `rtsp://<host>/live/<uid>` for
        an X5, `rtsp://<host>/rtp/<last 8 of uid>` otherwise. The host is
        the viewer -- the app or the bridge -- so the camera's video
        lands where it was asked for.

        With the local RTSP relay running, the app-facing relayPath points
        THERE -- the relay is a transport experiment, not the camera's
        own leg: the camera sends plain UDP RTP to the armed address
        (SIGNALLING_PROTOCOL.md §6) and never pulls or publishes RTSP.
        """
        relay = self.relay_address()
        if relay:
            host = f"{relay['host']}:{relay['port']}"
            if len(uid) > 8:
                return f"rtsp://{host}/rtp/{uid[-8:]}"
            return f"rtsp://{host}/live/{uid}"
        viewer = self.viewers.get(uid) or {}
        host = str(viewer.get("privateIp") or viewer.get("publicIp") or "")
        if not host:
            return ""
        port = viewer.get("privatePort") or viewer.get("publicPort") or 0
        if port:
            host = f"{host}:{port}"
        if len(uid) > 8:
            return f"rtsp://{host}/rtp/{uid[-8:]}"
        return f"rtsp://{host}/live/{uid}"

    def send_to(self, uid: str, frame: bytes) -> bool:
        """Send one frame to a camera's signalling connection.

        Every write to that socket goes through here, so a pushed frame
        and a normal answer can never interleave.
        """
        conn = self.camera_links.get(uid)
        if conn is None:
            return False
        with self.push_lock:
            try:
                conn.sendall(frame)
            except OSError as e:
                log("push", ("-", 0), f"uid={uid} send failed: {e}".encode())
                return False
        return True

    def start_keepalive_worker(self, interval: float = KEEPALIVE_INTERVAL):
        """Start the thread that keeps every camera's session alive.

        The camera's own traffic is not a reliable clock: it goes quiet
        when it has nothing to say, and a session that is not fed dies
        in about twelve seconds. So the keepalive has its own timer, and
        a camera is fed whether or not it is talking.
        """
        if self._keepalive_thread is not None:
            return self._keepalive_thread
        self._keepalive_stop.clear()

        def loop():
            while not self._keepalive_stop.wait(interval):
                now = time.monotonic()
                for uid in list(self.camera_links):
                    # A camera seen for the first time is armed now, so
                    # it is fed one interval from now, not immediately.
                    last = self.last_keepalive.setdefault(uid, now)
                    if now - last < interval:
                        continue
                    if self.keepalive(uid):
                        # Only a successful send counts as fed: a broken
                        # socket must not push the next attempt out.
                        self.last_keepalive[uid] = now
                # The delayed media trigger rides the same clock: one
                # sweep per tick, both entering and leaving the loop.
                self.serve_due_relay_triggers(now)
                self.serve_due_p2p_fires(now)

        self._keepalive_thread = threading.Thread(target=loop, daemon=True,
                                                  name="ziot-keepalive")
        self._keepalive_thread.start()
        return self._keepalive_thread

    def stop_keepalive_worker(self, timeout: float = 2.0):
        """Stop the keepalive thread and wait for it to finish."""
        self._keepalive_stop.set()
        thread = self._keepalive_thread
        self._keepalive_thread = None
        if thread is not None:
            thread.join(timeout)

    def send_heartbeat(self, uid: str) -> None:
        """Turn one bridge keepAlive into the camera's 0x8002 (§13.6).

        Every eventType=0 the bridge sends every 2 s must reach the
        camera as the heartbeat frame: it is the only thing that stamps
        the liveness tick during play, and without it the camera stops
        every live view 36 s after its start (SIGNALLING_PROTOCOL.md
        §15 item 6). A send whose socket is gone is logged once per
        outage, not once per beat: the bridge keeps sending every 2 s
        whether or not the camera is listening, and the log must not
        drown in the absence.
        """
        if self.camera_links.get(uid) is None:
            if uid not in self.heartbeat_dropped:
                self.heartbeat_dropped.add(uid)
                log("push", ("-", 0),
                    f"uid={uid} 0x8002 heartbeat: camera not connected"
                    .encode())
            return
        self.heartbeat_dropped.discard(uid)
        # Every frame goes through send_to, so a heartbeat and a normal
        # answer can never interleave on the socket.
        self.send_to(uid, vendor_heartbeat(uid, self.last_seq.get(uid, 0)))

    def keepalive_due(self, uid: str, now: float | None = None) -> bool:
        """True when this camera is due for a keepalive.

        The camera's session dies in about twelve seconds without a
        message every two seconds -- the same interval the app's
        keepalive uses, and the one the vendor's own periodic push
        follows. The clock is the camera's own traffic: it speaks every
        second or two, and the answer has to be periodic, not one per
        frame.

        A camera that has just registered is given the full interval
        first: the registration answer is itself the first message of
        the session.
        """
        if now is None:
            now = time.monotonic()
        last = self.last_keepalive.get(uid)
        if last is None:
            self.last_keepalive[uid] = now
            return False
        if now - last < KEEPALIVE_INTERVAL:
            return False
        self.last_keepalive[uid] = now
        return True

    def keepalive(self, uid: str) -> bool:
        """Keep a camera's session alive, the way the vendor does.

        The vendor's answer to the camera's pullVideo is the go-ahead
        (`cmd 0x8001`, body `00 03 00 02 00`), and it has to arrive
        every couple of seconds -- the camera's session dies in about
        twelve.

        NOT on this beat: the `0x8005` media trigger (see
        push_relay_trigger). It was fired here once -- live, 2026-10-24
        -- and broke the handshake: the camera reconnect-looped instead
        of settling, and no media resulted. The trigger belongs after an
        arm and only behind --relay-start; the go-ahead below is the
        whole keepalive, and exactly all the wiretaps show the vendor's
        own beat carrying.

        Returns True when the session was fed this tick -- a failed
        send must not push the next attempt out.

        The camera's re-registration every forty seconds makes it a
        good oracle: the bridge's `rx_datagrams` climbing is the only
        signal that one of these was understood.
        """
        if self.camera_links.get(uid) is None:
            return False
        # The answer echoes the sequence number of the request it
        # answers (the capture does), so the last one seen is used.
        seq = self.last_seq.get(uid, 0)
        # Every frame goes through send_to, so a pushed frame and a
        # normal answer can never interleave on the socket.
        if not self.send_to(uid, vendor_pullvideo_answer(uid, seq)):
            return False
        # NOT on this beat: the 0x8005 media trigger. Firing it here -- live,
        # 2026-10-24 -- broke the camera's session: it reconnect-looped and
        # never reached the blue-LED/connected state it had before, and no
        # media resulted. The firmware reading agrees (each fire closes a
        # live session fd, SIGNALLING_PROTOCOL.md §6): the trigger belongs
        # after an arm, once, behind --relay-start. The go-ahead above is
        # the whole keepalive, and the only frame the wiretaps show the
        # vendor sending on this beat.
        return True

    def push_start(self, uid: str) -> None:
        """Tell a camera to start sending its media.

        The viewer's live-view press is the app's
        `notify-live-event?eventType=5` (`CameraEventType.relay`), and
        what the cloud does with it is send the unsolicited `0x8005`.
        The broker fires that trigger only from the registered arm,
        once, and only when --relay-start asks for it (the unsolicited
        0x8005 was tried live on 2026-10-24 and broke the handshake --
        unvalidated until a capture shows media).
        """
        if self.camera_links.get(uid) is None:
            log("push", ("-", 0), f"uid={uid} not connected".encode())
            return
        self.push_relay_trigger(uid)

    def schedule_p2p_live(self, uid: str, force: bool = False) -> None:
        """Start the vendor's direct live-view chain for a camera (§13).

        The register's `0x8000` arm is already out. What the app's own
        flow adds is the `0x8101` viewer-address delivery (the camera
        applies the subnet rule, binds its P2P socket to the RTP port
        and waits for the viewer's hello) followed, 1.5 s later, by
        `0x8004` value 1 (PlayLiveview -- the firmware rejects it while
        the P2P socket is unbound) and the hello punch.

        This is the DIRECT chain, not the relay: it is not gated behind
        --relay-start, which only governs the `0x8005` relay trigger
        (`push_relay_trigger`). Gating this one too is what left both
        cameras registered and silent on 2026-10-24: the flag went off,
        the `0x8101` was held, the camera never bound its P2P socket,
        and `0x8004` was rejected as "p2p is not finish".

        An unchanged viewer pair is not re-sent within P2P_RESEND_MIN_S
        of the last 0x8101 (§18.7); a moved pair always is. `force`
        means the camera has just registered: its state is fresh, so
        any chain and resend floor from the previous connection are
        stale and dropped first.
        """
        if force:
            self.p2p_fire_at.pop(uid, None)
            self.p2p_ready.pop(uid, None)
            self.p2p_sent_at.pop(uid, None)
        if self.camera_links.get(uid) is None:
            log("push", ("-", 0),
                f"uid={uid} not connected -- 0x8101 skipped".encode())
            return
        viewer = self.viewer_for(uid)
        priv_ip = str(viewer.get("privateIp") or "").strip()
        try:
            priv_port = int(viewer.get("privatePort") or 0)
        except (TypeError, ValueError):
            priv_port = 0
        if not priv_ip or not priv_port:
            log("push", ("-", 0),
                f"uid={uid} no viewer registered -- 0x8101 skipped".encode())
            return
        pub_ip = str(viewer.get("publicIp") or "").strip() or priv_ip
        try:
            pub_port = int(viewer.get("publicPort") or 0) or priv_port
        except (TypeError, ValueError):
            pub_port = priv_port
        pair = (pub_ip, pub_port, priv_ip, priv_port)
        if uid in self.p2p_fire_at:
            if self.p2p_sent_pair.get(uid) == pair:
                # A play is already pending for this camera; the camera
                # does not need a second viewer address.
                return
            # The viewer moved (e.g. the bridge restarted with a new
            # port while the chain for the stale pair was pending): the
            # camera streams to the pair of the last 0x8101 (§18.3), so
            # it needs the new one, and a "socket bound" earned by the
            # old pair must not fire the play (§18.5).
            log("push", ("-", 0),
                f"uid={uid} viewer moved -- 0x8101 re-sent".encode())
            self.p2p_ready.pop(uid, None)
        age = time.monotonic() - self.p2p_sent_at.get(uid, float("-inf"))
        if (not force and pair == self.p2p_sent_pair.get(uid)
                and age < P2P_RESEND_MIN_S):
            log("push", ("-", 0),
                f"uid={uid} 0x8101 sent {age:.0f}s ago -- held".encode())
            return
        seq = self.last_seq.get(uid, 0)
        frame = vendor_p2p_conn_req(uid, seq, pub_ip, pub_port,
                                    priv_ip, priv_port,
                                    self.pending_seq.get(uid, 0))
        if not self.send_to(uid, frame):
            return
        self.p2p_sent_pair[uid] = pair
        self.p2p_sent_seq[uid] = seq
        # The camera re-binds after every 0x8101, so the old answer is
        # stale too.
        self.p2p_answer.pop(uid, None)
        self.p2p_sent_at[uid] = time.monotonic()
        # The camera re-binds after every 0x8101, so ports reported for
        # an earlier chain are stale.
        self.p2p_ports[uid] = []
        log("push", ("-", 0),
            f"uid={uid} seq={seq} 0x8101 viewer "
            f"{pub_ip}:{pub_port}/{priv_ip}:{priv_port}".encode())
        # The play waits for the camera to bind its P2P socket, which it
        # signals by reporting mediaState 3 -- `p2p_socket_bound` fires it
        # then. This deadline only bounds the wait: if the camera never
        # reports, the play is dropped, not sent (§16.3).
        self.p2p_fire_at[uid] = time.monotonic() + P2P_PLAY_FALLBACK_S

    def p2p_socket_bound(self, uid: str) -> bool:
        """The camera says its P2P socket is up: schedule the play.

        `0x8101` tells the camera the viewer's address; it then starts
        its P2P thread, binds the RTP port and waits there for the
        viewer's hello. The camera says so itself: its `0x0004` status
        report carries the media state, and **3** means "P2P socket
        bound, waiting for the viewer" (§13.7). That is exactly the
        precondition PlayLiveview checks (§13.3: `peerToPeerFlag == 1`,
        else "p2p is not finish"), so the play waits for that report --
        not for a timer.

        The play is then a *short* delay away, not immediate: the
        camera's `0x0003` with its freshest RTP port lands just after
        the report, and the viewer's hello has to reach that port
        **before** the play, because PlayLiveview closes the P2P socket
        (§13.3) and the app punches before it starts the view (§14.1).

        Live 2026-10-24 21:07: a 1.5 s timer fired the play *before* the
        camera had bound its socket, and the camera sent nothing -- it
        sat there asking to pull video (30 x `cmd 0x0002`) and never
        pushed a byte.

        Returns True when a play was scheduled.
        """
        if uid not in self.p2p_fire_at:
            # No 0x8101 is outstanding for this camera, so there is no
            # play to schedule.
            return False
        now = time.monotonic()
        self.p2p_ready[uid] = now
        self.p2p_fire_at[uid] = now + P2P_PLAY_SETTLE_S
        return True

    def serve_due_p2p_fires(self, now: float | None = None) -> int:
        """Send each due `0x8004` PlayLiveview and punch the camera.

        A camera that dropped its session before the fire came due is
        skipped: the next register re-delivers 0x8101 and reschedules.

        The order is the app's (§14.1): the viewer's hello reaches the
        camera's RTP port first, then the play. The camera's P2P thread
        is waiting for exactly that datagram, and PlayLiveview closes
        the socket it was waiting on (§13.3).
        """
        if now is None:
            now = time.monotonic()
        sent = 0
        for uid, due in list(self.p2p_fire_at.items()):
            if now < due:
                continue
            del self.p2p_fire_at[uid]
            ready = self.p2p_ready.pop(uid, None)
            if ready is None:
                # The deadline passed with no mediaState-3 report. A
                # play sent now is the one the firmware ignores
                # ("p2p is not finish", §16.3).
                log("push", ("-", 0),
                    f"uid={uid} 0x8004 dropped -- P2P socket never bound"
                    .encode())
                continue
            if self.camera_links.get(uid) is None:
                log("push", ("-", 0),
                    f"uid={uid} 0x8004 due but not connected -- dropped"
                    .encode())
                continue
            # The hello first, at the port the camera has just told us
            # is its RTP port.
            self.punch_camera(uid)
            seq = self.last_seq.get(uid, 0)
            if self.send_to(uid, vendor_play_ctrl(uid, seq, 1)):
                sent += 1
                log("push", ("-", 0),
                    f"uid={uid} seq={seq} 0x8004 play-liveview (p2p bound)"
                    .encode())
        return sent

    def note_p2p_port(self, uid: str, port: int) -> None:
        """Remember an RTP port the camera reported for this chain."""
        ports = self.p2p_ports.setdefault(uid, [])
        if port and port not in ports:
            ports.append(port)

    def punch_camera(self, uid: str) -> None:
        """Send the viewer's hello to the camera's RTP port(s) (§13.2).

        The camera's P2P socket is bound to the RTP port its STUN comes
        from; the first datagram there is the vendor's "App send
        hello". The media goes to the pair the camera armed from
        `0x8101`, not to the punch's source, so any local socket can
        punch. The camera often reports two different ports per chain
        (the answer to the 0x8101, then a redraw), and captures show
        .107's P2P socket on the first while .108 went live on the
        second (`SIGNALLING_PROTOCOL.md` §18.6), so every distinct
        reported port gets the hello; one nobody listens on is harmless.
        """
        camera = self.cameras.get(uid) or {}
        ip = camera.get("_ip") or ""
        ports = [int(p) for p in self.p2p_ports.get(uid) or []]
        last = int(camera.get("_port") or 0)
        if last and last not in ports:
            ports.append(last)
        if not ip or not ports:
            log("push", ("-", 0),
                f"uid={uid} no camera address to punch".encode())
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _ in range(3):
                for port in ports:
                    s.sendto(b"hello", (ip, port))
                time.sleep(0.15)
            log("push", ("-", 0),
                f"uid={uid} hello x3 -> {ip}:"
                f"{','.join(str(p) for p in ports)}".encode())
        except OSError as exc:
            log("push", ("-", 0), f"uid={uid} punch failed {exc}".encode())
        finally:
            s.close()

    def push_relay_trigger(self, uid: str) -> bool:
        """Send the camera the `0x8005` that starts it sending.

        The camera's firmware splits the media trigger in two
        (`ziot-fw/SIGNALLING_PROTOCOL.md` §5-§6):

        * the `0x8000` registration answer **arms** the target -- the
          camera copies the relay ip + port and the STUN host + port
          out of the frame into its device structure (via
          `spook_rtsp_set_relay_info`) and sets its session state to 3
          -- but it does not connect to anything;
        * an unsolicited `0x8005` **starts** it: its handler sets a
          relay-start flag (`dev->0xc8 = 2`) and closes any stale
          session fd, and a separate state machine then runs
          `rtsp_p2p_start`, which opens a **UDP RTP** socket and
          sendto's JPEG (RTP/AVP 26) + PCMU (RTP/AVP 0) to the armed
          ip:port -- so the armed target must be a UDP RTP sink.

        The body of `0x8005` is empty -- the handler reads nothing from
        it -- so it is a pure trigger, and in the vendor's flow the
        app's `notify-live-event?eventType=5` (`CameraEventType.relay`)
        is what makes the cloud send it.

        Held unless self.relay_start (--relay-start): firing it
        unsolicited on 2026-10-24 made the camera reconnect-loop instead
        of settling, with no media at all. Every fire also closes a
        live session fd (§6) -- so once the chain works, this wants a
        once-per-arm schedule, not a periodic one.

        Returns True when the frame went out.
        """
        if not self.relay_start:
            log("push", ("-", 0),
                f"uid={uid} --relay-start off: cmd 0x8005 held".encode())
            return False
        if self.camera_links.get(uid) is None:
            return False
        seq = self.last_seq.get(uid, 0)
        frame = vendor_relay_trigger(uid, seq)
        if not self.send_to(uid, frame):
            return False
        log("push", ("-", 0),
            f"uid={uid} seq={seq} relay-trigger cmd=0x8005".encode())
        return True

    def schedule_relay_trigger(self, uid: str) -> None:
        """Arm ONE `0x8005` for this register, RELAY_START_DELAY_S out.

        The trigger must not land inside the handshake it responds to:
        a fire within that second breaks the camera's session (live,
        2026-10-24), and the vendor only ever triggers once a viewer
        wants the stream -- long after the register settles. The pending
        fire lives in `relay_trigger_at` until the keepalive worker's
        clock sends it, at most one per camera at a time.
        """
        if not self.relay_start or uid in self.relay_trigger_at:
            return
        self.relay_trigger_at[uid] = time.monotonic() + RELAY_START_DELAY_S

    def serve_due_relay_triggers(self, now: float | None = None) -> int:
        """Send any due trigger whose camera is still connected.

        A camera that dropped the session before its fire came due gets
        thrown away, not fired into the void: the next register will
        schedule it again. Returns the number of frames pushed.
        """
        if now is None:
            now = time.monotonic()
        sent = 0
        for uid, due in list(self.relay_trigger_at.items()):
            if now < due:
                continue
            del self.relay_trigger_at[uid]
            if self.camera_links.get(uid) is None:
                log("push", ("-", 0),
                    f"uid={uid} relay-trigger due but not connected "
                    "-- dropped".encode())
                continue
            sent += 1 if self.push_relay_trigger(uid) else 0
        return sent

    def set_camera_address(self, uid: str, ip: str, port: int) -> bool:
        """Record the address a camera's own call came from.

        The camera's STUN Binding Request comes from the port its media
        socket listens on, so this is how the vendor's cloud learns where
        a camera is -- and how the bridge learns where to send its
        requests.

        Returns True when the address changed: a camera that re-runs
        STUN from a *new* port has just bound a new socket (the P2P one,
        after a `0x8101`), and that is the moment PlayLiveview has to
        arrive -- the firmware rejects it while the socket is unbound
        ("p2p is not finish").
        """
        camera = self.cameras.get(uid)
        if camera is None:
            return False
        if camera.get("_ip") == ip and camera.get("_port") == port:
            return False
        camera["_ip"] = ip
        camera["_port"] = port
        self.save_cameras()
        return True

    def set_media_state(self, uid: str, value: int) -> None:
        """Record the camera's own media state (§13.7).

        The device list's `mediaState` is the camera's own report, not a
        server-side opinion: the camera sends it as `cmd 0x0004`, wire
        id 3. 3 means "P2P socket bound, waiting for the viewer", 1
        direct live, 2 relay live, 0 stopped.
        """
        camera = self.cameras.get(uid)
        if camera is None:
            return
        if str(camera.get("mediaState", "")) == str(value):
            return
        camera["mediaState"] = str(value)
        self.save_cameras()

    def set_camera_public(self, uid: str, ip: str, port: int) -> None:
        """Record the camera's own STUN mapping (its public pair).

        §14.1: the vendor's `IpcPublicIP/IpcPublicPort` is the
        **camera's** pair, not the viewer's -- the phone and the camera
        shared one NAT in the capture, so the IPs matched, but the ports
        never did. The camera reports it in `cmd 0x0003` (the STUN
        result at body 0/20).
        """
        camera = self.cameras.get(uid)
        if camera is None:
            return
        if camera.get("_pub_ip") == ip and camera.get("_pub_port") == port:
            return
        camera["_pub_ip"] = ip
        camera["_pub_port"] = port
        self.save_cameras()

    def note_p2p_answer(self, uid: str, seq_no: int, pub_ip: str,
                        pub_port: int, loc_ip: str, loc_port: int) -> bool:
        """Keep the 0x0003 that answers the last 0x8101; True if it did.

        The answering report carries seq_no == the 0x8101's header seq
        (verified in every chain: 1->1, 13->13, 87->87, 210->210); a
        later "redraw" report carries seq_no 0 and often another port.
        The P2P socket is the answering report's port (camera .107
        punched us from it every time, the redraw port got nothing),
        and the vendor's `stun-addr` serves that record (§14.1, §18.8).
        """
        if not seq_no or seq_no != self.p2p_sent_seq.get(uid):
            return False
        self.p2p_answer[uid] = (pub_ip, int(pub_port), loc_ip, int(loc_port))
        log("push", ("-", 0),
            f"uid={uid} p2p socket {loc_ip}:{loc_port} "
            f"(answers 0x8101 seq {seq_no})".encode())
        return True

    def set_pending_seq(self, uid: str, n: int) -> None:
        """The registration number to carry in the next `0x8101`."""
        if n:
            self.pending_seq[uid] = n

    def set_reported_seq(self, uid: str, n: int) -> None:
        """Record the registration number the camera has reported.

        §14.1: each `send-stun-addr` takes the next N for the camera;
        the camera echoes N back in its `0x0003`, and only then does
        `stun-addr` show it. The app waits for its own N and does
        nothing else until it sees it -- which is why the 13:47 app
        gave up and went to relay.
        """
        self.reported_seq[uid] = n

    def save_cameras(self) -> None:
        """Write the camera registry out, so a restart keeps it.

        A broker that forgets the cameras looks like a cloud that has
        never heard of them: the bridge gets no address and the app
        gets an empty device list.
        """
        if not self.camera_file:
            return
        try:
            with open(self.camera_file, "w") as fh:
                json.dump(self.cameras, fh, indent=1)
        except OSError:
            pass

    def load_cameras(self) -> None:
        """Read the camera registry back at startup."""
        if not self.camera_file:
            return
        try:
            with open(self.camera_file) as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(saved, dict):
            for uid, camera in saved.items():
                if uid not in self.cameras and isinstance(camera, dict):
                    # Nothing is live at startup: a mediaState left over
                    # from the last run would make the app refuse the
                    # camera (`设备繁忙`, §17.1) until the camera
                    # reported again. Idle is "0".
                    camera["mediaState"] = "0"
                    self.cameras[uid] = camera
                    log("camera", ("-", 0),
                        f"uid={uid} restored from {self.camera_file}".encode())

    def _camera_row(self, uid: str) -> dict:
        row = dict(self.cameras.get(uid) or {})
        for key in ("_ip", "_port"):
            row.pop(key, None)
        # The local RTSP relay's address: the device list's `relay_ip`
        # (experiments with `ziot_relay.py` and explicit --relay-port
        # runs). The camera's own ingest arm is decided inside
        # arm_target(): the registered viewer -- the bridge's UDP media
        # socket -- comes first, this only when no viewer has
        # registered.
        relay = self.relay_address()
        if relay:
            row["relay_ip"] = f"{relay['host']}:{relay['port']}"
            row["server_ip"] = relay["host"]
        return row

    def relay_address(self) -> dict:
        """The local RTSP relay's address, or an empty dict.

        An experiment target (`--relay-port`, `ziot_relay.py`), not the
        camera's own media leg: the camera sends plain UDP RTP to the
        address the registration answer arms (SIGNALLING_PROTOCOL.md
        §6), and that arm is decided in arm_target() -- the registered
        viewer first, this slot only when no viewer has registered.
        """
        if not self.relay_port:
            return {}
        return {"host": self.relay_host, "port": self.relay_port}

    def _stun(self, uid: str, receive_ts: bool = True,
              seq_no: int | None = None) -> dict:
        """The camera's address, as the capture shows it.

        The two endpoints answer with slightly different shapes, and the
        difference is in the capture: send-stun-addr carries `receiveTs`,
        stun-addr carries `updateTime` instead.

        **Both pairs are the camera's own** (§14.1, two vendor-era
        captures): the public pair is the camera's STUN mapping -- the
        address and port the camera's own STUN call came from, which the
        camera reports in `cmd 0x0003` -- and the private pair is its
        local pair, the RTP port its media comes from. The earlier
        reading (the public pair being the *viewer's*) came from a
        capture where phone and camera shared one NAT, so the IPs matched
        and only the ports disagreed.

        `seqNo` is the registration number: the one `send-stun-addr`
        handed out for this camera, but only once the camera has
        reported answering it (§14.1). The app waits for its own number
        to come back and does nothing else until it does.
        """
        cam = self.cameras.get(uid) or {}
        public_ip = cam.get("_pub_ip") or cam.get("_ip") or ""
        try:
            public_port = int(cam.get("_pub_port") or cam.get("_port") or 0)
        except (TypeError, ValueError):
            public_port = 0
        out = {
            "IpcPublicIP": public_ip,
            "IpcPublicPort": public_port,
            "IpcPrivateIP": cam.get("_ip", ""),
            "IpcPrivatePort": cam.get("_port", 0),
        }
        answer = self.p2p_answer.get(uid)
        if answer is not None:
            # The vendor serves the report that answered the 0x8101
            # (§14.1, §18.8); a later redraw to another port must not
            # move the bridge's punch target off the P2P socket.
            (out["IpcPublicIP"], out["IpcPublicPort"],
             out["IpcPrivateIP"], out["IpcPrivatePort"]) = answer
        if receive_ts:
            out["receiveTs"] = int(time.time())
        # The number: `send-stun-addr` answers with the one it has just
        # taken for this camera, `stun-addr` with the one the camera has
        # reported answering (§14.1). The app waits for its own number to
        # come back before it starts the view.
        out["seqNo"] = (seq_no if seq_no is not None
                        else self.reported_seq.get(uid, 0))
        return out

    def _register_viewer(self, uid: str, one: dict) -> int:
        """Remember where the viewer wants the media delivered.

        The viewer's address is what the camera is told over the signalling
        connection, so it has to be kept: it is the one piece of state the
        broker cannot invent. In the capture the app registers
        publicIp=177.221.120.116, publicPort=64550, privateIp=192.168.18.30,
        privatePort=55795 -- the address its own media socket listens on.

        It is written to disk as well, because losing it means telling the
        camera to punch at the wrong address: a restarted broker that
        forgot the viewer would send the camera back to the vendor's own
        STUN server.

        Returns the registration number taken for this camera (§14.1):
        the next N for it, which the answer carries and the camera
        echoes back in its `0x0003`.
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
        self._save_viewers()
        log("viewer", ("-", 0),
            f"uid={uid} -> {one.get('privateIp')}:{private_port}".encode())
        # The registration number for this camera (§14.1): the next one
        # for it, which the answer carries, which the `0x8101` carries
        # to the camera, and which the camera echoes back in its
        # `0x0003`. Only then does `stun-addr` show it.
        n = self.seq_no.get(uid, 0) + 1
        self.seq_no[uid] = n
        return n

    def _save_viewers(self) -> None:
        """Write the viewer registrations out, so a restart keeps them."""
        if not self.viewer_file:
            return
        try:
            with open(self.viewer_file, "w") as fh:
                json.dump(self.viewers, fh, indent=1)
        except OSError:
            pass

    def load_viewers(self) -> None:
        """Read the viewer registrations back at startup."""
        if not self.viewer_file:
            return
        try:
            with open(self.viewer_file) as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(saved, dict):
            self.viewers.update(saved)
            for uid, viewer in saved.items():
                log("viewer", ("-", 0),
                    f"uid={uid} -> {viewer.get('privateIp')}:"
                    f"{viewer.get('privatePort')} (restored)".encode())

    def viewer_for(self, uid: str) -> dict:
        """The viewer registered for a camera, or an empty dict."""
        return dict(self.viewers.get(uid) or {})

    def respond(self, method: str, path: str, query: str = "",
                body: bytes = b"", token: str = "") -> bytes | None:
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

        # The account's credential is not checked here. The camera never
        # presents one (its firmware has no HTTP client), so the token is
        # only between the app or the bridge and this broker, and this
        # broker is the cloud: it issued the token, so checking it would
        # be checking its own bookkeeping. A vendor-style 401 would also
        # put the bridge into its "the cloud rejected the token" path,
        # which is a dead end for a local cloud. A token that is not one
        # of ours is still worth noticing once: the bridge carrying the
        # vendor's own JWT works against this broker, but a locally
        # minted token keeps the log honest about who issued what.
        if token and not local_token_valid(token):
            _warn_bad_token(token)

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
        elif path == "/api/v1/ipc-function/get-sig-ip":
            # §17.5: the app reads the answer's `code` and only when it
            # equals 400 takes `data` (a plain "host:port" string) and
            # saves it as `icpSigAddress` -- the urlAndPort its BLE
            # provisioning frame carries. The vendor's success marker
            # here really is 400, so this answer deliberately breaks the
            # {"code": 200, ...} envelope every other route uses.
            payload = json.dumps({"requestId": str(uuid.uuid4()),
                                  "code": 400, "msg": msg,
                                  "data": self.sig_address()}).encode()
            return _http_response("200 OK", payload)
        elif path == "/api/v1/ipc/send-stun-addr":
            uid = one.get("uid", "")
            # The viewer registers where it wants the media delivered; the
            # camera is told that address over the signalling connection.
            n = self._register_viewer(uid, one)
            data = self._stun(uid, seq_no=n)
            # §15 item 5: the viewer address reaches the camera in the
            # `0x8101`, and the registration number goes with it (the
            # camera echoes it in its `0x0003`). This is the app's
            # order: the viewer registers, the camera binds its P2P
            # socket, the app sees its own number come back and only
            # then starts the view.
            self.set_pending_seq(uid, n)
            self.schedule_p2p_live(uid)
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
            # The event reaches the camera over the signalling connection.
            # eventType "1" is "start live" and "4" is "connected" -- the
            # app's P2P-established notify, which replaces start on every
            # X5 newer than V1.0.11.46, i.e. both our units
            # (SIGNALLING_PROTOCOL.md §17.2). The cloud's translation for
            # 4 was never captured; the camera has only one live-start
            # arm (PlayLiveview), so it gets the same chain. The other
            # event types the app sends must not fire the punch: a push
            # fired on a stop event makes the rotation's reaction --
            # which can only be read in the bridge's datagram count --
            # unattributable.
            if event in ("1", "4"):
                # The direct chain's own timing: the 0x8101 goes out now
                # and the 0x8004 1.5 s later, once the camera has bound
                # its P2P socket. The caller must not overwrite the due
                # time -- that would fire the play before the viewer
                # address had been delivered, and the firmware rejects
                # PlayLiveview while the P2P socket is unbound.
                self.schedule_p2p_live(uid)
            elif event == "0":
                # §13.6/§15 item 6: the bridge's keepAlive every 2 s is
                # the camera's live heartbeat. Without the 0x8002 the
                # camera stops every live view 36 s after its start
                # ("live heartbeat timeout, stop live") -- seen live
                # 16:14-16:23, where every stream was a ~36 s burst.
                self.send_heartbeat(uid)
            elif event in ("2", "3"):
                # pause (2) and stop (3) are one command with a value in
                # its body (vendor_play_ctrl, §13.3). The bridge sends
                # stop when it tears a session down; not translating it
                # left the camera pushing into a socket nobody reads.
                self.send_to(uid, vendor_play_ctrl(
                    uid, self.last_seq.get(uid, 0), int(event)))
            elif event == "5":
                # §13.6: eventType 5 is `CameraEventType.relay`, and the
                # cloud answers it with the unsolicited `0x8005` -- the
                # relay start (mode 2, §6). The trigger is held unless
                # --relay-start asks for it: fired unsolicited on
                # 2026-10-24 it broke the camera's handshake.
                self.push_relay_trigger(uid)
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

    def __init__(self, addr, kind, api: "HttpApi | None" = None,
                 own_ip: str = "", relay_ip: str = "",
                 relay_port: int = 0, relay_port2: int = 0):
        self.kind = kind
        self.api = api
        # This host's address on the LAN: what the camera is told when
        # it asks where to send its media and where the signalling
        # server is.
        self.own_ip = own_ip
        # A local RTSP relay's address -- the fallback arm target for
        # sink experiments (`--relay-port`, `ziot_relay.py`). The
        # camera's own media leg is plain UDP RTP to the armed address
        # (SIGNALLING_PROTOCOL.md §6); arm_target() prefers the
        # registered viewer (the bridge's UDP media socket), so this
        # only fills when no viewer has registered.
        self.relay_ip = relay_ip
        self.relay_port = relay_port
        # The second relay port slot of the vendor's capture (554 at
        # offset 0x16, 10000 at 0x18): the camera reads the SECOND one.
        # Kept for capture-rebuild experiments; the arm writes the same
        # sink port into both slots either way.
        self.relay_port2 = relay_port2
        # Which ack candidate each camera gets next: the reply format is
        # not in the firmware's strings, so the candidates are tried in
        # turn and the camera's reaction decides.
        self.ack_index: dict[str, int] = {}
        # The candidate sweep is off by default: the protocol is known
        # now (SIGNALLING_PROTOCOL.md §4 -- the ack is a 0x8001 with a
        # `cSeq/cmd/ack` body), and the sweep's frames are not ones the
        # vendor sends. --ack-sweep turns it back on for experiments.
        self.ack_sweep = False
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
        # The camera's own STUN client (CUSTOM_TASK_StunXorAddr,
        # SIGNALLING_PROTOCOL.md §12.2) walks the attributes looking for
        # type 0x0020 (RFC 5389 XOR-MAPPED-ADDRESS) and returns failure
        # -- "call stun server fail", then a sig teardown after 4 tries
        # -- when it is absent. The phone's old RFC 3489 shape above is
        # kept for it; this one is for the camera.
        attr(0x0020, _stun_xor_addr(seen_ip, peer_port, cookie, txid)),
    ])
    return struct.pack("!HH", 0x0101, len(body)) + cookie + txid + body


class StunHandler(socketserver.BaseRequestHandler):
    """Answer a STUN Binding Request the way the vendor's server does.

    The camera asks this server to tell it its own address -- that is what
    lets it register and be reached. Answering it is not optional for a
    camera that has to talk to our broker instead of the vendor's.

    The call also tells us the camera's address: it comes from the
    camera, so the peer's source port is the port the camera's media
    socket listens on. That is the address the bridge needs to reach the
    camera, and the vendor's cloud learns it the same way.
    """

    def handle(self):
        data, sock, dest = self.request
        # `dest` is the local address the request was sent to, and the
        # reply has to come from it (§12.5): the phone sends to
        # `stunaddr.gps555.net` (a claimed vendor address) and drops an
        # answer that comes from anywhere else. The socket is bound to
        # 0.0.0.0, so the kernel would pick the source address from the
        # route; IP_PKTINFO tells us the destination the request
        # arrived at, and lets us answer from it.
        log("stun", self.client_address, data)
        reply = stun_binding_success(data, self.client_address[0],
                                     self.client_address[1],
                                     dest or self.server.own_ip,
                                     self.server.server_address[1])
        if reply:
            if dest:
                self._send_from(sock, reply, dest)
            else:
                sock.sendto(reply, self.client_address)
            log("stun", self.client_address, b"-> " + reply[:24].hex().encode())
        api = self.server.api
        if api is not None:
            ip, port = self.client_address[0], self.client_address[1]
            for uid, camera in api.cameras.items():
                # The camera is on the LAN with us, so its address is the
                # one it just called from.
                if camera.get("_ip") in ("", ip):
                    api.set_camera_address(uid, ip, port)
                    log("stun", self.client_address,
                        f"uid={uid} media at {ip}:{port}".encode())
                    break

    def _send_from(self, sock, payload: bytes, src: str) -> None:
        """Send with the source address the request came to."""
        try:
            info = struct.pack("i4s4s", 0, socket.inet_aton(src),
                               b"\x00" * 8)
            sock.sendmsg([payload],
                         [(socket.IPPROTO_IP, IP_PKTINFO, info)],
                         0, self.client_address)
        except OSError:
            sock.sendto(payload, self.client_address)


class StunServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, own_ip: str = "", api: "HttpApi | None" = None):
        self.own_ip = own_ip
        self.api = api
        super().__init__(addr, StunHandler)

    def server_bind(self):
        # Ask for the destination address of every datagram, so the
        # reply can come from the address the request was sent to (§12.5).
        try:
            self.socket.setsockopt(socket.IPPROTO_IP, IP_PKTINFO, 1)
        except OSError:
            pass
        socketserver.ThreadingUDPServer.server_bind(self)

    def get_request(self):
        """Read one datagram, with the address it was sent to."""
        try:
            data, ancdata, _, addr = self.socket.recvmsg(
                self.max_packet_size, socket.CMSG_SPACE(64))
        except OSError:
            return (b"", self.socket, ""), addr
        dest = ""
        for level, ctype, cdata in ancdata:
            if level == socket.IPPROTO_IP and ctype == IP_PKTINFO:
                try:
                    _, spec_dst, _ = struct.unpack("i4s4s", cdata[:12])
                    dest = socket.inet_ntoa(spec_dst)
                except (OSError, struct.error):
                    dest = ""
        return (data, self.socket, dest), addr


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
#     86 86 | LEN16 | SEQ16 | DLEN16 | payload | XOR8 | 0x0D
#     LEN16 = 5 + DLEN (seq + dlen + payload + xor); all multi-byte BE
#     XOR8  = xor of every preceding byte
#     0x0D  = end symbol, NOT counted by LEN16
#
# The end symbol was missing here until 2026-10-23, when the camera itself
# showed it: a frame without it draws silence (the camera waits for the rest
# of the length), a frame with it draws an answer -- even a rejection
# (`ffff00` = "command not understood"). So the camera never acted on the
# frames we sent because it never received a complete one.
# (DECOMPILATION_REPORT.md §14.1.)
#
# wifiConnect payload (cmd 0x00,0x01):
#     00 01 | <len><ssid utf-8> | <len><pwd utf-8> | <len><sigAddr utf-8> |
#     <tz minutes, negative biased +0x8000, big-endian> | [regCode 4B BE]
#
# The firmware parses nine fields (cmd, ssid, pwd, urlAndPort, p1, port,
# timezone, bind id, param bind id) -- DECOMPILATION_REPORT.md §13.3 -- so
# this payload is still missing `p1`, `port` and the two binding ids.
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


def string_to_uint32(text: str) -> int:
    """The app's regCode nonce: `DeviceAddData.stringToUint32`.

    sha256(text), then digest bytes 0, 2, 4 and 6 packed big-endian
    (device_add_controller.dart 0x6e7d5c: lsl #0x18, #0x10, #8, #0).
    The app feeds it `phoneUuid + DateTime.microseconds`, which makes it a
    nonce rather than a credential -- the camera cannot validate a phone
    UUID -- but the app sends it for every real device, so the four bytes
    belong in the frame.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return (digest[0] << 24) | (digest[2] << 16) | (digest[4] << 8) | digest[6]


def wifi_connect_frame(ssid: str, pwd: str, sig_addr: str, tz_minutes: int,
                       seq: int = 1, reg_code: int = 0) -> bytes:
    # Negative minutes are biased +0x8000, not two's complement
    # (custom_ble_protocol_packet.dart 0x53ec64/0x53ecc4; the app's
    # _getTimezoneOffset is microseconds ~/ 60000000, then +0x8000).
    # reg_code is the app's 4-byte nonce (string_to_uint32); 0 omits it,
    # which the app only does for its "FFFFFFFFFFFF" placeholder device.
    t = tz_minutes + 0x8000 if tz_minutes < 0 else tz_minutes
    payload = (b"\x00\x01" + _lp(ssid) + _lp(pwd) + _lp(sig_addr) + _be16(t))
    if reg_code:
        payload += _be16(reg_code >> 16) + _be16(reg_code & 0xFFFF)
    body = _be16(seq) + _be16(len(payload)) + payload
    head = b"\x86\x86" + _be16(len(body) + 1)
    return head + body + _u8(_xor(head + body)) + b"\x0d"


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
          own_ip: str = "",
          relay_ip: str = "",
          relay_port: int = 0,
          relay_port2: int = 0) -> socketserver.BaseServer:
    srv = StunServer(addr, own_ip, api) if kind == "stun" \
        else StreamServer(addr, kind, api, own_ip, relay_ip, relay_port,
                          relay_port2)
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
    ap.add_argument("--mint-token", action="store_true",
                    help="print a token this broker accepts and exit")
    ap.add_argument("--http", type=int, default=80, help="HTTP API port (0=off)")
    ap.add_argument("--sig", type=int, default=8882, help="signalling port (0=off)")
    ap.add_argument("--stun", type=int, default=13478, help="STUN UDP port (0=off)")
    ap.add_argument("--relay-port2", type=int, default=0,
                    help="a second port to write into the registration "
                         "answer's other slot (offset 0x16; the camera "
                         "reads 0x18). 0 = the same as --relay-port. "
                         "Kept for capture-rebuild experiments.")
    ap.add_argument("--relay-port", type=int, default=0,
                    help="fallback arm target in the registration answer "
                         "when no viewer has registered (experiments with "
                         "a local relay). The camera sends plain UDP RTP "
                         "there -- the registered viewer's media socket is "
                         "preferred when one exists.")
    ap.add_argument("--relay-start", action="store_true",
                    help="EXPERIMENT: send the unsolicited cmd 0x8005 "
                         "media trigger once after every arm (the camera "
                         "then sendto's UDP RTP to the armed address). "
                         "Tried live 2026-10-24, it broke the handshake "
                         "-- keep off until a capture shows where it "
                         "fits (SIGNALLING_PROTOCOL.md §0/§6/§9)")
    ap.add_argument("--strict-bind", action="store_true",
                    help="a requested listener that cannot bind is fatal, "
                         "so a supervisor restarts the broker instead of "
                         "it running without, e.g., signalling; meant for "
                         "deployments (compose)")
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
    ap.add_argument("--camera-file", default="",
                    help="file to keep the camera registry in, so a restart "
                         "does not make the broker look like a cloud that "
                         "has never heard of the cameras (default: next to "
                         "the log file)")
    ap.add_argument("--viewer-file", default="",
                    help="file to keep the viewer registrations in, so a "
                         "restart does not forget where the media should "
                         "be delivered (default: next to the log file)")
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

    if args.mint_token:
        print(mint_local_token())
        return 0

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

    base = (os.path.splitext(args.log)[0] if args.log else "")
    api = HttpApi(viewer_file=args.viewer_file
                   or (base + "-viewers.json" if base else ""),
                  camera_file=args.camera_file
                   or (base + "-cameras.json" if base else ""),
                  relay_host=my_ip or _detect_lan_ip(),
                  relay_port=args.relay_port,
                  relay_start=args.relay_start,
                  sig_host=my_ip or _detect_lan_ip(),
                  sig_port=args.sig)
    api.load_viewers()
    api.load_cameras()
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
    failed = []
    for kind, port in (("http", args.http), ("sig", args.sig),
                       ("ws", args.ws), ("stun", args.stun)):
        if port:
            try:
                # The signalling port shares the API: a camera that
                # registers there must show up in the device list, which
                # is where the bridge looks it up.
                servers.append(serve(kind, (args.bind, port),
                                      api if kind in ("http", "sig", "stun")
                                      else None,
                                      my_ip or _detect_lan_ip(),
                                      relay_ip=my_ip or _detect_lan_ip(),
                                      relay_port=args.relay_port,
                                      relay_port2=args.relay_port2))
            except OSError as e:
                print(f"cannot bind {kind} port {port}: {e} -- ports below "
                      "1024 need root (see module docstring)", file=sys.stderr)
                failed.append(f"{kind}:{port}")
    # With --strict-bind, a listener the operator asked for that cannot
    # bind is fatal (opt-in: dev hosts often cannot bind :80 without
    # root, so the default reports and continues). A broker that stays
    # up without its signalling port looks healthy (the container
    # healthcheck only connects to :80) while no camera can register --
    # a silent outage; Docker restarts a container on exit, not on
    # "unhealthy", so exiting lets it retry until the port is free.
    # Every bind was attempted first so one run reports all failures.
    if failed and args.strict_bind:
        print(f"fatal: could not bind {', '.join(failed)} -- exiting so "
              "the supervisor can retry", file=sys.stderr)
        for srv in servers:
            srv.shutdown()
            srv.server_close()
        return 1
    # The session-keeper: feeds every connected camera every couple of
    # seconds, on a clock of its own -- a camera goes silent for 40-90 s
    # while it waits for that beat, and a session that is not fed dies
    # in about twelve. The 0x8005 media trigger is NOT on this beat
    # (firing it broke the handshake live); it flies only behind
    # --relay-start, once per arm.
    api.start_keepalive_worker()
    if args.dns:
        # DNS stays best-effort (non-fatal): the deploy runs --dns 0, and
        # on a dev host port 53 is often owned by systemd-resolved.
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

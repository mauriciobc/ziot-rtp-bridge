#!/usr/bin/env python3
"""Grade candidate BLE provisioning frames against the camera.

The camera is the oracle. Its own reply frame, decoded against the
firmware's format, is:

    86 86 | LEN | SEQ | DLEN | payload | XOR | 0d

with LEN counting the bytes after itself (SEQ + DLEN + payload + XOR) and
XOR covering magic..payload. A frame it cannot act on draws an error
payload `ffff00` ("command not understood"); a frame whose length it
accepts but whose body never completes draws silence, because it is still
waiting for the rest of the length ("BLE totalLen:%d, offset:%d").

The firmware says the provisioning frame carries nine fields -- cmd, ssid,
pwd, urlAndPort, p1, port, timezone, bind id, param bind id -- while the
frame built from the app's assembly carries six. This tries the layouts
the firmware implies and prints what the camera says about each.

Usage: att_candidates.py <ble-mac> [sig-addr] [ssid] [pwd]
"""
import hashlib
import socket
import struct
import sys
import time

CCCD = 0x000B
WRITE = 0x0008


def be16(n):
    return struct.pack("!H", n & 0xFFFF)


def lp(text):
    raw = text.encode()
    return bytes([len(raw)]) + raw


def frame(payload, seq=1, end=b"", len_includes_end=False):
    """Build one frame in the camera's own format."""
    body = struct.pack("!HH", seq, len(payload)) + payload
    length = len(body) + 1 + (1 if len_includes_end else 0)
    head = b"\x86\x86" + be16(length)
    x = 0
    for b in head + body:
        x ^= b
    return head + body + bytes([x]) + end


def parse(pdu):
    """Decode a camera notification payload."""
    if len(pdu) < 8 or pdu[:2] != b"\x86\x86":
        return {"raw": pdu.hex()}
    dlen = struct.unpack("!H", pdu[6:8])[0]
    return {"seq": struct.unpack("!H", pdu[4:6])[0],
            "payload": pdu[8:8 + dlen].hex(),
            "tail": pdu[8 + dlen:].hex()}


def att(s, op, p=b""):
    s.send(bytes([op]) + p)


def read(s, t=1.5):
    s.settimeout(t)
    try:
        return s.recv(512)
    except socket.timeout:
        return b""


def session(mac):
    """Open one L2CAP connection and return the socket, or None."""
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                      socket.BTPROTO_L2CAP)
    s.bind(("00:00:00:00:00:00", 0, 4, 1))
    s.settimeout(12)
    try:
        s.connect((mac, 0, 4, 1))
    except OSError as e:
        s.close()
        return None, str(e)
    att(s, 0x12, struct.pack("<H", CCCD) + b"\x01\x00")
    read(s, 1.0)
    return s, None


def probe(mac, label, pkt, wait=8.0):
    """Send one frame and collect everything the camera says."""
    s, err = session(mac)
    if s is None:
        return f"connect failed ({err})"
    out = []
    try:
        # 20-byte ATT writes, as the app chunks them.
        for i in range(0, len(pkt), 20):
            att(s, 0x12, struct.pack("<H", WRITE) + pkt[i:i + 20])
            rsp = read(s, 1.2)
            if rsp and rsp[0] == 0x1B:
                out.append(parse(rsp[3:]))
        end = time.time() + wait
        while time.time() < end:
            rsp = read(s, 0.6)
            if not rsp:
                continue
            if rsp[0] == 0x1B:
                out.append(parse(rsp[3:]))
            else:
                out.append({"event": rsp.hex()})
    finally:
        s.close()
    if not out:
        return "silence"
    return " | ".join(
        f"payload={o['payload']} tail={o.get('tail','')}" if "payload" in o
        else f"event={o.get('event', o.get('raw', '?'))}" for o in out)


def payloads(ssid, pwd, sig, key):
    """The layouts the firmware's field list implies."""
    ssid_lp, pwd_lp, sig_lp, key_lp = lp(ssid), lp(pwd), lp(sig), lp(key)
    tz = be16((-180) & 0xFFFF)
    digest = hashlib.sha256(key.encode()).digest()
    bind_id = struct.unpack("<I", digest[:4])[0]

    yield ("sanity: one byte 0x00", b"\x00")

    six = b"\x00\x01" + ssid_lp + pwd_lp + sig_lp + tz
    yield ("six: cmd ssid pwd url tz", six)

    reg = struct.pack("!I", struct.unpack(">I", digest[:4])[0])
    yield ("six+regCode: cmd ssid pwd url tz reg", six + reg)

    yield ("nine: cmd ssid pwd url p1 port tz bid pbid",
           b"\x00\x01" + ssid_lp + pwd_lp + sig_lp + key_lp + be16(0) + tz +
           struct.pack("<I", bind_id) + struct.pack("<I", bind_id))

    yield ("nine, p1 empty, port 16-bit",
           b"\x00\x01" + ssid_lp + pwd_lp + sig_lp + lp("") + be16(0) + tz +
           struct.pack("<I", bind_id) + struct.pack("<I", bind_id))

    yield ("five: cmd ssid pwd url tz (key instead of tz)",
           b"\x00\x01" + ssid_lp + pwd_lp + sig_lp + key_lp)

    yield ("nine, bind ids as strings",
           b"\x00\x01" + ssid_lp + pwd_lp + sig_lp + key_lp + be16(0) + tz +
           key_lp + key_lp)


def main():
    mac = sys.argv[1]
    sig = sys.argv[2] if len(sys.argv) > 2 else "192.168.68.114:8882"
    ssid = sys.argv[3] if len(sys.argv) > 3 else None
    pwd = sys.argv[4] if len(sys.argv) > 4 else None
    if ssid is None:
        env = {}
        for line in open("/home/mauriciobc/Documentos/Code/ziot-rtp-bridge/.env"):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v
        ssid, pwd = env.get("WIFI_SSID", ""), env.get("WIFI_PWD", "")
    key = "keyyfg5y34yqxqgd"

    for label, payload in payloads(ssid, pwd, sig, key):
        for tag, pkt in (("len=5+dlen", frame(payload)),
                         ("len=5+dlen,end 0d", frame(payload, end=b"\x0d")),
                         ("len counts end", frame(payload, end=b"\x0d",
                                                  len_includes_end=True))):
            print(f"{label} [{tag}]: {probe(mac, label, pkt)}", flush=True)
            time.sleep(0.4)
    return 0


if __name__ == "__main__":
    sys.exit(main())

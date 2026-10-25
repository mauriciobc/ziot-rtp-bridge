#!/usr/bin/env python3
"""Record a whole vendor signalling session, answer by answer.

The camera's protocol is a conversation: it registers, then asks for the
clock, for the video, for its configuration. The vendor's answer to each
step was captured one at a time, but the camera may expect them in one
particular order or state. This records a *complete* session -- the
camera's messages and the vendor's answers, in order -- by playing the
camera's part against the real server.

Usage: capture_vendor_session.py [outfile] [seconds]
"""
import json
import socket
import struct
import sys
import time

VENDOR = ("47.97.71.82", 8882)
UID = b"140979857781"


def xor(data):
    x = 0
    for b in data:
        x ^= b
    return x


def frame(cmd, seq, uid, body):
    head = (b"\x86\x86" + struct.pack("!HHH", cmd, len(uid) + len(body), seq)
            + struct.pack("!H", len(uid)) + uid)
    return head + body + bytes([xor(head + body)]) + b"\x0d"


def decode(raw):
    """(cmd, seq, uid, body) of a frame, or None."""
    if len(raw) < 10 or raw[:2] != b"\x86\x86":
        return None
    cmd, length, seq, uid_len = struct.unpack("!HHHH", raw[2:10])
    if uid_len > 64 or length > 4096:
        return None
    body = raw[10 + uid_len:10 + uid_len + length]
    return cmd, seq, raw[10:10 + uid_len], body


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/vendor_session.json"
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0
    s = socket.create_connection(VENDOR, timeout=10)
    s.settimeout(6)
    log = []
    seq = 1
    # The camera's registration.
    reg = frame(1, seq, UID, b"TXW817_A_V1.0.12.32".ljust(32, b"\x00"))
    s.sendall(reg)
    log.append({"dir": "camera->vendor", "cmd": 1, "seq": seq,
                "hex": reg.hex()})
    end = time.time() + seconds
    while time.time() < end:
        try:
            raw = s.recv(512)
        except socket.timeout:
            break
        if not raw:
            break
        got = decode(raw)
        if got:
            cmd, rseq, uid, body = got
            log.append({"dir": "vendor->camera", "cmd": cmd, "seq": rseq,
                        "body": body.hex(), "hex": raw.hex()})
            # Answer the way the camera would expect, and keep going.
            reply = frame(cmd | 0x8000, rseq, uid, b"\x00\x03\x00\x02\x00")
            s.sendall(reply)
            log.append({"dir": "camera->vendor", "cmd": cmd | 0x8000,
                        "seq": rseq, "hex": reply.hex()})
    s.close()
    with open(out_path, "w") as fh:
        json.dump(log, fh, indent=1)
    print(f"recorded {len(log)} messages -> {out_path}")
    for entry in log:
        body = entry.get("body", "")
        print(f"  {entry['dir']:16} cmd=0x{entry['cmd']:04X} seq={entry['seq']}"
              f" {('body=' + body[:40]) if body else ''}")


if __name__ == "__main__":
    main()

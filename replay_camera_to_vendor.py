#!/usr/bin/env python3
"""Replay a camera's real frame sequence against the vendor, recording it.

The camera's conversation was captured from the broker's log. Replaying
it against the real signalling server gives the vendor's answers in the
order it would give them, including any message the vendor *pushes* --
which is the part that starts the media.

Usage: replay_camera_to_vendor.py [frames-from-log] [outfile]
"""
import collections
import json
import re
import socket
import struct
import sys
import time

VENDOR = ("47.97.71.82", 8882)


def xor(data):
    x = 0
    for b in data:
        x ^= b
    return x


def decode(raw):
    if len(raw) < 10 or raw[:2] != b"\x86\x86":
        return None
    cmd, length, seq, uid_len = struct.unpack("!HHHH", raw[2:10])
    if uid_len > 64 or length > 4096:
        return None
    return (cmd, seq, raw[10:10 + uid_len],
            raw[10 + uid_len:10 + uid_len + length])


def camera_frames(log_path, limit=40):
    """The camera's frames, in order, from the broker's log."""
    rows = []
    for line in open(log_path):
        m = re.search(r"sig\s+(192\.168\.68\.10\d):(\d+)\s+(\d+)B\s+.*"
                      r"\[([0-9a-f]+)\]\s*$", line)
        if not m or "decoded" in line or "sig-ack" in line or "sig-tag" in line:
            continue
        data = bytes.fromhex(m.group(4))
        for off in range(len(data) - 9):
            if data[off:off + 2] == b"\x86\x86":
                got = decode(data[off:])
                if got and got[0] not in (0x8000, 0x8001, 0x8021, 0x8601):
                    rows.append(got)
                break
    return rows[:limit]


def main():
    log_path = sys.argv[1] if len(sys.argv) > 1 else \
        "/home/mauriciobc/ziot-broker.log"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/vendor_replay.json"
    frames = camera_frames(log_path)
    if not frames:
        print("no camera frames in the log")
        return 1
    print(f"replaying {len(frames)} camera frames")
    s = socket.create_connection(VENDOR, timeout=10)
    s.settimeout(4)
    session = []
    for cmd, seq, uid, body in frames:
        raw = (b"\x86\x86" + struct.pack("!HHH", cmd, len(uid) + len(body), seq)
               + struct.pack("!H", len(uid)) + uid
               + body + bytes([0]))
        # rebuild with the right checksum
        head = (b"\x86\x86" + struct.pack("!HHH", cmd, len(uid) + len(body), seq)
                + struct.pack("!H", len(uid)) + uid)
        raw = head + body + bytes([xor(head + body)]) + b"\x0d"
        s.sendall(raw)
        session.append({"dir": "camera->vendor", "cmd": cmd, "seq": seq,
                        "uid": uid.decode("latin-1"),
                        "body": body.hex()})
        # Collect whatever the vendor says about it.
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                answer = s.recv(512)
            except socket.timeout:
                break
            if not answer:
                break
            got = decode(answer)
            if got:
                acmd, aseq, auid, abody = got
                session.append({"dir": "vendor->camera", "cmd": acmd,
                                "seq": aseq, "body": abody.hex(),
                                "hex": answer.hex()})
                print(f"  cmd 0x{cmd:04X} -> vendor cmd=0x{acmd:04X} "
                      f"seq={aseq} body={abody.hex()}")
            else:
                session.append({"dir": "vendor->camera", "raw":
                                answer.hex()})
    s.close()
    with open(out_path, "w") as fh:
        json.dump(session, fh, indent=1)
    print(f"\n{len(session)} messages -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

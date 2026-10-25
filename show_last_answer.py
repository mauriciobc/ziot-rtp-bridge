#!/usr/bin/env python3
"""Show the broker's last registration answer, decoded.

The camera is told where to send its media in the registration answer
(what the vendor's own answer carries). This prints the decoded fields
of the last one, so it is obvious what the camera was pointed at.
"""
import re
import struct
import sys

LOG = sys.argv[1] if len(sys.argv) > 1 else "/home/mauriciobc/ziot-broker.log"
last = None
for line in open(LOG):
    m = re.search(r"vendor-answer ([0-9a-f]{40,})", line)
    if not m:
        continue
    raw = bytes.fromhex(m.group(1))
    if len(raw) < 100:
        continue          # the registration answer is 118 bytes
    cmd, length, seq, uid_len = struct.unpack("!HHHH", raw[2:10])
    if cmd != 0x8000:
        continue
    last = (line[:8], raw)
if not last:
    print("no registration answer in the log")
    sys.exit(1)
stamp, raw = last
body = raw[10 + uid_len:-2]
print(f"{stamp}  {len(raw)} bytes")
print("  uid        :", raw[10:10 + uid_len].decode("latin-1"))
print("  relay ip   :", body[2:22].rstrip(b"\x00").decode())
print("  relay ports:", struct.unpack("!HH", body[22:26]))
print("  stun host  :", body[26:58].rstrip(b"\x00").decode(),
      struct.unpack("!H", body[58:60])[0])
print("  sig ip     :", body[60:92].rstrip(b"\x00").decode(),
      struct.unpack("!H", body[92:94])[0])

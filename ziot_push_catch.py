#!/usr/bin/env python3
"""Watch the wiretap log and catch the vendor's live-view push.

`ziot_wiretap.py` forwards the camera's signalling to the real vendor and
logs every frame decoded. During idle the vendor sends only its four
handshake answers (0x8000 register, 0x8021 clock, 0x8001 ack, 0x8601
config). The one frame we are after -- the *push* the vendor sends when a
viewer presses live view, carrying the address the camera must stream to --
looks like none of those: it is either an unseen command id, or a known one
whose body suddenly carries a new IP or port (the viewer's, or a relay's).

This tails the wiretap log, learns the idle baseline (the IPs, ports and
(cmd,body) shapes seen while nothing is streaming), and then prints a loud,
fully decoded banner the instant a vendor->cam frame breaks that baseline.
It also flags any cam->vendor command outside the camera's usual five, since
a camera told to stream may answer with a new one.

Run it next to the wiretap, arm it, wait for "ARMED", then press live view
in the app. Standard library only.

Usage:
    python3 ziot_push_catch.py [--log ~/ziot-wiretap.log] [--from-start]
    python3 ziot_push_catch.py --analyze ~/ziot-wiretap.log   # scan an existing log, no tail
"""
import argparse
import os
import re
import sys
import time

# The frames the vendor sends while nothing is streaming. Anything else from
# the vendor is a candidate push.
KNOWN_VENDOR_CMDS = {"0x8000", "0x8001", "0x8021", "0x8601"}
# The commands the camera sends on its own during the handshake.
KNOWN_CAM_CMDS = {"0x0001", "0x0002", "0x0004", "0x0021", "0x1601"}
# 0x8021 is the server clock: its body is a timestamp that changes every
# frame, so never treat its body as novel.
VOLATILE_CMDS = {"0x8021"}

LINE_RE = re.compile(
    r"^(?P<ts>\d\d:\d\d:\d\d)\s+(?P<dir>\S+)\s+\S+\s+\d+B\s+(?P<rest>.*)$"
)
HEX_RE = re.compile(r"\[([0-9a-fA-F]{8,})\]\s*$")
CMD_RE = re.compile(r'"cmd":\s*"(0x[0-9a-fA-F]{4})"')
IPV4_RE = re.compile(rb"(?<!\d)((?:\d{1,3}\.){3}\d{1,3})(?!\d)")


def decode_body(frame: bytes):
    """Split a 86 86 framed PDU into header fields + body, tolerant of junk."""
    if len(frame) < 10 or frame[:2] != b"\x86\x86":
        return None
    cmd = frame[2] << 8 | frame[3]
    ln = frame[4] << 8 | frame[5]
    seq = frame[6] << 8 | frame[7]
    uidlen = frame[8] << 8 | frame[9]
    uid = frame[10:10 + uidlen]
    body = frame[10 + uidlen:]
    # strip trailing xor + 0x0d if present
    if body and body[-1:] == b"\x0d":
        body = body[:-1]
    if body:
        body = body[:-1]  # xor byte
    return {"cmd": cmd, "len": ln, "seq": seq, "uid": uid, "body": body}


def ascii_ips(b: bytes):
    return {m.group(1).decode() for m in IPV4_RE.finditer(b)
            if all(0 <= int(p) <= 255 for p in m.group(1).split(b"."))}


def u16s(b: bytes):
    """Every big-endian u16 in the body that looks like a plausible port."""
    out = []
    for i in range(0, len(b) - 1):
        v = b[i] << 8 | b[i + 1]
        if 1 <= v <= 65535:
            out.append((i, v))
    return out


def hexdump(b: bytes) -> str:
    lines = []
    for off in range(0, len(b), 16):
        chunk = b[off:off + 16]
        hx = " ".join(f"{x:02x}" for x in chunk)
        asc = "".join(chr(x) if 32 <= x < 127 else "." for x in chunk)
        lines.append(f"    {off:03x}  {hx:<47}  {asc}")
    return "\n".join(lines)


def banner(title: str, line: str, frame: bytes, info: dict, note: str):
    bar = "=" * 72
    print(f"\n{bar}\n*** {title} ***\n{bar}")
    print(f"  {line}")
    if note:
        print(f"  WHY: {note}")
    if info:
        print(f"  cmd=0x{info['cmd']:04x} len={info['len']} seq={info['seq']} "
              f"uid={info['uid'].decode('latin-1', 'replace')}")
        body = info["body"]
        print(f"  body ({len(body)}B):")
        print(hexdump(body))
        ips = ascii_ips(body)
        if ips:
            print(f"  ASCII IPv4 in body: {sorted(ips)}")
        ports = [f"@{o}:{v}" for o, v in u16s(body)
                 if v in (554, 10000, 8554, 13478, 8882) or v > 1024]
        if ports:
            print(f"  plausible big-endian ports: {', '.join(ports[:20])}")
    print(f"{bar}\n")
    sys.stdout.flush()


def process(line: str, state: dict, armed: bool):
    m = LINE_RE.match(line)
    if not m:
        return
    direction = m.group("dir")
    rest = m.group("rest")
    hm = HEX_RE.search(rest)
    if not hm:
        return
    try:
        frame = bytes.fromhex(hm.group(1))
    except ValueError:
        return
    cm = CMD_RE.search(rest)
    cmd_s = cm.group(1).lower() if cm else None
    info = decode_body(frame)

    if direction == "vendor->cam":
        novel = []
        if cmd_s and cmd_s not in KNOWN_VENDOR_CMDS:
            novel.append(f"unknown vendor command {cmd_s}")
        if info and (cmd_s not in VOLATILE_CMDS):
            new_ips = ascii_ips(info["body"]) - state["ips"]
            if new_ips:
                novel.append(f"new IP(s) in body: {sorted(new_ips)}")
            shape = (info["cmd"], bytes(info["body"]))
            if armed and shape not in state["shapes"] and cmd_s in KNOWN_VENDOR_CMDS:
                novel.append(f"new body for {cmd_s}")
        if novel and armed:
            banner("PUSH CANDIDATE (vendor -> camera)", line.rstrip(),
                   frame, info, "; ".join(novel))
        # learn baseline
        if info and cmd_s not in VOLATILE_CMDS:
            state["ips"] |= ascii_ips(info["body"])
            state["shapes"].add((info["cmd"], bytes(info["body"])))
    elif direction == "cam->vendor":
        if armed and cmd_s and cmd_s not in KNOWN_CAM_CMDS:
            banner("NEW CAMERA COMMAND (camera -> vendor)", line.rstrip(),
                   frame, info, f"camera sent {cmd_s}, outside its usual five")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=os.path.expanduser("~/ziot-wiretap.log"),
                    help="wiretap log to watch (default ~/ziot-wiretap.log)")
    ap.add_argument("--arm-after", type=float, default=8.0,
                    help="seconds of baseline-learning before arming (default 8)")
    ap.add_argument("--from-start", action="store_true",
                    help="read the whole file first (baseline from history) then tail")
    ap.add_argument("--analyze", metavar="LOGFILE",
                    help="scan an existing log once and exit (no tailing)")
    args = ap.parse_args()

    state = {"ips": set(), "shapes": set()}

    if args.analyze:
        with open(args.analyze, "r", errors="replace") as f:
            lines = f.readlines()
        # first pass: baseline from everything, second: report novelties
        for ln in lines:
            process(ln, state, armed=False)
        print(f"[analyze] baseline learned: {len(state['ips'])} IPs, "
              f"{len(state['shapes'])} vendor body shapes")
        print(f"[analyze] IPs seen: {sorted(state['ips'])}")
        for ln in lines:
            process(ln, state, armed=True)
        print("[analyze] done.")
        return 0

    path = args.log
    while not os.path.exists(path):
        print(f"[*] waiting for {path} to appear ...")
        time.sleep(1)
    f = open(path, "r", errors="replace")
    if args.from_start:
        for ln in f:
            process(ln, state, armed=False)
        print(f"[*] baseline from history: IPs={sorted(state['ips'])}")
    else:
        f.seek(0, os.SEEK_END)

    t0 = time.time()
    armed = False
    print(f"[*] watching {path} -- learning baseline for {args.arm_after:g}s, "
          f"then ARMED. Press live view AFTER you see ARMED.")
    while True:
        where = f.tell()
        ln = f.readline()
        if not ln:
            if not armed and time.time() - t0 >= args.arm_after:
                armed = True
                print(f"\n>>> ARMED <<<  baseline: IPs={sorted(state['ips'])}, "
                      f"{len(state['shapes'])} vendor shapes. Press LIVE VIEW now.\n")
            time.sleep(0.3)
            f.seek(where)
            continue
        process(ln, state, armed)


if __name__ == "__main__":
    sys.exit(main())

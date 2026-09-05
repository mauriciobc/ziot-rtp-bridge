#!/usr/bin/env python3
"""
ziot_offline_probe.py -- find ZIOT cameras on the LAN without the cloud
========================================================================
Sends the vendor app's plaintext UDP hello to a sweep of ports on a target
IP and reports anything that answers with RTP. No account, no token, no
network beyond the LAN.

Usage:
    python3 ziot_offline_probe.py 192.168.18.75
    python3 ziot_offline_probe.py 192.168.18.29 --ports 1024-65535
    python3 ziot_offline_probe.py 192.168.18.75 --ports 52901 --uid 141030191094

A hit looks like:
    RESPONDER 192.168.18.75:52901  12 pkts  ssrc=0x30191094 pt=26,0

On TXW817 firmware, unsolicited hellos are ignored — a silent sweep is
normal, not a probe bug. Those cameras still need a token in the bridge
config so `--offline` can send notify/keepalive; put the LAN IP only:

    {"token": "eyJ…", "offline_endpoints": {"141030191094": "192.168.18.75"}}

If a HIT does appear, that camera will punch without a token. Re-probe
after every reboot: the listen port is ephemeral.

Exit code is 0 when at least one responder (or the expected UID's SSRC)
is seen, 1 when nothing answers.
"""
import argparse
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ziot_rtp_bridge import PUNCH, HEART, uid_ssrc, is_rtp_media

DEFAULT_PORTS = "32768-61000"  # Linux ephemeral range; camera ports land here


def parse_ports(spec: str) -> list:
    ports = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            ports.update(range(int(lo), int(hi) + 1))
        else:
            ports.add(int(part))
    return sorted(ports)


def describe(payload: bytes) -> str:
    pt = payload[1] & 0x7F
    ssrc = struct.unpack("!I", payload[8:12])[0] if len(payload) >= 12 else 0
    if not is_rtp_media(payload):
        if len(payload) < 12:
            return f"{len(payload)}B non-RTP"
        return f"non-RTP (v{(payload[0] >> 6)} pt={pt} ssrc=0x{ssrc:08x})"
    return f"ssrc=0x{ssrc:08x} pt={pt}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("target", help="camera LAN IP to sweep")
    ap.add_argument("--ports", default=DEFAULT_PORTS,
                    help=f"ports to hello (default {DEFAULT_PORTS})")
    ap.add_argument("--uid", default=None,
                    help="camera UID: only its SSRC counts as a hit")
    ap.add_argument("--rate", type=float, default=2000,
                    help="hellos per second (default 2000)")
    ap.add_argument("--listen", type=float, default=5.0,
                    help="seconds to keep listening after the sweep")
    ap.add_argument("--bind-ip", default=None,
                    help="local IP to send from (default: route to target)")
    args = ap.parse_args()

    try:
        target_ip = socket.gethostbyname(args.target)
    except OSError as e:
        print(f"cannot resolve {args.target}: {e}", file=sys.stderr)
        return 1

    try:
        ports = parse_ports(args.ports)
    except ValueError:
        ap.error(f"bad --ports spec {args.ports!r} -- "
                 "use N, N-M, comma-separated, e.g. 32768-61000")
    if not ports:
        print("no ports to scan", file=sys.stderr)
        return 1
    want_ssrc = uid_ssrc(args.uid) if args.uid else None
    if args.uid and want_ssrc is None:
        print(f"uid {args.uid!r} carries no usable SSRC; "
              f"accepting any RTP responder", file=sys.stderr)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if args.bind_ip:
        sock.bind((args.bind_ip, 0))
    sock.settimeout(0.5)
    stop = threading.Event()
    seen: dict = {}
    lock = threading.Lock()

    def collect():
        while not stop.is_set():
            try:
                data, source = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            with lock:
                e = seen.setdefault(source, {"n": 0, "sample": data})
                e["n"] += 1

    listener = threading.Thread(target=collect, daemon=True)
    listener.start()

    gap = 1.0 / max(args.rate, 1)
    hellos = (PUNCH, HEART)
    t0 = time.monotonic()
    try:
        total = len(ports)
        for i, p in enumerate(ports):
            for h in hellos:
                try:
                    sock.sendto(h, (target_ip, p))
                except OSError:
                    pass
            if gap > 0:
                time.sleep(gap)
            if (i + 1) % 2000 == 0:
                print(f"... {i + 1}/{total} ports", flush=True)
    except KeyboardInterrupt:
        pass
    sweep_s = time.monotonic() - t0
    print(f"swept {len(ports)} ports on {args.target} ({target_ip}) "
          f"in {sweep_s:.1f}s; listening {args.listen:.0f}s more...",
          flush=True)
    time.sleep(args.listen)
    stop.set()
    listener.join(timeout=2)
    sock.close()

    hits = 0
    with lock:
        items = sorted(seen.items(),
                       key=lambda kv: kv[1]["n"], reverse=True)
    for (ip, port), e in items:
        if ip != target_ip:
            continue
        info = describe(e["sample"])
        wanted = is_rtp_media(e["sample"], want_ssrc)
        mark = "HIT " if wanted else "other "
        print(f"{mark}RESPONDER {ip}:{port}  {e['n']} pkts  {info}")
        hits += wanted
    with lock:
        foreign = [(s, e["n"]) for s, e in seen.items() if s[0] != target_ip]
    for (ip, port), n in foreign:
        print(f"note: {n} pkts also arrived from {ip}:{port} "
              f"(not the target; another camera answering?)")

    if hits:
        print(f"{hits} responder(s) - put ip:port in offline_endpoints "
              f"and run the bridge with --offline")
        return 0
    print("nothing answered. The camera either is not at this IP, "
          "needs a cloud notify to wake, or drops unsolicited hellos. "
          "If the sweep crawled, the target is probably off the LAN: "
          "no ARP reply stalls every hello — check `ip neigh show` first.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

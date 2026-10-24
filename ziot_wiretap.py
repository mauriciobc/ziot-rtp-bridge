#!/usr/bin/env python3
"""Wiretap the camera's signalling so the real vendor's push is captured.

The local broker (`ziot_fake_cloud.py`) answers the camera with the
vendor's own frames pointed at this host, and that gets the camera
through its whole handshake -- register, clock, pullVideo, status,
config -- but the camera then goes silent and never sends a single RTP
datagram. The one thing the broker cannot reproduce is the *push*: the
frame the vendor's signalling server sends, unprompted, when a viewer
presses "live view", telling the camera where to send its media. Its
command id and body are not in the firmware (the code region does not
decode in any Capstone ISA) and 20 guessed shapes -- every body form
under every plausible command id -- were fired at the live camera and
not one produced a datagram.

So the format has to be read off the wire. This script is a transparent
TCP relay: the camera connects to it (via the broker's DNS hijack /
the router's static route, exactly as it reaches the broker today), and
it forwards every byte to the *real* vendor signalling server and back,
logging both directions decoded. When the user presses live view in the
vendor app, the real push travels camera<->vendor straight through here
and lands in the log, byte for byte. No vendor credential is needed:
the camera's own frames carry the uid, and the vendor answers the
camera, not us.

It coexists with the broker's HTTP / STUN / DNS: only the signalling
port (8882) moves out of the broker for the tap. Run the broker with
`--sig 0` so this owns 8882, point `--upstream-host` at a vendor sig IP
that is actually routable from here (the claimed /32s loop back to this
host, so the upstream address must bypass them -- see --help), and
press live view.

Standard library only; no third-party dependencies.

Example
-------
    # broker keeps HTTP/STUN/DNS, drops its own signalling listener:
    sudo systemctl stop ziot-broker.service   # or restart with --sig 0

    # tap listens where the camera already lands, forwards to the real
    # vendor, logs the push when you press live view in the app:
    sudo python3 ziot_wiretap.py \
        --listen 0.0.0.0:8882 \
        --upstream-host 47.97.71.82 --upstream-port 8882 \
        --log /home/mauriciobc/ziot-wiretap.log

The decisive line to look for is a vendor->cam frame with a command id
the broker does not already answer (not 0x8000/0x8021/0x8001/0x8601),
carrying this host's relay/stun address: that is the push.
"""
import argparse
import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path

# Reuse the broker's decoder so a frame reads the same here as in the
# broker's log. It is a pure stdlib function with no import side effects
# (the broker starts nothing until its own __main__). If this script is
# run on its own, away from the broker, the inline fallback below keeps
# it working.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from ziot_fake_cloud import decode_signalling as _decode_signalling
except Exception:                                    # pragma: no cover
    _decode_signalling = None


def _xor(data: bytes) -> int:
    acc = 0
    for b in data:
        acc ^= b
    return acc


def decode_signalling(data: bytes) -> dict:
    """Decode a camera/server signalling frame the way the broker does.

    Prefers the broker's own `decode_signalling` so the two logs agree;
    falls back to this copy when the broker is not importable. The frame
    is `86 86 | cmd(2) | len(2) | seq(2) | uidLen(2) | uid | body | xor
    | 0x0D`, all big-endian (DECOMPILATION_REPORT.md, read live
    2026-10-23).
    """
    if _decode_signalling is not None:
        return _decode_signalling(data)
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


# The commands the broker already answers on its own: a vendor->cam
# frame whose command is NOT one of these is the interesting one -- most
# likely the push.
BROKER_ANSWERS = {"0x8000", "0x8021", "0x8001", "0x8601", "0x8002"}

_log_lock = threading.Lock()
_log_file = None


def log(direction: str, peer, data: bytes, note: str = "") -> None:
    """Print one framed PDU, decoded when it is signalling, plus hex.

    Every line is `HH:MM:SS dir peer len <decoded|ascii> [hex]`, the
    same column shape the broker uses, so a capture reads the same in
    both logs. A vendor->cam frame with a command the broker does not
    answer is flagged, because that is the push the whole exercise is
    for.
    """
    stamp = time.strftime("%H:%M:%S")
    where = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)
    decoded = decode_signalling(data)
    flag = ""
    if direction == "vendor->cam" and decoded.get("cmd") not in BROKER_ANSWERS \
            and "cmd" in decoded:
        flag = "  <<< PUSH? (command the broker does not answer)"
    if "cmd" in decoded:
        summary = "decoded " + json.dumps(decoded)
    else:
        ascii_preview = data[:48].decode("latin-1", "replace")
        ascii_preview = "".join(c if 32 <= ord(c) < 127 else "." for c in
                                ascii_preview)
        summary = ascii_preview
    line = (f"{stamp} {direction:<12} {where:<22} {len(data):>5}B  "
            f"{note}{summary}  [{data.hex()}]{flag}")
    with _log_lock:
        print(line, flush=True)
        if _log_file is not None:
            _log_file.write(line + "\n")
            _log_file.flush()


def _pump(src: socket.socket, dst: socket.socket, direction: str,
          peer, stop: threading.Event) -> None:
    """Copy src->dst, logging each chunk, until either end closes.

    One frame per recv is the common case (the camera and the vendor
    both write a whole PDU at a time), so a chunk is logged as it comes;
    a coalesced pair still lands in the log verbatim in the hex column.
    """
    try:
        while not stop.is_set():
            try:
                data = src.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            log(direction, peer, data)
            try:
                dst.sendall(data)
            except OSError:
                break
    finally:
        stop.set()
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _handle_client(cam_sock: socket.socket, cam_addr,
                   upstream_host: str, upstream_port: int) -> None:
    """Bridge one camera connection to the real vendor, both ways.

    The camera opened the connection (it reached us by DNS hijack / the
    router route, same as it reaches the broker); we open the matching
    one to the real vendor and let the two talk. The camera's own frames
    carry the uid, so the vendor answers it with no credential from us.
    """
    try:
        up = socket.create_connection((upstream_host, upstream_port),
                                      timeout=10)
    except OSError as e:
        log("tap", cam_addr,
            f"upstream {upstream_host}:{upstream_port} unreachable: {e}"
            .encode())
        try:
            cam_sock.close()
        except OSError:
            pass
        return
    log("tap", cam_addr,
        f"bridged to vendor {upstream_host}:{upstream_port}".encode())
    cam_sock.settimeout(1.0)
    up.settimeout(1.0)
    stop = threading.Event()
    threads = [
        threading.Thread(target=_pump,
                         args=(cam_sock, up, "cam->vendor", cam_addr, stop),
                         daemon=True),
        threading.Thread(target=_pump,
                         args=(up, cam_sock, "vendor->cam", cam_addr, stop),
                         daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for s in (cam_sock, up):
        try:
            s.close()
        except OSError:
            pass
    log("tap", cam_addr, b"connection closed")


def _accept_loop(listener: socket.socket, upstream_host: str,
                 upstream_port: int) -> None:
    while True:
        try:
            cam_sock, cam_addr = listener.accept()
        except OSError:
            return
        threading.Thread(target=_handle_client,
                         args=(cam_sock, cam_addr, upstream_host,
                               upstream_port),
                         daemon=True).start()


def _parse_listen(spec: str) -> tuple:
    """`host:port` or bare `port` -> (host, port). Bare port binds all."""
    if ":" in spec:
        host, _, port = spec.rpartition(":")
        return (host or "0.0.0.0", int(port))
    return ("0.0.0.0", int(spec))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Upstream reachability: this host claims the vendor IPs as /32s "
            "so the router can point them here, which means a plain connect "
            "to the vendor sig IP would loop back to this host. For the tap "
            "to reach the REAL vendor, give --upstream-host an address that "
            "is still routed out (temporarily drop the matching /32 with "
            "`sudo ip addr del <ip>/32 dev wlan0`, or route it via the "
            "gateway), and make sure the broker is not also on the sig port "
            "(`--sig 0`)."))
    ap.add_argument("--listen", action="append", default=[], metavar="HOST:PORT",
                    help="where the camera lands; repeatable for several "
                         "claimed vendor IPs (default: 0.0.0.0:8882)")
    ap.add_argument("--upstream-host", required=True,
                    help="the real vendor signalling IP to forward to "
                         "(e.g. 47.97.71.82 or 47.110.77.193)")
    ap.add_argument("--upstream-port", type=int, default=8882,
                    help="the real vendor signalling port (default 8882)")
    ap.add_argument("--log", default=None,
                    help="also append every logged line to this file")
    args = ap.parse_args(argv)

    listens = [_parse_listen(s) for s in (args.listen or ["0.0.0.0:8882"])]

    global _log_file
    if args.log:
        _log_file = open(args.log, "a", buffering=1)

    listeners = []
    for host, port in listens:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((host, port))
        except OSError as e:
            print(f"cannot bind {host}:{port}: {e} -- ports below 1024 need "
                  "root, and the broker must not also hold this port "
                  "(run it with --sig 0)", file=sys.stderr)
            continue
        srv.listen(16)
        listeners.append(srv)
        threading.Thread(target=_accept_loop,
                         args=(srv, args.upstream_host, args.upstream_port),
                         daemon=True).start()
        print(f"wiretap listening on {host}:{port} -> real vendor "
              f"{args.upstream_host}:{args.upstream_port}", flush=True)

    if not listeners:
        print("nothing to listen on", file=sys.stderr)
        return 1
    print("press live view in the vendor app; watch for a 'vendor->cam' "
          "frame flagged PUSH?", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
ziot_fake_cloud.py -- local stand-in for the gps555.net cloud
=============================================================
Phase 1 of the cloud bypass (see DECOMPILATION_REPORT.md §8): a listener
farm that impersonates the vendor cloud's endpoints and LOGS every byte
the camera sends, so the camera→cloud protocol can be recovered from the
camera's own re-registration and keepalive traffic.

Endpoints impersonated (from DECOMPILATION_REPORT.md §4.1):

    ipc.gps555.net:80      HTTP API   (plaintext; login, notify, stun-addr)
    sig.gps555.net:8882    signalling (role unknown -- the broker)
    stun.gps555.net:13478  STUN-like  (UDP)
    ws.gps555.net:7080     status websocket (HTTP upgrade expected)

Point the router's DNS for *.gps555.net at this host and the camera's
own traffic lands here. Ports below 1024 need privileges: run under
sudo, or once: sysctl -w net.ipv4.ip_unprivileged_port_start=0

Nothing here pretends to work yet -- phase 1 answers "what does the
camera send", not "how do we answer". Responses are deliberate
protocol-shaped errors that keep the camera retrying.

Usage:
    python3 ziot_fake_cloud.py [--log FILE] [--no-http] [--no-sig]
                               [--no-stun] [--no-ws]
"""
import argparse
import socket
import socketserver
import sys
import threading
import time

VENDOR_DOMAINS = ("ipc.gps555.net", "sig.gps555.net", "stun.gps555.net",
                  "stunaddr.gps555.net", "ws.gps555.net", "gps555.net")

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
    """TCP: log each PDU (read until 2s idle or close) and answer nothing.

    The camera side of the protocol is unknown; a silent server that
    keeps the connection open logs the most. A refused connection logs
    nothing, which is the failure mode this must avoid.
    """

    def handle(self):
        self.request.settimeout(2.0)
        while True:
            try:
                data = self.request.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            log(self.server.kind, self.request.getpeername(), data)
            # Phase 2 will answer per protocol; for now acknowledge TCP
            # streams with nothing and let the client time out.


class StreamServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, kind):
        self.kind = kind
        super().__init__(addr, StreamHandler)


class StunHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        log("stun", self.client_address, data)
        # Silence: the bridge's own STUN checks reject answers with a
        # lower seqNo, and an unsolicited reply would poison nothing we
        # run locally. Phase 2 decides what a valid answer looks like.


class StunServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr):
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
    """Minimal A-record answer echoing the question section."""
    name_end = 12
    while query[name_end]:
        name_end += 1 + query[name_end]
    name_end += 1
    question = query[12:name_end]
    header = query[:2] + b"\x81\x80" + b"\x00\x01\x00\x01\x00\x00\x00\x00"
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


def serve(kind: str, addr: tuple) -> socketserver.BaseServer:
    srv = StunServer(addr) if kind == "stun" else StreamServer(addr, kind)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"listening {kind} on {addr[0]}:{addr[1]}", flush=True)
    return srv


def _wait_forever() -> None:
    """Sleep until Ctrl-C. Module-level so tests can stub it."""
    threading.Event().wait()


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
    args = ap.parse_args()

    global _log_file
    if args.log:
        _log_file = open(args.log, "a", buffering=1)

    my_ip = args.bind_ip
    if args.dns:
        if my_ip is None:
            # The address the default route uses: what LAN peers can reach.
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("10.255.255.255", 53))
                my_ip = probe.getsockname()[0]
            except OSError:
                my_ip = "127.0.0.1"
            finally:
                probe.close()
        if args.upstream_dns:
            upstream = args.upstream_dns
        else:
            upstream = None
            for line in open("/etc/resolv.conf"):
                if line.startswith("nameserver"):
                    upstream = line.split()[1]
                    break
            upstream = upstream or "1.1.1.1"

    servers = []
    for kind, port in (("http", args.http), ("sig", args.sig),
                       ("ws", args.ws), ("stun", args.stun)):
        if port:
            try:
                servers.append(serve(kind, (args.bind, port)))
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

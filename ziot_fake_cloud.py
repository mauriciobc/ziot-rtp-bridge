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
import subprocess
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


def serve(kind: str, addr: tuple) -> socketserver.BaseServer:
    srv = StunServer(addr) if kind == "stun" else StreamServer(addr, kind)
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

    Drives btgatt-client over stdin (same tool the GATT enumeration used):
    connect, subscribe notify on 0x000a, then write the frame in 20-byte
    chunks to 0x0008 -- the order the vendor app uses (it subscribes after
    writing, we subscribe first so the reply cannot be missed).
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
    args.ble_mac = ble_mac
    sig_addr = args.sig_addr or f"{_detect_lan_ip()}:8882"
    tz = args.tz if args.tz is not None else _local_tz_minutes()
    frame = wifi_connect_frame(args.ssid, args.pwd, sig_addr, tz)
    chunks = chunk20(frame)
    print(f"frame: {frame.hex()}", flush=True)
    print(f"{len(chunks)} chunk(s) -> char 0x0008 on {args.ble_mac}, "
          f"sig-addr {sig_addr}, tz {tz} min", flush=True)

    proc = subprocess.Popen(["btgatt-client", "-t", "public", "-d",
                             args.ble_mac],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        assert proc.stdin
        proc.stdin.write("register-notify 0x000a\n")
        proc.stdin.flush()
        time.sleep(2)
        for c in chunks:
            proc.stdin.write("write-value 0x0008 %s\n" % c.hex())
            proc.stdin.flush()
            time.sleep(0.4)
        # The camera answers in exactly two notifications; give them time.
        end = time.time() + 15
        while time.time() < end and proc.poll() is None:
            line = proc.stdout.readline()
            if line:
                print(line.rstrip(), flush=True)
                if "uid" in line.lower() or "Notification" in line:
                    # keep draining briefly for the second notification
                    for _ in range(6):
                        line = proc.stdout.readline()
                        if line:
                            print(line.rstrip(), flush=True)
                    break
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("if the camera joined the LAN it will appear in `ip neigh` and "
          "register with the broker on :8882", flush=True)
    return 0


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
    ap.add_argument("--provision", action="store_true",
                    help="skip the listeners: build the BLE wifiConnect "
                         "frame, write it to the camera in 20-byte chunks "
                         "via bluetoothctl/btgatt-client, and print its "
                         "reply. Requires --ssid/--pwd/--ble-mac; "
                         "--sig-addr defaults to this host's LAN address")
    ap.add_argument("--ssid", default=None)
    ap.add_argument("--pwd", default=None)
    ap.add_argument("--ble-mac", default=None, dest="ble_mac",
                    help="camera's BLE MAC (the advertising address)")
    ap.add_argument("--tz", type=int, default=None,
                    help="timezone offset in minutes (default: local)")
    ap.add_argument("--sig-addr", default=None, dest="sig_addr",
                    help="signalling server for the camera to register with "
                         "(default: <LAN IP>:8882 -- the local broker)")
    args = ap.parse_args()

    if args.provision:
        return ble_provision(args)

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

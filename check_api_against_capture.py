#!/usr/bin/env python3
"""Check the broker's API answers against the vendor's real ones.

The app capture (PCAPdroid_29_ago._15_13_27.pcap) holds the phone's
session with the vendor: every request it sent to ipc.gps555.net:80 and
every answer the vendor gave. This replays each captured request against
the local broker and compares the answers field by field, so the broker
cannot drift from the vendor's shapes.

Usage: check_api_against_capture.py <pcap> [broker-base]
"""
import json
import struct
import sys
import urllib.error
import urllib.request

VENDOR_IP = "120.26.121.165"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the redirect: it is part of the vendor's contract."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def pcap_streams(path):
    """Yield (src, dst, sport, dport, seq, payload) for the TCP port 80 flow."""
    data = open(path, "rb").read()
    if data[:4] == b"\x0a\x0d\x0d\x0a":
        return []
    endian = "<" if data[:4] in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
    _, _, _, _, _, link = struct.unpack(endian + "HHiIII", data[4:24])
    out = []
    off = 24
    while off + 16 <= len(data):
        _, _, incl, _ = struct.unpack(endian + "IIII", data[off:off + 16])
        off += 16
        pkt = data[off:off + incl]
        off += incl
        if link != 101 or len(pkt) < 20:
            continue
        # The capture is a VPN tunnel, two levels deep: an outer IP
        # packet carrying what looks like TCP, whose payload is PPP,
        # which carries the real IP packet with the real TCP inside.
        # Unwrap by looking for the innermost IP header whose protocol
        # is TCP and whose ports are the vendor's.
        payload = b""
        for _ in range(4):
            ip = None
            for idx in range(0, min(80, len(pkt) - 20)):
                if (pkt[idx] >> 4) != 4 or (pkt[idx] & 0xF) != 5:
                    continue
                if pkt[idx + 9] == 6:
                    ip = pkt[idx:]
                    break
            if ip is None:
                break
            ihl = (ip[0] & 0xF) * 4
            if len(ip) < ihl + 20:
                break
            sport, dport = struct.unpack("!HH", ip[ihl:ihl + 4])
            tcp = ip[ihl:]
            # A real TCP header has its data offset in the high nibble
            # of byte 12 (0x5 for a 20-byte header).
            doff = (tcp[12] >> 4) * 4
            if 20 <= doff <= 60 and len(tcp) > doff:
                payload = tcp[doff:]
                src = ".".join(str(b) for b in ip[12:16])
                dst = ".".join(str(b) for b in ip[16:20])
                seq = struct.unpack("!I", tcp[4:8])[0]
                out.append((src, sport, dst, dport, seq, payload))
                break
            # Not a real TCP header: the payload is another tunnel.
            pkt = tcp[20:]
        else:
            continue
    return out


def reassemble(chunks):
    """Put a TCP byte stream back in order, by sequence number."""
    buf = bytearray()
    seen = set()
    for seq, payload in sorted(chunks, key=lambda c: c[0]):
        if seq in seen:
            continue
        seen.add(seq)
        buf.extend(payload)
    return bytes(buf)


def tcp_messages(pkts):
    """Pair each connection's requests with the vendor's answers.

    The capture holds one TCP connection per request (the app opens a
    new one each time), so the streams are grouped by connection and
    reassembled per direction.
    """
    conns = {}
    for src, sport, dst, dport, seq, payload in pkts:
        if VENDOR_IP not in (src, dst):
            continue
        if dport == 80:
            key = (src, sport)
            conns.setdefault(key, ([], []))[0].append((seq, payload))
        elif sport == 80:
            key = (dst, dport)
            conns.setdefault(key, ([], []))[1].append((seq, payload))
    pairs = []
    for key, (req, rsp) in conns.items():
        if req and rsp:
            pairs.append((reassemble(req), reassemble(rsp)))
    return pairs


def main():
    path = sys.argv[1]
    base = sys.argv[2] if len(sys.argv) > 2 else "http://120.26.121.165"
    pkts = pcap_streams(path)
    pairs = tcp_messages(pkts)
    if not pairs:
        print("no vendor HTTP traffic in the capture")
        return 1

    # Split a captured byte stream into HTTP messages.
    def messages(stream):
        buf = stream
        out = []
        while True:
            head_end = buf.find(b"\r\n\r\n")
            if head_end < 0:
                break
            head = buf[:head_end].decode("latin-1", "replace")
            length = 0
            for line in head.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
            total = head_end + 4 + length
            if total > len(buf):
                break
            out.append((head, buf[head_end + 4:total]))
            buf = buf[total:]
        return out

    print(f"captured {len(pairs)} vendor connection(s)")
    seen = set()
    problems = []
    for req_stream, rsp_stream in pairs:
        reqs = messages(req_stream)
        resps = messages(rsp_stream)
        for (head, body), (rhead, rbody) in zip(reqs, resps):
            parts = head.split("\r\n")[0].split()
            if len(parts) < 2:
                continue
            method, target = parts[0], parts[1]
            path_only = target.split("?")[0]
            key = (method, path_only)
            if key in seen:
                continue
            seen.add(key)
            try:
                # No redirect following: the vendor's 301 is part of the
                # contract, and urllib would hide it.
                opener = urllib.request.build_opener(NoRedirect)
                r = opener.open(base + target, timeout=6)
                got = r.read()
                got_status = r.status
                got_head = r.headers
            except urllib.error.HTTPError as e:
                got = e.read()
                got_status = e.code
                got_head = e.headers
            except Exception as e:
                problems.append(f"{method} {path_only}: broker unreachable ({e})")
                continue

            def shape(payload):
                try:
                    obj = json.loads(payload)
                except Exception:
                    return ("not-json", payload[:40])
                if isinstance(obj, dict):
                    return ("json", tuple(sorted(obj.keys())))
                return ("json", type(obj).__name__)

            vendor_shape = shape(rbody)
            broker_shape = shape(got)
            if vendor_shape != broker_shape:
                problems.append(
                    f"{method} {path_only}: vendor {vendor_shape} != "
                    f"broker {broker_shape}")
                print(f"  MISMATCH {method} {path_only}")
                print(f"    vendor: {rbody[:240]!r}")
                print(f"    broker: {got[:240]!r}")
            else:
                extra = ""
                if vendor_shape[0] == "not-json":
                    # A redirect: compare the status and the Location.
                    v_status = rhead.split("\r\n", 1)[0]
                    b_status = f"HTTP/1.1 {got_status} Moved Permanently"
                    v_loc = [l.split(":", 1)[1].strip()
                             for l in rhead.split("\r\n")
                             if l.lower().startswith("location:")]
                    b_loc = [l.strip() for l in got_head.get_all("Location")]
                    if v_status != b_status or v_loc != b_loc:
                        problems.append(
                            f"{method} {path_only}: redirect differs")
                        print(f"  MISMATCH {method} {path_only} redirect")
                        print(f"    vendor: {v_status} {v_loc}")
                        print(f"    broker: {b_status} {b_loc}")
                    else:
                        extra = " (redirect identical)"
                print(f"  ok {method} {path_only}  ({vendor_shape[0]}, "
                      f"{len(rbody)}B vendor / {len(got)}B broker){extra}")

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print("  -", p)
        return 1
    print("\nall captured endpoints answer with the vendor's shapes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

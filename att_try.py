#!/usr/bin/env python3
"""Write the wifiConnect frame to each candidate handle and watch for the
camera's answer.

The camera acknowledges a write to any handle, so an acknowledgement
proves nothing. What identifies the command channel is the camera's
*reaction*: writing the frame there makes it answer with a notification,
and that notification is the reply the app parses.

So each candidate handle gets the real frame, and the one that produces
a notification is the command channel.

Usage: att_try.py <ble-mac> [sig-addr]
"""
import socket
import struct
import sys
import time

sys.path.insert(0, "/home/mauriciobc/Documentos/Code/ziot-rtp-bridge")
import ziot_fake_cloud as z

MAC = sys.argv[1]
SIG = sys.argv[2] if len(sys.argv) > 2 else "192.168.68.114:8882"
CCCD = 0x000B
CANDIDATES = [0x0008, 0x0009, 0x000A, 0x0007, 0x0006, 0x0005, 0x0004,
               0x0003, 0x000C]


def att(sock, opcode, payload=b""):
    sock.send(bytes([opcode]) + payload)


def read_pdu(sock, timeout=1.5):
    sock.settimeout(timeout)
    try:
        return sock.recv(512)
    except socket.timeout:
        return b""


def connect(mac):
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                      socket.BTPROTO_L2CAP)
    s.bind(("00:00:00:00:00:00", 0, 4, 1))
    s.settimeout(4.0)
    s.connect((mac, 0, 4, 1))
    return s


def main():
    env = z._load_env("/home/mauriciobc/Documentos/Code/ziot-rtp-bridge/.env")
    frame = z.wifi_connect_frame(env["WIFI_SSID"], env["WIFI_PWD"], SIG, -180)
    chunks = z.chunk20(frame)
    print(f"frame {len(frame)}B -> {len(chunks)} chunks", flush=True)
    print(f"frame: {frame.hex()}", flush=True)

    for handle in CANDIDATES:
        try:
            s = connect(MAC)
        except OSError as e:
            print(f"handle {handle:#06x}: connect failed ({e})", flush=True)
            time.sleep(1)
            continue
        try:
            att(s, 0x02, struct.pack("<H", 247))
            read_pdu(s, 1.5)
            # Subscribe notifications on every plausible CCCD. The value is
            # two bytes: a write of four is rejected with "invalid
            # attribute value length" (0x0c) -- which is how the CCCD was
            # identified in the first place.
            for cccd in (0x000C, 0x000B, 0x000A, 0x0009):
                att(s, 0x12, struct.pack("<H", cccd) + b"\x01\x00")
                rsp = read_pdu(s, 0.6)
                print(f"  cccd {cccd:#06x}: {rsp.hex() if rsp else 'silent'}",
                      flush=True)
            for i, c in enumerate(chunks):
                att(s, 0x12, struct.pack("<H", handle) + c)
                rsp = read_pdu(s, 1.5)
                if rsp and rsp[0] == 0x1B:
                    payload = rsp[3:]
                    print(f"handle {handle:#06x} chunk {i}: "
                          f"NOTIFY {len(payload)}B {payload.hex()}", flush=True)
                    try:
                        print("  REPLY:", z.parse_provision_response(payload),
                              flush=True)
                    except (ValueError, IndexError) as e:
                        print("  (partial:", e, ")", flush=True)
                elif rsp and rsp[0] == 0x01:
                    print(f"handle {handle:#06x} chunk {i}: error "
                          f"{rsp[2]:#04x}" if len(rsp) > 2 else "error",
                          flush=True)
                elif rsp:
                    print(f"handle {handle:#06x} chunk {i}: {rsp.hex()}",
                          flush=True)
                else:
                    print(f"handle {handle:#06x} chunk {i}: silent",
                          flush=True)
            # Give the camera a moment to answer.
            end = time.time() + 6
            while time.time() < end:
                rsp = read_pdu(s, 0.8)
                if not rsp:
                    continue
                if rsp[0] == 0x1B:
                    payload = rsp[3:]
                    print(f"handle {handle:#06x}: NOTIFY {len(payload)}B "
                          f"{payload.hex()}", flush=True)
                    try:
                        print("  REPLY:", z.parse_provision_response(payload),
                              flush=True)
                    except (ValueError, IndexError) as e:
                        print("  (partial:", e, ")", flush=True)
                else:
                    print(f"handle {handle:#06x}: event {rsp.hex()}", flush=True)
        finally:
            s.close()
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())

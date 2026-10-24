#!/usr/bin/env python3
"""Listen to everything the camera pushes, and answer what it asks.

The app's function is called `_handleApRequest` -- the camera asks, the
app answers. So the camera may push a request over the notify
characteristic and expect the wifiConnect frame back.

The earlier attempt to listen for it read PDUs byte by byte, which
truncates every multi-byte PDU to its first byte -- so a notification
looked empty and the request was never seen. This one reads whole PDUs
and prints everything.

Usage: att_listen.py <ble-mac> [seconds] [--reply]
"""
import socket
import struct
import sys
import time

sys.path.insert(0, "/home/mauriciobc/Documentos/Code/ziot-rtp-bridge")
import ziot_fake_cloud as z

MAC = sys.argv[1]
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
REPLY = "--reply" in sys.argv

ATT_MTU_REQUEST = 0x02
ATT_MTU_RESPONSE = 0x03
ATT_WRITE_REQUEST = 0x12
ATT_NOTIFY = 0x1B
ATT_ERROR_RESPONSE = 0x01

CCCD_HANDLE = 0x000B
WRITE_HANDLE = 0x0008
START = time.time()


def note(label, data=b""):
    print(f"[{time.time() - START:6.2f}s] {label}"
          + (f" {data.hex()}" if data else ""), flush=True)


def read_pdu(sock, timeout=1.5):
    sock.settimeout(timeout)
    try:
        return sock.recv(512)
    except socket.timeout:
        return b""


def main():
    env = z._load_env("/home/mauriciobc/Documentos/Code/ziot-rtp-bridge/.env")
    frame = z.wifi_connect_frame(env["WIFI_SSID"], env["WIFI_PWD"],
                                 "192.168.68.114:8882", -180)
    chunks = z.chunk20(frame)

    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                      socket.BTPROTO_L2CAP)
    s.bind(("00:00:00:00:00:00", 0, 4, 1))
    s.settimeout(4.0)
    s.connect((MAC, 0, 4, 1))
    note("connected")

    att = lambda op, payload=b"": s.send(bytes([op]) + payload)

    # Subscribe notifications right away.
    att(0x12, struct.pack("<H", CCCD_HANDLE) + b"\x01\x00")
    note("cccd written")

    end = time.time() + SECONDS
    replied = False
    while time.time() < end:
        pdu = read_pdu(s, 1.0)
        if not pdu:
            continue
        if pdu[0] == ATT_NOTIFY:
            # Handle Value Notification: opcode, handle (2), value.
            handle = struct.unpack("<H", pdu[1:3])[0]
            value = pdu[3:]
            note(f"NOTIFY handle {handle:#06x} {len(value)}B", value)
            if REPLY and not replied:
                replied = True
                note("replying with the wifiConnect frame")
                for i, c in enumerate(chunks):
                    att(0x12, struct.pack("<H", WRITE_HANDLE) + c)
                    r = read_pdu(s, 1.0)
                    note(f"chunk {i}", r)
        elif pdu[0] == ATT_MTU_REQUEST:
            note("camera Exchange MTU Request", pdu)
            # Answer with the same size, in the same 3-byte shape it used.
            att(ATT_MTU_RESPONSE, pdu[1:3])
            note("answered its MTU request")
        elif pdu[0] == ATT_MTU_RESPONSE:
            note("MTU response", pdu)
        elif pdu[0] == ATT_ERROR_RESPONSE:
            note(f"error response {pdu.hex()}")
        elif pdu[0] == 0x13:
            note("write response")
        else:
            note("event", pdu)
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

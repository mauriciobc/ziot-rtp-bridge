#!/usr/bin/env python3
"""Write-probe the camera's GATT database.

This firmware refuses ATT reads (every Read Request and every discovery
request answers with an Error Response), but it accepts writes. A write
to a handle that does not exist errors; a write to a real characteristic
value is acknowledged with a Write Response -- and a write to the notify
characteristic's value makes the camera push a notification, which is how
the command channel is recognised.

So the handle map is found by writing one byte to each handle and looking
at what comes back.

Usage: att_probe.py <ble-mac> [first] [last]
"""
import socket
import struct
import sys
import time

WRITE_HANDLE = 0x0008
CCCD_HANDLE = 0x000B


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


def probe(mac, first, last):
    s = connect(mac)
    try:
        att(s, 0x02, struct.pack("<H", 247))
        print("mtu:", read_pdu(s, 2.0).hex())
        print("probing handles:")
        for handle in range(first, last + 1):
            # Subscribe the notify characteristic first, so a notification
            # triggered by the probe is delivered.
            att(s, 0x12, struct.pack("<HH", CCCD_HANDLE, 0x0001) +
                b"\x01\x00")
            read_pdu(s, 0.8)
            att(s, 0x12, struct.pack("<H", handle) + b"\x00")
            rsp = read_pdu(s, 1.2)
            if not rsp:
                print(f"  {handle:#06x}: silent")
                continue
            if rsp[0] == 0x13:
                print(f"  {handle:#06x}: WRITE OK (0x13)")
            elif rsp[0] == 0x1B:
                print(f"  {handle:#06x}: NOTIFY -> {rsp[3:].hex()}")
            elif rsp[0] == 0x01:
                err = rsp[2] if len(rsp) > 2 else -1
                print(f"  {handle:#06x}: error {err:#04x}")
            else:
                print(f"  {handle:#06x}: {rsp.hex()}")
    finally:
        s.close()


if __name__ == "__main__":
    mac = sys.argv[1]
    first = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x0001
    last = int(sys.argv[3], 0) if len(sys.argv) > 3 else 0x0014
    probe(mac, first, last)

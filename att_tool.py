#!/usr/bin/env python3
"""Raw-ATT tools for the ZIOT camera (no GATT client, no bluetoothctl).

The camera's GATT database resists discovery on some units, and a GATT
client holds the link, so everything here speaks ATT over L2CAP CID 4
directly. One recv() is one PDU (SOCK_SEQPACKET), and a short recv()
discards the rest of the datagram -- so a PDU is never read byte by byte.

Usage:
    att_tool.py enum   <ble-mac>            dump the GATT database
    att_tool.py write  <ble-mac> [sig-addr] write the wifiConnect frame
"""
import socket
import struct
import sys
import time

sys.path.insert(0, "/home/mauriciobc/Documentos/Code/ziot-rtp-bridge")
import ziot_fake_cloud as z

ATT_MTU_REQUEST = 0x02
ATT_WRITE_REQUEST = 0x12
ATT_NOTIFY = 0x1B
ATT_ERROR_RESPONSE = 0x01
ATT_READ_BY_GROUP = 0x10
ATT_READ_BY_TYPE = 0x08
ATT_FIND_INFO = 0x04

WRITE_HANDLE = 0x0008
CCCD_HANDLE = 0x000B


def att(sock, opcode, payload=b""):
    sock.send(bytes([opcode]) + payload)


def read_pdu(sock, timeout=1.5):
    """One whole ATT PDU, or b"" on timeout."""
    sock.settimeout(timeout)
    try:
        return sock.recv(512)
    except socket.timeout:
        return b""


def connect(mac, timeout=4.0):
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                      socket.BTPROTO_L2CAP)
    s.bind(("00:00:00:00:00:00", 0, 4, 1))
    s.settimeout(timeout)
    s.connect((mac, 0, 4, 1))
    return s


def uuid_str(raw):
    import uuid
    if len(raw) == 2:
        return "%04x" % struct.unpack("<H", raw)[0]
    if len(raw) == 16:
        return str(uuid.UUID(bytes_le=raw)).upper()
    return raw.hex()


def dump_gatt(sock):
    """Read the GATT database: services, characteristics, descriptors."""
    print("-- find information")
    att(sock, ATT_FIND_INFO, struct.pack("<HH", 0x0001, 0xFFFF))
    rsp = read_pdu(sock, 2.0)
    if rsp and rsp[0] == 0x05:
        length = rsp[1]
        body = rsp[5:]
        for off in range(0, len(body) - length + 1, length):
            entry = body[off:off + length]
            handle = struct.unpack("<H", entry[:2])[0]
            fmt = entry[2]
            uuid = uuid_str(entry[3:5] if fmt == 1 else entry[3:19])
            print(f"   {handle:#06x} {uuid}")

    print("-- services")
    att(sock, ATT_READ_BY_GROUP,
        struct.pack("<HH", 0x0001, 0xFFFF) + struct.pack("<H", 0x2803))
    rsp = read_pdu(sock, 2.0)
    services = []
    if rsp and rsp[0] == 0x11:
        length = rsp[1]
        body = rsp[5:]
        for off in range(0, len(body) - length + 1, length):
            entry = body[off:off + length]
            start, end = struct.unpack("<HH", entry[:4])
            services.append((start, end, uuid_str(entry[4:])))
            print(f"   service {start:#06x}-{end:#06x} {uuid_str(entry[4:])}")

    print("-- characteristics")
    chars = []
    for start, end, _ in services:
        att(sock, ATT_READ_BY_TYPE,
            struct.pack("<HH", start, end) + struct.pack("<H", 0x2803))
        rsp = read_pdu(sock, 2.0)
        if not (rsp and rsp[0] == 0x09):
            continue
        length = rsp[1]
        body = rsp[5:]
        for off in range(0, len(body) - length + 1, length):
            entry = body[off:off + length]
            handle = struct.unpack("<H", entry[:2])[0]
            props = entry[2]
            value_handle = struct.unpack("<H", entry[3:5])[0]
            cuuid = uuid_str(entry[5:])
            chars.append((handle, props, value_handle, cuuid))
            print(f"   char decl {handle:#06x} props {props:#04x} "
                  f"value {value_handle:#06x} {cuuid}")

    print("-- descriptors")
    for i, (handle, props, value_handle, cuuid) in enumerate(chars):
        end = chars[i + 1][0] - 1 if i + 1 < len(chars) else 0xFFFF
        att(sock, ATT_READ_BY_TYPE,
            struct.pack("<HH", value_handle, end) + struct.pack("<H", 0x2902))
        rsp = read_pdu(sock, 1.5)
        if not (rsp and rsp[0] == 0x09):
            continue
        length = rsp[1]
        body = rsp[5:]
        for off in range(0, len(body) - length + 1, length):
            entry = body[off:off + length]
            dhandle = struct.unpack("<H", entry[:2])[0]
            print(f"   cccd {dhandle:#06x} on char {handle:#06x} "
                  f"(value {value_handle:#06x})")


def write_frame(mac, sig_addr):
    env = z._load_env("/home/mauriciobc/Documentos/Code/ziot-rtp-bridge/.env")
    frame = z.wifi_connect_frame(env["WIFI_SSID"], env["WIFI_PWD"], sig_addr,
                                  -180)
    chunks = z.chunk20(frame)
    print(f"frame {len(frame)}B -> {len(chunks)} chunks")
    print(f"frame: {frame.hex()}")
    s = connect(mac)
    try:
        att(s, ATT_MTU_REQUEST, struct.pack("<H", 247))
        print("mtu:", read_pdu(s, 2.0).hex())
        att(s, ATT_WRITE_REQUEST,
            struct.pack("<HH", CCCD_HANDLE, 0x0001) + b"\x01\x00")
        print("cccd:", read_pdu(s, 2.0).hex())
        for i, c in enumerate(chunks):
            att(s, ATT_WRITE_REQUEST, struct.pack("<H", WRITE_HANDLE) + c)
            rsp = read_pdu(s, 2.0)
            print(f"chunk {i}: {rsp.hex() if rsp else 'silent'}")
            if rsp and rsp[0] == ATT_NOTIFY:
                payload = rsp[3:]
                print(f"  NOTIFY {len(payload)}B: {payload.hex()}")
                try:
                    print("  REPLY:", z.parse_provision_response(payload))
                except (ValueError, IndexError) as e:
                    print("  (partial:", e, ")")
        end = time.time() + 20
        while time.time() < end:
            rsp = read_pdu(s, 1.0)
            if not rsp:
                continue
            if rsp[0] == ATT_NOTIFY:
                payload = rsp[3:]
                print(f"NOTIFY {len(payload)}B: {payload.hex()}")
                try:
                    print("REPLY:", z.parse_provision_response(payload))
                except (ValueError, IndexError) as e:
                    print("(partial:", e, ")")
            else:
                print("event:", rsp.hex())
    finally:
        s.close()


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    what, mac = sys.argv[1], sys.argv[2]
    if what == "enum":
        s = connect(mac)
        try:
            att(s, ATT_MTU_REQUEST, struct.pack("<H", 247))
            print("mtu:", read_pdu(s, 2.0).hex())
            dump_gatt(s)
        finally:
            s.close()
        return 0
    if what == "write":
        sig = sys.argv[3] if len(sys.argv) > 3 else "192.168.68.114:8882"
        write_frame(mac, sig)
        return 0
    print("unknown command:", what)
    return 2


if __name__ == "__main__":
    sys.exit(main())

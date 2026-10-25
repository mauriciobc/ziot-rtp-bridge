#!/usr/bin/env python3
"""Provision the camera over raw ATT, answering its MTU request.

Two things the earlier attempts got wrong:

* the camera opens its *own* Exchange MTU Request (`02 17 00`, MTU 23)
  and expects an Exchange MTU Response (`03 17 00`) before it does
  anything else -- an unanswered request may leave it waiting;
* the notify subscription is a two-byte write to the CCCD; writing four
  bytes draws "invalid attribute value length" (0x0c).

Everything that comes back is printed with an elapsed timestamp, because
the camera's replies interleave with its own requests.

Usage: att_prov.py <ble-mac> [sig-addr] [write-handle]
"""
import hashlib
import socket
import struct
import sys
import time

sys.path.insert(0, "/home/mauriciobc/Documentos/Code/ziot-rtp-bridge")
import ziot_fake_cloud as z

MAC = sys.argv[1]
SIG = sys.argv[2] if len(sys.argv) > 2 else "192.168.68.114:8882"
# The vendor's own GATT dump (DECOMPILATION_REPORT.md section 8) lists the
# characteristics by their VALUE handles: 36000002 (write) at 0x0008 and
# 36000003 (notify) at 0x000a, so the CCCD sits at 0x000b. The camera
# agrees: a write to 0x000c answers "attribute not found" (0x0a), while
# 0x000b accepts one.
WRITE_HANDLE = int(sys.argv[3], 0) if len(sys.argv) > 3 else 0x0008
CCCD_HANDLE = 0x000B

ATT_MTU_REQUEST = 0x02
ATT_MTU_RESPONSE = 0x03
ATT_WRITE_REQUEST = 0x12
ATT_NOTIFY = 0x1B
ATT_ERROR_RESPONSE = 0x01

START = time.time()


def att(sock, opcode, payload=b""):
    sock.send(bytes([opcode]) + payload)


def read_pdu(sock, timeout=1.5):
    sock.settimeout(timeout)
    try:
        return sock.recv(512)
    except socket.timeout:
        return b""


def note(label, data=b""):
    print(f"[{time.time() - START:6.2f}s] {label}"
          + (f" {data.hex()}" if data else ""), flush=True)


def main(mac=None, sig=None, write_handle=None):
    mac = mac or MAC
    sig = sig or SIG
    write_handle = write_handle or WRITE_HANDLE
    env = z._load_env("/home/mauriciobc/Documentos/Code/ziot-rtp-bridge/.env")
    # The app appends a regCode for every real device -- it is omitted only
    # for the placeholder "FFFFFFFFFFFF". It is a nonce (the phone's UUID
    # plus a timestamp, hashed), so its value does not matter, but its
    # presence does: the frame is 61 bytes with it and 57 without.
    nonce = f"{mac}:{time.time():.0f}"
    digest = hashlib.sha256(nonce.encode()).digest()
    reg_code = ((digest[0] << 24) | (digest[2] << 16) |
                (digest[4] << 8) | digest[6])
    frame = z.wifi_connect_frame(env["WIFI_SSID"], env["WIFI_PWD"], sig, -180,
                                 reg_code=reg_code)
    chunks = z.chunk20(frame)
    note(f"frame {len(frame)}B -> {len(chunks)} chunks (regCode "
         f"{reg_code:#010x})")
    note("frame", frame)

    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                      socket.BTPROTO_L2CAP)
    s.bind(("00:00:00:00:00:00", 0, 4, 1))
    s.settimeout(4.0)
    s.connect((mac, 0, 4, 1))
    note("connected")

    att(s, ATT_MTU_REQUEST, struct.pack("<H", 247))
    note("sent MTU request")

    # The camera sends its own Exchange MTU Request (02 17 00, MTU 23).
    # Answering it draws "request not supported" (0x06), so it is left
    # alone -- the exchange is complete once we have its response.
    def handle(pdu):
        if pdu[0] == ATT_MTU_REQUEST and len(pdu) >= 3:
            note(f"camera MTU request (ignored): {pdu.hex()}")
            return True
        return False

    # Read the MTU response and anything else until the camera is ready.
    deadline = time.time() + 5
    while time.time() < deadline:
        pdu = read_pdu(s, 0.8)
        if not pdu:
            continue
        if pdu[0] == ATT_MTU_RESPONSE:
            note("MTU response", pdu)
            continue
        if handle(pdu):
            continue
        note("early event", pdu)

    # Subscribe notifications: a two-byte write to the CCCD (0x000b).
    att(s, ATT_WRITE_REQUEST, struct.pack("<H", CCCD_HANDLE) + b"\x01\x00")
    pdu = read_pdu(s, 1.0)
    note(f"cccd {CCCD_HANDLE:#06x}", pdu)

    for i, c in enumerate(chunks):
        att(s, ATT_WRITE_REQUEST, struct.pack("<H", write_handle) + c)
        pdu = read_pdu(s, 1.5)
        if pdu and pdu[0] == ATT_NOTIFY:
            payload = pdu[3:]
            note(f"chunk {i} NOTIFY {len(payload)}B", payload)
            try:
                note(f"chunk {i} REPLY: {z.parse_provision_response(payload)}")
            except (ValueError, IndexError) as e:
                note(f"chunk {i} (partial: {e})")
        else:
            note(f"chunk {i}", pdu)

    deadline = time.time() + 25
    while time.time() < deadline:
        pdu = read_pdu(s, 1.0)
        if not pdu:
            continue
        if pdu[0] == ATT_NOTIFY:
            payload = pdu[3:]
            note(f"NOTIFY {len(payload)}B", payload)
            try:
                note(f"REPLY: {z.parse_provision_response(payload)}")
            except (ValueError, IndexError) as e:
                note(f"(partial: {e})")
        elif pdu[0] == ATT_ERROR_RESPONSE:
            note(f"error response (handle {struct.unpack('<H', pdu[2:4])[0]:#06x}, "
                 f"code {pdu[4]:#04x})" if len(pdu) > 4 else "error response")
        else:
            note("event", pdu)
            handle(pdu)
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

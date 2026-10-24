#!/usr/bin/env python3
"""Talk to a ZIOTA camera over BLE -- the local path, no cloud.

The camera is an RTSP *publisher* (its own firmware names the flow:
`spook_rtsp_set_relay_info`, `rtsp://%s:%d/live/%s`, "start push,
session id:%s"). The vendor's app drives that publisher over the
camera's own local channel: BLE, and a TCP port the camera opens while
it is in AP mode.

This is the BLE side of it. The camera's GATT database, read from a
live one:

    0x0002  00002a00  device name             (read)
    0x0004  00002a01  appearance              (read)
    0x0008  36000002  write / write-no-rsp   the command channel
    0x000a  36000003  notify                 the answer channel
    0x000b  00002902  CCCD                   (subscribe)

The frame is the app's `CustomBleProtocolPacket`:

    86 86 | LEN16 | SEQ16 | DLEN16 | payload | XOR8 | 0x0D

with `LEN16 = 5 + DLEN` (the end symbol is not counted) and the XOR
over every preceding byte. The commands are `enum CmdAPType`:
wifiConnect 01, pullVideo 02, light 03, infraredLight 04, switchStatus
05, restart 06, reset 07, sdState 08, sdReset 09, sdVideoRecorde 10,
originHorizontal 11, originVertical 12, phoneTimestamp 13, appVoice
14, motorControl 15, speakControl 16, lampLight 17, deviceType 18,
definition 19, pushVideo 20 -- and the answer to `pullVideo` is what
carries the video URL.

The camera refuses BLE connections outside pairing mode, so this only
works while the camera is freshly reset (or in its AP mode).

`btgatt-client` is driven through a pty: it wants a terminal, and it
holds the link for as long as the session lives -- which matters,
because the camera drops the connection when the central goes away.

Usage:
    python3 ziot_ble.py                       # scan and report
    python3 ziot_ble.py --mac AA:BB:CC:DD:EE:FF
    python3 ziot_ble.py --mac ... --cmd pullVideo
"""
import argparse
import os
import pty
import re
import select
import struct
import subprocess
import sys
import time

CMD_WIFI_CONNECT = 0x01
CMD_PULL_VIDEO = 0x02
CMD_LIGHT = 0x03
CMD_INFRARED = 0x04
CMD_SWITCH_STATUS = 0x05
CMD_RESTART = 0x06
CMD_RESET = 0x07
CMD_SD_STATE = 0x08
CMD_SD_RESET = 0x09
CMD_SD_RECORD = 0x0A
CMD_ORIGIN_H = 0x0B
CMD_ORIGIN_V = 0x0C
CMD_TIMESTAMP = 0x0D
CMD_APP_VOICE = 0x0E
CMD_MOTOR = 0x0F
CMD_SPEAK = 0x10
CMD_LAMP = 0x11
CMD_DEVICE_TYPE = 0x12
CMD_DEFINITION = 0x13
CMD_PUSH_VIDEO = 0x14

COMMANDS = {
    "wifiConnect": CMD_WIFI_CONNECT, "pullVideo": CMD_PULL_VIDEO,
    "light": CMD_LIGHT, "infraredLight": CMD_INFRARED,
    "switchStatus": CMD_SWITCH_STATUS, "restart": CMD_RESTART,
    "reset": CMD_RESET, "sdState": CMD_SD_STATE, "sdReset": CMD_SD_RESET,
    "sdVideoRecorde": CMD_SD_RECORD, "originHorizontal": CMD_ORIGIN_H,
    "originVertical": CMD_ORIGIN_V, "phoneTimestamp": CMD_TIMESTAMP,
    "appVoice": CMD_APP_VOICE, "motorControl": CMD_MOTOR,
    "speakControl": CMD_SPEAK, "lampLight": CMD_LAMP,
    "deviceType": CMD_DEVICE_TYPE, "definition": CMD_DEFINITION,
    "pushVideo": CMD_PUSH_VIDEO,
}

CHAR_NAME = "0x0002"
CHAR_APPEARANCE = "0x0004"
CHAR_COMMAND = "0x0008"
CHAR_NOTIFY = "0x000a"


def xor(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return x


def ble_frame(payload: bytes, seq: int = 1) -> bytes:
    """The app's CustomBleProtocolPacket.

    `LEN16 = 5 + DLEN` counts the sequence, the data length, the payload
    and the XOR -- the end symbol is not counted. The end symbol IS
    present: the camera reassembles until it has the whole frame
    (`BLE totalLen:%d, offset:%d`) and answers nothing without it.
    """
    dlen = len(payload)
    ln = 5 + dlen
    head = b"\x86\x86" + struct.pack("!HHH", ln, seq, dlen) + payload
    return head + bytes([xor(head)]) + b"\x0d"


def decode_answer(raw: bytes) -> dict:
    """The camera's answer frame.

    `86 86 | LEN | SEQ | DLEN | payload | XOR | 0x0D`, and the payload
    of a status frame is `ff ff 00` -- the camera saying the command is
    not understood.
    """
    if len(raw) < 10 or raw[:2] != b"\x86\x86":
        return {"raw": raw.hex()}
    ln, seq, dlen = struct.unpack_from(">HHH", raw, 2)
    payload = raw[8:8 + dlen]
    out = {"len": ln, "seq": seq, "dlen": dlen, "payload": payload.hex(),
           "ascii": "".join(chr(c) if 32 <= c < 127 else "."
                            for c in payload)}
    if len(payload) >= 3 and payload[:2] == b"\xff\xff":
        out["verdict"] = "not understood"
    return out


class Gatt:
    """A live `btgatt-client` session, driven through a pty."""

    def __init__(self, mac):
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            ["btgatt-client", "-d", mac], stdin=slave, stdout=slave,
            stderr=slave, close_fds=True)
        os.close(slave)
        self.text = ""

    def read(self, seconds=1.0):
        out = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.3)
            if not ready:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            chunk = data.decode("latin-1", "replace")
            out.append(chunk)
            self.text += chunk
        return "".join(out)

    def send(self, line):
        os.write(self.master, (line + "\n").encode())
        time.sleep(0.4)

    def wait_for(self, needle, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.read(1.0):
                return True
        return False

    def close(self):
        try:
            os.write(self.master, b"quit\n")
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def scan():
    """Report the cameras that advertise themselves."""
    subprocess.run(["bluetoothctl", "power", "on"], capture_output=True)
    subprocess.run(["bluetoothctl", "--timeout", "20", "scan", "on"],
                   capture_output=True)
    time.sleep(8)
    out = subprocess.run(["bluetoothctl", "devices"], capture_output=True,
                         text=True).stdout
    found = {}
    for line in out.splitlines():
        m = re.match(r"Device ([0-9A-Fa-f:]{17})\s+(.*)", line)
        if m:
            found[m.group(1)] = m.group(2)
    for mac, name in found.items():
        if "ZIOTA" in name.upper():
            print("  %s  %s" % (mac, name))
    if not any("ZIOTA" in n.upper() for n in found.values()):
        print("  no camera advertising ZIOTA_*")
        for mac, name in found.items():
            print("  (other) %s  %s" % (mac, name))
    return found


def talk(mac, commands):
    """Connect, run the commands, and print the answers."""
    g = Gatt(mac)
    try:
        if not g.wait_for("GATT discovery procedures complete", timeout=45):
            print("  connect failed:", g.text.strip()[-200:])
            return 1
        print("  connected; GATT discovered")
        g.wait_for("[GATT client]#", timeout=20)

        g.send("register-notify %s" % CHAR_NOTIFY)
        g.wait_for("[GATT client]#", timeout=12)
        g.read(1)

        for label, cmd in commands:
            frame = ble_frame(bytes([cmd]))
            before = len(g.text)
            g.send("write-value %s %s"
                   % (CHAR_COMMAND,
                      " ".join("0x%02x" % b for b in frame)))
            time.sleep(1.8)
            chunk = g.read(3)
            new = g.text[before:]
            m = re.search(r"\((\d+) bytes\): ([0-9a-fA-F x0-9a-f]+)", new)
            print()
            print("  %-16s -> %s" % (label, frame.hex()))
            if m:
                raw = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", m.group(2)))
                info = decode_answer(raw)
                print("     answer: %s" % info)
                if info.get("verdict") == "not understood":
                    print("     (the camera did not understand it)")
            else:
                print("     (no answer)")
        return 0
    finally:
        g.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mac", help="the camera's BLE address")
    ap.add_argument("--cmd", action="append", default=[],
                    choices=sorted(COMMANDS),
                    help="a command to send (repeatable)")
    ap.add_argument("--scan", action="store_true",
                    help="only list the cameras")
    args = ap.parse_args()

    print("=" * 74)
    print("the cameras that advertise themselves")
    print("=" * 74)
    found = scan()
    if args.scan or not args.mac:
        if not args.mac:
            print()
            print("pass --mac to talk to one of them")
        return 0

    cmds = [(name, COMMANDS[name]) for name in (args.cmd or ["pullVideo"])]
    print()
    print("=" * 74)
    print("talking to %s" % args.mac)
    print("=" * 74)
    return talk(args.mac, cmds)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Arm the camera: the moment it advertises, provision it.

The camera's BLE window is short and opens at boot, so the connect has to
be ready before it does. This polls for the device and fires the
provisioning the moment it is there.

Usage: att_arm.py <ble-mac> [sig-addr] [write-handle] [timeout]
"""
import subprocess
import sys
import time

sys.path.insert(0, "/home/mauriciobc/Documentos/Code/ziot-rtp-bridge")
import att_prov

MAC = sys.argv[1]
SIG = sys.argv[2] if len(sys.argv) > 2 else "192.168.68.114:8882"
HANDLE = int(sys.argv[3], 0) if len(sys.argv) > 3 else 0x0009
TIMEOUT = float(sys.argv[4]) if len(sys.argv) > 4 else 1800.0


def present():
    try:
        r = subprocess.run(["bluetoothctl", "devices"], capture_output=True,
                           text=True, timeout=10)
        return MAC.upper() in r.stdout.upper()
    except (subprocess.TimeoutExpired, OSError):
        return False


def main():
    print(f"armed for {MAC} (write handle {HANDLE:#06x})", flush=True)
    end = time.time() + TIMEOUT
    while time.time() < end:
        if not present():
            time.sleep(0.4)
            continue
        print(f"{time.strftime('%H:%M:%S')} device present -- provisioning",
              flush=True)
        try:
            att_prov.main(MAC, SIG, HANDLE)
        except OSError as e:
            print(f"connect failed ({e}); retrying", flush=True)
            time.sleep(1)
            continue
        # Provisioned without an exception: keep the link open and wait
        # for the camera's answer.
        print("provision attempt finished", flush=True)
        time.sleep(5)
        return 0
    print("timed out", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Arm: catch the camera's BLE window and dump its GATT handles.

The camera advertises for a short window at boot. The connect is the
trigger, so this polls for the advertisement and connects the moment the
device is there.

Usage: att_watch.py <ble-mac> [timeout-seconds]
"""
import subprocess
import sys
import time

sys.path.insert(0, "/home/mauriciobc/Documentos/Code/ziot-rtp-bridge")
import att_probe

MAC = sys.argv[1]
TIMEOUT = float(sys.argv[2]) if len(sys.argv) > 2 else 1800.0


def known():
    try:
        r = subprocess.run(["bluetoothctl", "devices"], capture_output=True,
                           text=True, timeout=10)
        return r.stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


def main():
    print(f"armed for {MAC}", flush=True)
    end = time.time() + TIMEOUT
    while time.time() < end:
        if MAC.upper() not in known().upper():
            time.sleep(0.5)
            continue
        print(f"{time.strftime('%H:%M:%S')} device present -- probing",
              flush=True)
        try:
            att_probe.probe(MAC, 0x0003, 0x000c)
            return 0
        except OSError as e:
            print(f"connect failed ({e}); waiting", flush=True)
            time.sleep(2)
    print("timed out", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())

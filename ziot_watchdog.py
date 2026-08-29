#!/usr/bin/env python3
"""
ziot_watchdog.py — Check bridge + go2rtc health, restart dead streams
=====================================================================
Runs as a cron or systemd timer. Checks:
1. Bridge /health endpoint — are cameras streaming?
2. go2rtc /api/streams — are ffmpeg producers running?
3. Restarts Frigate if go2rtc streams are dead

Exit codes:
  0 = all healthy
  1 = issues found and fixed
  2 = critical failure
"""
import json
import subprocess
import sys
import urllib.request

BRIDGE_URL = "http://127.0.0.1:8085"
GO2RTC_URL = "http://127.0.0.1:1984"
FRIGATE_CONTAINER = "frigate"
MIN_FPS = 1.0  # minimum fps to consider a camera "healthy"

def check_bridge():
    """Check bridge health endpoint."""
    try:
        req = urllib.request.Request(f"{BRIDGE_URL}/health")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        return data
    except Exception as e:
        return {"status": "error", "error": str(e), "cameras": []}

def check_go2rtc():
    """Check go2rtc streams."""
    try:
        req = urllib.request.Request(f"{GO2RTC_URL}/api/streams")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        return data
    except Exception as e:
        return {"error": str(e)}

def restart_frigate():
    """Restart Frigate container."""
    print("RESTARTING Frigate...")
    result = subprocess.run(
        ["docker", "restart", FRIGATE_CONTAINER],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode == 0:
        print(f"  Frigate restarted: {result.stdout.strip()}")
        return True
    else:
        print(f"  RESTART FAILED: {result.stderr.strip()}")
        return False

def main():
    issues = []

    # 1. Check bridge
    bridge = check_bridge()
    if bridge.get("status") == "error":
        issues.append(f"BRIDGE DOWN: {bridge.get('error')}")
        print(f"CRITICAL: Bridge unreachable — {bridge.get('error')}")
    else:
        for cam in bridge.get("cameras", []):
            uid = cam["uid"]
            streaming = cam["streaming"]
            fps = cam["fps"]
            moves = cam["endpoint_moves"]
            last_rx = cam.get("last_rx_ago_s")

            if not streaming:
                if last_rx and last_rx > 30:
                    issues.append(f"{uid}: offline for {last_rx:.0f}s")
                else:
                    # Brief blip, might recover
                    pass
            elif fps < MIN_FPS:
                issues.append(f"{uid}: low fps ({fps})")

            status = "OK" if streaming and fps >= MIN_FPS else "DEGRADED"
            print(f"  [{status}] {uid}: {fps} fps, streaming={streaming}, "
                  f"moves={moves}, last_rx={last_rx}s")

    # 2. Check go2rtc
    go2rtc = check_go2rtc()
    if "error" in go2rtc:
        issues.append(f"go2rtc unreachable: {go2rtc['error']}")
    else:
        for name, info in go2rtc.items():
            if not name.startswith("cat_cam"):
                continue
            producers = info.get("producers", [])
            consumers = info.get("consumers", []) or []
            has_video = any(p.get("bytes_recv", 0) > 1000 for p in producers)

            if not producers:
                issues.append(f"go2rtc/{name}: no producers")
                print(f"  [DEAD] go2rtc/{name}: no producers")
            elif not has_video:
                issues.append(f"go2rtc/{name}: 0 video bytes")
                print(f"  [DEAD] go2rtc/{name}: 0 video bytes")
            else:
                print(f"  [OK] go2rtc/{name}: {len(producers)} producers, "
                      f"{len(consumers)} consumers")

    # 3. Summary
    print(f"\n{'='*40}")
    if not issues:
        print("ALL HEALTHY")
        sys.exit(0)
    else:
        print(f"ISSUES ({len(issues)}):")
        for i in issues:
            print(f"  - {i}")

        # If go2rtc streams are dead but bridge is up, restart Frigate
        go2rtc_dead = any("go2rtc" in i for i in issues)
        bridge_up = bridge.get("status") != "error"

        if go2rtc_dead and bridge_up:
            print("\nGo2rtc streams dead but bridge alive — restarting Frigate")
            if restart_frigate():
                issues.append("Frigate restarted")
                sys.exit(1)
            else:
                sys.exit(2)
        else:
            sys.exit(1)

if __name__ == "__main__":
    main()

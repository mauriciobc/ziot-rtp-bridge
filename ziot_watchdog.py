#!/usr/bin/env python3
"""
ziot_watchdog.py — Check bridge + go2rtc health, restart dead streams
=====================================================================
Runs as a cron or systemd timer. Checks:
1. Bridge /health endpoint — are cameras streaming?
2. go2rtc /api/streams — are producers receiving media?
3. Restarts Frigate if go2rtc streams are dead while the camera is live

A battery camera that is asleep (`streaming: false`) is not a go2rtc
failure — its HTTP producers go idle on purpose. Restarting Frigate then
just kicks a healthy nest.

Exit codes:
  0 = all healthy
  1 = issues found and fixed
  2 = critical failure
"""
import json
import re
import subprocess
import sys
import urllib.request

BRIDGE_URL = "http://127.0.0.1:8085"   # matches the bridge's default port
GO2RTC_URL = "http://127.0.0.1:1984"
FRIGATE_CONTAINER = "frigate"
MIN_FPS = 1.0  # minimum fps to consider a camera "healthy"

# UID as a path segment (/cam/1410…) or as the whole stream name.
_UID_IN_TEXT = re.compile(r"(?:/(?:cam|audio)/)(\d{8,})")
_UID_NAME = re.compile(r"^\d{8,}$")


def fetch_json(url: str, timeout: int = 5):
    """GET a JSON endpoint, or None when it cannot be reached."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None

def check_bridge():
    """Check bridge health endpoint."""
    data = fetch_json(f"{BRIDGE_URL}/health")
    return data if data is not None else \
        {"status": "error", "error": f"{BRIDGE_URL}/health unreachable",
         "cameras": []}

def check_go2rtc():
    """Check go2rtc streams. The "error" key means go2rtc itself is
    unreachable — distinct from up-but-streamless."""
    data = fetch_json(f"{GO2RTC_URL}/api/streams")
    return data if data is not None else {"error": f"{GO2RTC_URL} unreachable"}

def restart_frigate():
    """Restart Frigate container."""
    print("RESTARTING Frigate...")
    try:
        result = subprocess.run(
            ["docker", "restart", FRIGATE_CONTAINER],
            capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  RESTART FAILED: {e}")
        return False
    if result.returncode == 0:
        print(f"  Frigate restarted: {result.stdout.strip()}")
        return True
    else:
        print(f"  RESTART FAILED: {result.stderr.strip()}")
        return False


def go2rtc_stream_uid(name, info) -> str | None:
    """Camera UID this go2rtc stream belongs to, if we can see one.

    The example config names streams `cat_cam_1` and puts the UID in the
    producer URL (`/cam/<uid>`). Either is enough.
    """
    if _UID_NAME.fullmatch(name or ""):
        return name
    blobs = [name or ""]
    for p in info.get("producers") or []:
        if isinstance(p, dict):
            blobs.extend(str(v) for v in p.values()
                         if isinstance(v, (str, int)))
        else:
            blobs.append(str(p))
    m = _UID_IN_TEXT.search(" ".join(blobs))
    return m.group(1) if m else None


def go2rtc_has_video(info) -> bool:
    return any(
        isinstance(p, dict) and p.get("bytes_recv", 0) > 1000
        for p in (info.get("producers") or [])
    )


def go2rtc_stream_state(name, info, cams_by_uid) -> str:
    """`ok`, `idle` (camera is dark — not a go2rtc fault), or `dead`."""
    producers = info.get("producers") or []
    if producers and go2rtc_has_video(info):
        return "ok"
    uid = go2rtc_stream_uid(name, info)
    cam = cams_by_uid.get(uid) if uid else None
    if cam is not None and not cam.get("streaming"):
        return "idle"
    return "dead"


def main():
    issues = []
    go2rtc_dead = False
    go2rtc_down = False    # unreachable at all; no restart can fix this

    # 1. Check bridge
    bridge = check_bridge()
    cams_by_uid = {c["uid"]: c for c in bridge.get("cameras") or []
                   if isinstance(c, dict) and c.get("uid")}
    if bridge.get("status") == "error":
        issues.append(f"BRIDGE DOWN: {bridge.get('error')}")
        print(f"CRITICAL: Bridge unreachable — {bridge.get('error')}")
    else:
        # Fields added alongside the resilient boot; tolerate an older bridge.
        # A bridge that is up but has no cameras yet -- cloud unreachable, token
        # rejected -- serves an empty list, which the loop below would walk in
        # silence and call healthy. The boot phase is the only field that says
        # otherwise, so read it before trusting an empty roster.
        boot = bridge.get("boot") or {}
        phase = boot.get("phase")
        if phase and phase != "ready":
            detail = boot.get("detail") or phase
            issues.append(f"BRIDGE NOT SERVING CAMERAS ({phase}): {detail}")
            print(f"  [BOOT] {phase}: {detail}")

        for cam in bridge.get("cameras", []):
            # Tolerate an older or partial camera row: a malformed entry is
            # skipped, not a KeyError in the middle of the report.
            if not isinstance(cam, dict):
                continue
            uid = cam.get("uid")
            if not uid:
                continue
            streaming = cam.get("streaming", False)
            fps = cam.get("fps", 0.0)
            moves = cam.get("endpoint_moves", 0)
            last_rx = cam.get("last_rx_ago_s")
            # Fields added in bridge v3; tolerate an older bridge.
            mode = cam.get("mode", "direct")
            online = cam.get("online")
            free = cam.get("media_free")

            if not streaming:
                if last_rx is None:
                    # Never received a packet at all. `if last_rx and ...` used
                    # to swallow this case, hiding the worst failure there is.
                    issues.append(f"{uid}: no RTP since the bridge started")
                elif last_rx > 30:
                    issues.append(f"{uid}: offline for {last_rx:.0f}s")
                else:
                    # Brief blip, might recover
                    pass
            elif fps < MIN_FPS:
                issues.append(f"{uid}: low fps ({fps})")

            if mode == "relay":
                # Working, but on the vendor's forwarding server rather than
                # directly — worth surfacing, not worth restarting anything.
                issues.append(f"{uid}: streaming via relay, not direct")

            status = "OK" if streaming and fps >= MIN_FPS else "DEGRADED"
            state = f"online={online}, free={free}" if online is not None else \
                    f"online={cam.get('online_state')}, media={cam.get('media_state')}"
            seen = "never" if last_rx is None else f"{last_rx}s ago"
            print(f"  [{status}] {uid}: {fps} fps, streaming={streaming}, "
                  f"mode={mode}, {state}, "
                  f"moves={moves}, last_rx={seen}")

    # 2. Check go2rtc
    go2rtc = check_go2rtc()
    if go2rtc is None or "error" in go2rtc:
        go2rtc_down = True
        error = go2rtc.get("error") if isinstance(go2rtc, dict) else "no answer"
        issues.append(f"go2rtc unreachable: {error}")
    else:
        for name, info in go2rtc.items():
            if not isinstance(info, dict):
                continue
            producers = info.get("producers", []) or []
            consumers = info.get("consumers", []) or []
            state = go2rtc_stream_state(name, info, cams_by_uid)
            if state == "ok":
                print(f"  [OK] go2rtc/{name}: {len(producers)} producers, "
                      f"{len(consumers)} consumers")
            elif state == "idle":
                uid = go2rtc_stream_uid(name, info)
                print(f"  [IDLE] go2rtc/{name}: camera {uid} not streaming")
            else:
                go2rtc_dead = True
                why = "no producers" if not producers else "0 video bytes"
                issues.append(f"go2rtc/{name}: {why}")
                print(f"  [DEAD] go2rtc/{name}: {why}")

    # 3. Summary
    print(f"\n{'='*40}")
    if not issues:
        print("ALL HEALTHY")
        sys.exit(0)
    else:
        print(f"ISSUES ({len(issues)}):")
        for i in issues:
            print(f"  - {i}")

    # A restart only helps when go2rtc is up but its streams died inside
    # Frigate. A down go2rtc or a down bridge is fixed by neither.
    bridge_up = bridge.get("status") != "error"

    if go2rtc_dead and bridge_up and not go2rtc_down:
        print("\nGo2rtc streams dead but bridge alive — restarting Frigate")
        if restart_frigate():
            issues.append("Frigate restarted")
            sys.exit(1)
        else:
            sys.exit(2)
    sys.exit(1)

if __name__ == "__main__":
    main()

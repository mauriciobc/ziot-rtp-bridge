#!/usr/bin/env bash
# Capture the vendor's live-view push.
#
# Swaps the broker's signalling listener (8882) for the transparent wiretap,
# which forwards the camera's signalling to the REAL vendor and logs both
# directions. Then press live view in the vendor app: the push lands in the
# log and ziot_push_catch.py prints it, decoded.
#
#   sudo ./ziot_capture_push.sh start     # broker --sig 0, tap up, catcher up
#   sudo ./ziot_capture_push.sh restore   # tap down, broker back on 8882
#
# The broker keeps HTTP/STUN/WS/DNS the whole time; only 8882 moves.
# Upstream vendor sig defaults to 47.97.71.82:8882 (the IP the camera's own
# 0x8000 names, and it is NOT one of the claimed /32s, so it does not loop
# back). Override with VENDOR_SIG=<ip>.
set -euo pipefail

REPO="/home/mauriciobc/Documentos/Code/ziot-rtp-bridge"
LOG="/home/mauriciobc/ziot-wiretap.log"
VENDOR_SIG="${VENDOR_SIG:-47.97.71.82}"
BROKER_ARGS="--http 80 --sig 0 --stun 13478 --ws 7080 --dns 53 --upstream-dns 1.1.1.1 --bind-ip 192.168.68.114 --relay-port 8554 --log /home/mauriciobc/ziot-broker.log"
PIDDIR="/tmp/ziot-capture"
mkdir -p "$PIDDIR"

start() {
  echo "[*] stopping any broker holding :8882 (it will come back with --sig 0) ..."
  pkill -f 'ziot_fake_cloud.py' 2>/dev/null || true
  sleep 1
  echo "[*] relaunching broker WITHOUT its 8882 listener (--sig 0) ..."
  ( cd "$REPO" && nohup python3 ziot_fake_cloud.py $BROKER_ARGS >/tmp/ziot-capture/broker.out 2>&1 & echo $! > "$PIDDIR/broker.pid" )
  sleep 2
  echo "[*] starting transparent wiretap on 0.0.0.0:8882 -> vendor $VENDOR_SIG:8882 ..."
  ( cd "$REPO" && nohup python3 ziot_wiretap.py --listen 0.0.0.0:8882 \
       --upstream-host "$VENDOR_SIG" --upstream-port 8882 --log "$LOG" \
       >/tmp/ziot-capture/wiretap.out 2>&1 & echo $! > "$PIDDIR/wiretap.pid" )
  sleep 1
  if ! ss -ltn 2>/dev/null | grep -q ':8882'; then
    echo "[!] nothing is listening on :8882 -- check /tmp/ziot-capture/wiretap.out" >&2
  fi
  echo
  echo "=== READY ==="
  echo "  broker pid  : $(cat "$PIDDIR/broker.pid" 2>/dev/null)   (HTTP/STUN/WS/DNS, no sig)"
  echo "  wiretap pid : $(cat "$PIDDIR/wiretap.pid" 2>/dev/null)  -> $VENDOR_SIG:8882"
  echo "  log         : $LOG"
  echo
  echo "Next, in a normal (non-root) shell run the catcher and watch it:"
  echo "    python3 $REPO/ziot_push_catch.py --log $LOG"
  echo "Wait for the camera to re-handshake through the tap and for '>>> ARMED <<<',"
  echo "THEN open the camera's live view in the vendor app. The push prints decoded."
  echo
  echo "When done:  sudo $0 restore"
}

restore() {
  echo "[*] stopping wiretap ..."
  [ -f "$PIDDIR/wiretap.pid" ] && kill "$(cat "$PIDDIR/wiretap.pid")" 2>/dev/null || true
  pkill -f 'ziot_wiretap.py' 2>/dev/null || true
  sleep 1
  echo "[*] restoring broker WITH its 8882 listener ..."
  pkill -f 'ziot_fake_cloud.py' 2>/dev/null || true
  sleep 1
  ( cd "$REPO" && nohup python3 ziot_fake_cloud.py ${BROKER_ARGS/--sig 0/--sig 8882} >/tmp/ziot-capture/broker.out 2>&1 & echo $! > "$PIDDIR/broker.pid" )
  sleep 2
  ss -ltn 2>/dev/null | grep -q ':8882' && echo "[+] broker back on :8882" || echo "[!] broker not on :8882 -- check /tmp/ziot-capture/broker.out" >&2
}

case "${1:-}" in
  start)   start ;;
  restore) restore ;;
  *) echo "usage: sudo $0 {start|restore}" >&2; exit 2 ;;
esac

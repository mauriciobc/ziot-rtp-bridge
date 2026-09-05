# AGENTS.md

## What this is

Bridge for ZIOT/gps555.net (Taixin TXW817) IP cameras: registers with the vendor
cloud for signalling, receives RTP directly from the camera on the LAN, and
re-serves it as MJPEG/WAV over HTTP. Three standalone scripts, no packages:

- `ziot_rtp_bridge.py` — the entire bridge (~2400 lines, deliberately one file)
- `ziot_offline_probe.py` — LAN-only camera discovery (send vendor UDP hello, listen for RTP)
- `ziot_watchdog.py` — cron/systemd helper that checks bridge + go2rtc health and restarts Frigate

## Hard constraints

- **Python 3.10+ standard library only. Never add third-party dependencies.**
  The Docker image (`python:3.11-slim`) copies only `ziot_rtp_bridge.py`; if you
  split the code into modules or add imports beyond stdlib, update the Dockerfile too.
- `ziot_config.json` holds a real account JWT, is gitignored, and must never be
  committed. Tests must not read it; they build their own temp configs.
- Protocol behaviors are empirically verified against real firmware
  (`TXW817_A_V1.0.12.32`) and documented in `README.md` and
  `DECOMPILATION_REPORT.md`. Do not "simplify" these without checking the docs:
  - session dies in ~12 s without `notify(keepAlive)` every 2 s (so punch interval
    must be < 2 s; default 1.0 s)
  - unsolicited `App send hello` is ignored — `send-stun-addr` + `notify(start)`
    is what starts the session, even under `--offline`
  - STUN answers with a lower `seqNo` must be rejected (`_accept_stun`)
  - the bridge never rebinds its local UDP socket after rendezvous (a new source
    port misses replies)
- `DECOMPILATION_REPORT.md` §7 maps reverse-engineered vendor-app findings to
  specific bridge changes; consult it before changing rendezvous, relay, or
  cloud-state logic (`mediaState`/`onlineState` semantics match the app exactly).

## Commands

```bash
python3 -m pytest tests -q                    # full suite (~106 tests + subtests, ~11 s)
python3 -m pytest tests/test_ziot_rtp_bridge.py::DirectPacketTests -q   # one class
python3 -m pytest "tests/test_ziot_rtp_bridge.py::PunchIntervalTests::test_x" -q
python3 -m unittest discover -s tests -v      # works too; tests are plain unittest
```

- No lint/typecheck/codegen config exists; tests are the only verification.
- Tests are stdlib `unittest` classes (pytest runs them fine) using real loopback
  UDP sockets and `ThreadingHTTPServer` — they need no network, camera, or token,
  but do need loopback binding.
- There is no CI workflow. Manual run before committing.
- Live verification against real cameras is possible only on the camera's LAN
  subnet (host networking; Docker bridge mode cannot work). Don't attempt it
  unless the user asks.

## Style

- Single-file scripts with module docstrings; keep new code in the same file
  rather than introducing modules unless the user asks.
- Commit messages follow the repo's existing style: imperative, short rationale
  ("Never let a cloud failure at boot strand the fleet").

# ziot_rtp_bridge

Local MJPEG + audio bridge for **ZIOT / gps555.net** IP cameras (Taixin
TXW817, sold as "X5" / "A9"), so they can be consumed by go2rtc, Frigate,
Home Assistant, or a plain browser instead of the vendor phone app.

Verified working on hardware 2026-08-29 with three cameras streaming
simultaneously (video + audio), firmware `TXW817_A_V1.0.12.32`. `--offline`
verified 2026-09-05 on the same firmware: LAN RTP from the camera, cloud
used only for signalling.

---

## Why this exists

These cameras have **no local server in station mode**. A port scan of a
camera on the LAN comes back completely empty — no RTSP on 554, no HTTP, no
ONVIF. The usual tricks do not work:

| Approach | Result |
|---|---|
| `rtsp://…:554/` directly to camera | ✗ nothing listening |
| Block cloud at router to force local mode | ✗ camera has no local server to fall back to |
| `cam-reverse` / `aiopppp` / iLnk-PPPP tools | ✗ wrong protocol family for this generation |
| `rtsp://relay_ip:554/` (server root) | ✗ empty `DESCRIBE` — the path matters, see below |

What **does** work: register this host with the vendor cloud
(`send-stun-addr` + `notify(start)`), then keep the session alive with
`notify(keepAlive)` every 2 s. The camera replies by streaming **unencrypted
RTP from its own UDP socket to yours**. This script does that and re-serves
it as MJPEG over HTTP. Hellos (`App send hello`) keep NAT warm; they do not
start the session on this firmware.

The `relay_ip` server *is* usable, but only at the app's own paths —
`rtsp://<relay_ip>/live/<uid>` or `/rtp/<last 8 of uid>`, not the root. The
bridge falls back to it automatically when the direct path stays down.

> **Media is captured on the LAN, from the camera.** Video and audio never
> traverse `ipc.gps555.net`. The cloud is only signalling: publish our
> address, wake the camera, keepalive. `--offline` skips the device-list
> roster; it does not skip that handshake. See Limitations.

---

## Requirements

* **Python 3.10+**, standard library only. No `pip install`.
* The host **must be on the same LAN/subnet as the cameras.** This is a hard
  requirement, not a preference — the bridge talks straight to the camera's
  private IP. A NAT'd container (Docker bridge networking, Waydroid, a VM on
  NAT) will register an unroutable address and receive nothing.
  → Run on the host network, or use `network_mode: host`.
* A **valid account JWT** (see Configuration). Required on these TXW817
  cameras even with `--offline` — unsolicited hellos are ignored, and the
  session dies in ~12 s without `notify(keepAlive)`.
* `ffmpeg` — *not* needed by the bridge itself, only by go2rtc/Frigate
  downstream.

---

## Configuration

Create `ziot_config.json` next to the script:

```json
{
  "token":   "eyJhbGciOiJIUzI1NiIs…",
  "user_id": 3132031,
  "port":    8085,
  "cameras": [],
  "offline_endpoints": {
    "141030191094": "192.168.18.75",
    "140979857781": "192.168.18.29"
  }
}
```

`offline_endpoints` is unused unless `--offline` or `"offline": true`.

| Key | Required | Meaning |
|---|---|---|
| `token` | yes* | Account JWT, sent as `Authorization: Bearer …`. Required on TXW817 even with `--offline`. Omit only if `ziot_offline_probe.py` reports a HIT (punch-only cameras) |
| `user_id` | no | Numeric account id. Taken from the JWT `user_id` claim when omitted. Needed for the device-list roster and `--list-cameras` |
| `port` | no | HTTP listen port, default `8085` |
| `cameras` | no | Allow-list of UIDs. `[]` or omitted = every camera on the account (or every key in `offline_endpoints` when `--offline`) |
| `only_online` | no | Only start cameras the app would call online (`onlineState == "1"`). Checked **once at startup** — a camera that is offline then stays skipped until you restart. Default `false` — cloud statuses fluctuate, so the bridge normally tries every camera and reports state. Ignored under `--offline` |
| `punch_interval` | no | Seconds between `App send hello` packets. Default `1.0`, matching the app; must be a finite value strictly greater than 0 and strictly below the 2 s starvation threshold. Anything else is logged as an error and replaced with the default — a bad value never stops the bridge from booting |
| `relay_user` / `relay_pass` | no | Credentials for the RTSP relay, if it ever demands them. Unset by default — the relay is not known to authenticate, and the bridge fails loudly rather than guessing |
| `offline` / `offline_endpoints` | no | Roster from LAN targets instead of `GET /v1/ipc`: `{"offline": true, "offline_endpoints": {"<uid>": "192.168.18.75"}}`. With a token, IP only is enough — the listen port rotates and comes from STUN. Punch-only (no token), IP only makes the bridge hello-sweep the ephemeral range from its own socket; `ip:port` from a probe HIT skips the sweep. A token in the same file still does notify/keepalive. See "Running without the device list" |

```bash
chmod 600 ziot_config.json    # it holds an account credential
```

**Startup failures are deliberately two-tier.** The rule is whether retrying
could ever help.

*Fatal, exits `2`:* a config the bridge cannot read or cannot parse (nothing to
run), and a token the cloud rejects on the very first call (no token un-expires
itself, and nothing is serving yet, so this is a config error like any other).
Each names what to fix, and the two config cases are told apart deliberately —
an unreadable file names the mount, while a file that read fine but is not
valid JSON says so and gives the parser's line and column, because sending you
to check a mount that is already correct wastes the first thing you try.

*Never fatal:* a bad config *value* is logged and replaced with its default; an
unreachable cloud binds the HTTP port anyway and retries the device list in the
background with backoff, so `/health` answers from the first second and says
`boot.phase: retrying` with the error. Refusing to start in these cases puts
every camera dark in a `--restart unless-stopped` loop — the same retry, with a
worse duty cycle and nothing answering to say why — which is what commit
b423ebc ("Never let a startup failure strand a camera") exists to prevent.

A token rejected *later*, once the bridge is already serving, parks at
`boot.phase: failed` rather than exiting: staying up and reporting the reason
beats disappearing. If you want a typo caught before it reaches production,
validate the config in your deploy pipeline — the bridge is not the place to
fail that check.

### Getting the token

The token is **not** obtainable from the camera — it is minted when the phone
app logs in. Capture it once from the app's HTTPS traffic:

* **PCAPdroid** (Android, no root) with TLS decryption enabled, or
* **mitmproxy** with the phone proxied through it.

Open the app, then look for any request to `ipc.gps555.net` and copy the
`authorization: Bearer …` header value.

Recover `user_id` from the token itself:

```bash
python3 -c "import base64,json,sys; p=sys.argv[1].split('.')[1]; \
print(json.loads(base64.urlsafe_b64decode(p+'='*(-len(p)%4))))" "<TOKEN>"
```

**The token expires after ~15 days.** When it does, every camera stops — the
rendezvous and the 2 s keepalive are token-gated, including `--offline`.
Re-capture and restart. There is no refresh-token flow implemented here.

---

## Running

### Direct (bare Python)

```bash
python3 ziot_rtp_bridge.py --list-cameras   # inventory, verifies the token
python3 ziot_rtp_bridge.py                  # serve on :8085
```

| Flag | Purpose |
|---|---|
| `--config PATH` | config file (default `ziot_config.json`) |
| `--port N` | override listen port |
| `--list-cameras` | print cameras on the account and exit (needs the cloud; incompatible with `--offline`) |
| `--bind-ip IP` | LAN IP the camera will send RTP to. Set this on multi-homed hosts |
| `--offline` | Roster from `offline_endpoints` instead of the device list. Token (if present) still does notify/keepalive |
| `--force-relay` | Skip the direct path and play the vendor RTSP relay (for exercising that path) |

Cameras start in parallel; an awake camera is usually live within ~10 s. A
battery camera with `onlineState=0` stays dark until it next checks in —
LAN hellos will not wake it.

### Running without the device list (`--offline`)

`--offline` takes the camera roster from `offline_endpoints` instead of
`GET /v1/ipc`. Media is still LAN RTP from the camera. It does **not**
make TXW817 cameras independent of the vendor cloud. Measured on firmware
`TXW817_A_V1.0.12.32` (2026-09-05):

* Unsolicited `App send hello` is ignored, including a full ephemeral-port
  sweep and a hello at the current STUN port from a fresh socket.
* `send-stun-addr` + `notify(start)` is what starts the session. The camera
  then sends RTP to the registered viewer address, not to whoever punches.
* Without `notify(keepAlive)` every 2 s the session dies at ~12 s, hellos
  notwithstanding. The listen port also rotates (several times a minute).

So keep the account token in the same config. The LAN IP is enough — the
bridge pins that IP, follows the STUN private port, and does not need a
probed listen port:

```json
{
  "token": "eyJ…",
  "user_id": 3132031,
  "offline": true,
  "port": 8085,
  "offline_endpoints": {
    "141030191094": "192.168.18.75",
    "140979857781": "192.168.18.29"
  }
}
```

```bash
python3 ziot_rtp_bridge.py --config ziot_config.json --offline \
    --bind-ip 192.168.18.6
```

A camera the cloud marks `onlineState=0` is usually asleep (these TXW817
units are battery-powered). Punching its last LAN IP does nothing while
the radio is down. Keep the token: while the camera is cloud-offline the
bridge **reannounces the same local UDP port** (no rebind — a new source
port is a miss when it next checks in) and sends `notify(start)`. When
`onlineState` flips 0→1, rendezvous runs immediately instead of waiting
out a 60 s backoff. Device-list flags are polled every 5 s while any
camera is cloud-offline (otherwise 30 s).

Omit the token only if `ziot_offline_probe.py` reports a HIT. Then the
bridge punches that host, hello-sweeps when starved, and will not rebind
the local socket (a new source port would miss replies aimed at the old
one). The probe requires RTP v2 with PT 0 or 26, so an echo of
`App send hello` is not a hit. The bridge does not need the probe's port
number, though: give `offline_endpoints` the IP only and the bridge
hello-sweeps the ephemeral range **from its own listening socket** — a
reply is RTP arriving on that socket, and the punch loop follows the
source. The sweep is paced (~2000 hellos/s, ~14 s over the Linux
ephemeral range) and runs on the rendezvous socket, never on a throwaway
probe socket that would close before the reply lands.

```bash
python3 ziot_offline_probe.py 192.168.18.75 --uid 141030191094
```

A silent sweep on this firmware is normal, not a probe bug.

### Docker (recommended)

```bash
docker build -t ziot-bridge .
docker run -d --name ziot-bridge \
  --network host \
  --restart unless-stopped \
  -v /path/to/ziot_config.json:/app/ziot_config.json:ro \
  ziot-bridge
```

The runtime configuration is deliberately excluded from the build context and
image and must be supplied through the shown read-only mount. Forget it and the
bridge exits 2 with a log line naming the mount it wanted, rather than a bare
`FileNotFoundError`.

**`--network host` is mandatory.** The camera sends UDP directly to the
bridge's IP — Docker bridge networking puts the container behind NAT and the
camera sends to an unroutable address.

### systemd (bare Python)

`/etc/systemd/system/ziot-bridge.service`:

```ini
[Unit]
Description=ZIOT camera bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ziot
WorkingDirectory=/opt/ziot
ExecStart=/usr/bin/python3 /opt/ziot/ziot_rtp_bridge.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now ziot-bridge
```

`Restart=always` matters: if the JWT expires or the network drops, the process
should come back up rather than sit dead.

---

### Endpoints

| Path | Content |
|---|---|
| `/` | JSON index — per-camera `streaming` flag, fps, and packet counters |
| `/view/<uid>` | HTML page, video + audio together |
| `/cam/<uid>` | MJPEG, `multipart/x-mixed-replace` |
| `/audio/<uid>` | Streaming WAV, 8 kHz mono 16-bit PCM |
| `/health` | Detailed health JSON for watchdog/monitoring |

#### `/health` response

```json
{
  "status": "ok",
  "boot": { "phase": "ready" },
  "cameras": [
    {
      "uid": "141030191094",
      "mode": "direct",
      "relay_url": null,
      "endpoint_kind": "static-lan",
      "stun_seq": 42,
      "online_state": "1",
      "media_state": "1",
      "online": true,
      "media_free": false,
      "cloud_relay": "156.246.16.114:554",
      "cloud_age_s": 12.4,
      "streaming": true,
      "fps": 6.5,
      "frames_total": 1234,
      "audio_pkts": 5678,
      "rx_datagrams": 9012,
      "rx_errors": 0,
      "foreign_ssrc": 0,
      "media_source": "192.168.18.75:53296",
      "endpoint_moves": 2,
      "re_rendezvous_count": 1,
      "backoff_s": 5.0,
      "last_rx_ago_s": 0.1,
      "addr": "192.168.18.75:53296"
    }
  ]
}
```

`boot` says why the bridge is not serving cameras, when it isn't: `starting`,
`retrying` (with `detail` and `attempts` — the cloud is unreachable and the
bridge is backing off), `failed` (the cloud rejected the token; retrying cannot
help), `no-cameras` (the device list held nothing matching your allow-list), or
`ready`. `status` deliberately stays `"ok"`/`"degraded"` — the watchdog reserves
`"error"` for "could not reach the bridge at all", so a new value here would be
indistinguishable from a dead bridge.

`addr` is where punches and hellos go (STUN, or the pinned LAN IP under
`--offline`). `media_source` is where RTP actually arrives from. They
normally match; a lasting disagreement means the broker's answer is stale
and the direct path is one-way. `endpoint_kind` is `private` or `public`
from the app's /24 rule, or `static-lan` when `--offline` pins the
configured IP and takes only the STUN private port. `re_rendezvous_count`
and `backoff_s` are the recovery loop. `foreign_ssrc` counts datagrams
dropped for carrying another camera's SSRC, `rx_errors` counts packets
whose handling raised.

`rx_datagrams` counts every datagram that arrived on the direct socket, before
any filtering. It is the one number that separates *"the camera is silent"*
from *"packets arrive and we reject them all"* — two states that look identical
on every other counter. `rx_datagrams: 0` means nothing reached us and the
problem is upstream (camera asleep, not publishing, no L2 route). A climbing
`rx_datagrams` with `frames_total` stuck at 0 means the media is arriving and
being dropped — check `foreign_ssrc` next, and the `dropping RTP with ssrc …`
log line names what it saw against what it expected.

`status` is `"ok"` when at least one camera is streaming, `"degraded"` otherwise.
Use `/health` for Docker HEALTHCHECK or external monitoring.

---

### Watchdog

`ziot_watchdog.py` checks both the bridge and go2rtc health, and auto-restarts
Frigate if go2rtc streams die but the bridge is still alive. It also reads
`boot.phase`: a bridge that is up but serving an empty camera roster would
otherwise walk a zero-length list in silence and be called healthy. An older
bridge that does not send the field is tolerated.

```bash
python3 ziot_watchdog.py
# Exit codes: 0 = healthy, 1 = issues found, 2 = critical
```

Run via cron every 5 minutes:

```bash
*/5 * * * * /usr/bin/python3 /opt/ziot/ziot_watchdog.py >> /var/log/ziot-watchdog.log 2>&1
```

---

## go2rtc

A ready-to-run config is in [`go2rtc.yaml`](go2rtc.yaml). The cameras emit
MJPEG (no H.264 on the TXW817), which Frigate cannot record to mp4, and
the HTTP audio is a separate WAV. go2rtc merges them:

* Native MJPEG from `/cam/<uid>` — live view is instant (no keyframe wait).
* H.264 transcode only for clients that need it (Frigate, WebRTC, MSE),
  with a 1 s GOP (`-g 8`) instead of go2rtc's default `-g 50` (6–8 s at
  these frame rates).
* AAC for Frigate + Opus for WebRTC, `#async` so JPEG and WAV clocks
  do not have to agree.

```yaml
streams:
  cat_cam_1:
    - http://127.0.0.1:8085/cam/141030191094
    - ffmpeg:http://127.0.0.1:8085/audio/141030191094#audio=aac#audio=opus#async
    - ffmpeg:cat_cam_1#video=h264

ffmpeg:
  h264: "-c:v libx264 -g:v 8 -bf:v 0 -profile:v high -level:v 4.1 -preset:v superfast -tune:v zerolatency -pix_fmt:v yuv420p"
```

If the bridge runs outside the go2rtc container, replace `127.0.0.1` with the
host IP — and remember the bridge itself still needs host networking.
Standalone:

```bash
docker run -d --name go2rtc --network host --restart unless-stopped \
  -v /path/to/go2rtc.yaml:/config/go2rtc.yaml \
  alexxit/go2rtc
```

`--network host` is required for WebRTC UDP. UI: `http://<host>:1984/` —
pick MJPEG for a first-frame-now preview, or WebRTC/MSE for the transcode.

Append `#hardware` to the h264 line for VAAPI / NVENC / VideoToolbox.

### WebRTC (optional, LAN only)

```yaml
webrtc:
  listen: ":8555"
  candidates:
    - stun:8555
```

`stun:8555` advertises the host's own address. Pin a LAN IP instead
(`192.168.18.6:8555`) on a multi-homed box. WebRTC does not work through
Cloudflare tunnels without TURN — MSE fallback is used automatically.

---

## Frigate

Consume the go2rtc restream. Match `detect` to the camera's real output
(**640×480**); claiming more resolution than exists only wastes CPU.

If go2rtc is **nested in Frigate**, paste `streams` / `ffmpeg` / `webrtc`
from `go2rtc.yaml` under `go2rtc:` (do not also run a second go2rtc).
If go2rtc is **standalone**, omit the `go2rtc:` block and point Frigate
at `rtsp://127.0.0.1:8554/<name>`.

```yaml
go2rtc:
  streams:
    cat_cam_1:
      - http://127.0.0.1:8085/cam/141030191094
      - ffmpeg:http://127.0.0.1:8085/audio/141030191094#audio=aac#audio=opus#async
      - ffmpeg:cat_cam_1#video=h264
  ffmpeg:
    h264: "-c:v libx264 -g:v 8 -bf:v 0 -profile:v high -level:v 4.1 -preset:v superfast -tune:v zerolatency -pix_fmt:v yuv420p"

cameras:
  cat_cam_1:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/cat_cam_1
          input_args: preset-rtsp-restream
          roles: [detect, record]
    live:
      streams:
        cat_cam_1: cat_cam_1
    detect:
      width: 640
      height: 480
      fps: 5
```

The cameras deliver ~6-8 fps; setting `detect.fps` above that gains nothing.
`live.streams` makes Frigate's live view use go2rtc (WebRTC/MSE/MJPEG)
instead of opening a third ffmpeg on the restream.

---

## Bandwidth

~0.5–0.6 Mbit/s per camera (≈65 KB/s video + 16 KB/s audio). Three cameras is
roughly 1.7 Mbit/s — trivial for wired LAN, but it is continuous, and it is
Wi-Fi airtime on the camera side.

---

## Troubleshooting

**A camera shows `streaming: false`.**
Normal for the first few seconds. If it persists: confirm the host is on the
camera's subnet (`ping` its LAN IP) and that `--bind-ip` is an address on that
subnet. A battery camera with `onlineState=0` and no ARP is asleep — hellos
cannot wake it; wait for the 0→1 flip. The bridge re-resolves a starved
camera's endpoint automatically once media has been absent for 2 s, at most
once every 5 s — watch for `endpoint moved …` lines, which are expected and
healthy. A 2–3 s gap on a STUN port rotate is not a dead camera; recovery
only rebinds after ~10 s of silence.

**`--offline` boots but nothing streams.**
The roster came from LAN IPs; signalling still needs a token on this
firmware. A punch-only run (no token) dies at ~12 s if a session ever
starts at all. Check the boot log for `using token for notify/keepalive`
versus `punch-only`. Sleeping cameras (`onlineState=0`) stay dark until
they check in with the vendor.

**`ziot_offline_probe.py` reports nothing.**
Normal on TXW817 firmware — unsolicited hellos are ignored. Put the LAN
IP in `offline_endpoints` and run the bridge with a token. A HIT means
that camera will punch without a token.

**Everything says `mode: down` and nothing streams.**
The bridge stays up and retries rather than exiting, so this is a report, not a
crash. If the cameras hold DHCP leases, answer ARP, and the device list shows a
fresh `IpcPrivatePort` and a current `commTime`, then they are reaching the
cloud and the network is fine — the camera registered but never started its
session daemon, so it answers no hello and publishes nothing to the relay.
That is a device-side failure; nothing in this bridge can open a session the
camera never offers. See `DECOMPILATION_REPORT.md` §5 and §7.

**A camera streams but `online` is `false` (or `media_free` is `true`).**
The bridge streams while it receives RTP regardless of these flags. They are
the cloud's opinion — we've observed cameras actively streaming while the
cloud marks them offline, and cameras re-registering with the cloud while
never opening a media socket. Treat the flags as diagnostics, not the bridge's
source of truth. `media_free` is the app's own `mediaState == "0"` test and
means "no session on the cloud's books", which is not the same as "not
streaming to us".

**Everything stops at once, all cameras dead.**
Almost certainly an expired JWT. Re-run `--list-cameras`; an HTTP 401 confirms
it. Capture a fresh token. `--offline` is token-gated on these cameras too.

**`Address already in use` on startup.**
A previous instance is still holding the port:
```bash
pkill -f "[z]iot_rtp_bridge.py"
```
(The `[z]` bracket prevents the pattern from matching its own command line.)

**Nothing streams from a container.**
NAT. The camera cannot route to a container-private address. Use host
networking (`--network host` in Docker, or run on the host directly).

**Live view in Frigate shows "offline" but thumbnails work.**
This is usually a WebRTC issue. If you're accessing via Cloudflare tunnel,
WebRTC won't work without TURN. Frigate falls back to MSE. With the shipped
`go2rtc.yaml` (1 s GOP) that starts in about a second; the old default
`-g 50` needed 5–10 s. Or open the go2rtc UI and pick MJPEG — that is the
camera's JPEG, no transcode. You can verify by checking
`http://<host>:1984/api/streams` for active consumers.

**ffmpeg logs `overread 8`.**
Cosmetic. The camera's entropy data has a few trailing bytes ffmpeg's MJPEG
decoder is strict about; every frame still decodes.

**Colors look washed out / tinted.**
The sensor, not the decoder. The camera's chroma quantization table is pinned
near 241 of 255, so it discards most color information. Luma detail is fine.

---

## How it works

1. `GET /api/v1/ipc?terminalFamilyId=<user_id>` — enumerate cameras.
   `--offline` skips this: the roster is `offline_endpoints` (LAN IP,
   optional port). Signalling below still runs when a token is present.
2. `GET /api/v1/ipc/send-stun-addr?...` — register **the exact UDP port the
   camera will send RTP to**.
3. `GET /api/v1/ipc/notify-live-event?eventType=1` — wake the camera.
   `eventType` is a `CameraEventType`: `0` keepAlive, `1` start, `2` pause,
   `3` stop, `4` connected, `5` relay.
4. `GET /api/v1/ipc/stun-addr/<uid>` — read the camera's address. The reply
   carries **both** a LAN pair (`IpcPrivateIP:IpcPrivatePort`) and a public one
   (`IpcPublicIP:IpcPublicPort`), plus a `seqNo`. The bridge picks the LAN pair
   when the camera shares its `/24` and the public pair otherwise — the same
   rule as the app's `DeviceStunItem.ipAddress` — and ignores any reply whose
   `seqNo` went backwards. Under `--offline` the configured LAN IP is pinned
   and only the STUN private port is taken (`endpoint_kind: static-lan`).
5. Send the literal UDP bytes `App send hello` to that address (plus
   `App send heart for stun` every fifth punch, as the app does). On this
   firmware that keeps NAT warm; it does not start the session.
6. The camera streams plain RTP **from its LAN socket to the registered
   viewer address**: **PT 26** = JPEG (RFC 2435), 640×480 ~6-8 fps;
   **PT 0** = PCMU G.711 audio, 8 kHz mono. `/cam/<uid>` and `/audio/<uid>`
   are that RTP re-served locally — not a pull from the vendor relay.
7. `notify-live-event?eventType=0` (keepAlive) every 2 s keeps it alive;
   `eventType=3` (stop) is sent on shutdown.

While `onlineState=0` the bridge reannounces the same local UDP port
instead of rebinding — a sleeping battery camera checks in for a few
seconds, and a new source port is a miss. When the flag flips 0→1,
rendezvous runs immediately instead of waiting out the 60 s backoff.

**There is no "start live" command.** An earlier version of this bridge sent
`POST /api/v1/cmd/send-cmd {"cmdType":"20"}` before the rendezvous, believing it
started the session. Decompiling the app showed `20` is `speakOff` — the
`CameraCMDType` enum has no live-view member at all. That call is gone; live view
is the rendezvous above and nothing more.

**Relay fallback.** After three failed direct rendezvous the bridge sends
`notify-live-event?eventType=5` (relay) and plays the vendor's forwarding server
over RTSP — `rtsp://<relay_ip>/live/<uid>` or `rtsp://<relay_ip>/rtp/<last 8 of
uid>`, whichever answers — with RTP interleaved on the RTSP TCP connection. It
retries the direct path every two minutes and returns to it as soon as that
works. `--force-relay` takes this path immediately, for testing. `/health`
reports `mode` — `direct`, `relay`, or `down` when no transport is open — and
`relay_url`.

In practice the relay has never served media for these cameras: it answers
`OPTIONS` promptly and then stalls on `DESCRIBE`, which is a ZLMediaKit
publisher-wait. The relay flow is camera → relay → viewer, and these units
never publish. Treat the fallback as untested rather than working.

**The bridge does not exit when cameras are down.** A camera that fails its
first rendezvous is kept and retried in the background, and the HTTP server
comes up even if nothing is streaming, so `/health` stays readable and the
watchdog can tell "bridge dead" from "cameras dead". These cameras spend a lot
of time cloud-offline; a bridge that gave up at startup would stay down until
someone noticed.

Cloud device-list state (`onlineState`, `mediaState`, `relay_ip`, `commTime`)
is refreshed every 30 s by a single account-wide poller (every 5 s while any
camera is cloud-offline, so a battery camera's short awake window is not
missed) — the device-list endpoint returns every camera, so one request
serves all of them — and exposed on `/`, `/health`, and `--list-cameras`.
`/health` also carries `cloud_age_s`, the age of those flags; if the poller
starts failing (an expired JWT, say) the last-known values are kept,
`cloud_age_s` climbs, and the log warns after three consecutive failures. A
large `cloud_age_s` means the flags are stale, not that the camera is unwell.

Alongside the raw flags, `/` and `/health` carry `online` and `media_free`,
which are the app's own readings of them: `online` is `onlineState == "1"` and
`media_free` is `mediaState == "0"`. The app makes no finer distinction — see
*Protocol facts* below.

Notes for anyone modifying the media path:

* **The keepalive is mandatory.** Stop it and the camera stops sending
  entirely — punching alone will not sustain the stream.
* **The camera binds a new UDP port for every session**, and `stun-addr` may
  briefly report the previous one. Punch a stale port and you receive nothing,
  permanently. Hence the re-resolve-when-starved logic — floored to one lookup
  per 5 s, since `stun-addr` blocks for up to 10 s and the punch loop would
  otherwise ask on every tick without media.
* **RFC 2435 traps:** type `0x41` ≥ 64 means a 4-byte Restart Marker header
  follows the 8-byte main header — *on every fragment*, not just the first.
  Quantization tables then sit at payload offset 16–144 on fragment 0, and the
  rebuilt JPEG needs a DRI segment (interval 40) or the decoder desyncs.
* **RTP SSRC encodes the UID** — the last 8 decimal digits
  (`141030191094` → SSRC `0x30191094`). This is load-bearing: it is what the
  direct path accepts media on. **Do not filter on the source address**
  instead — the address the broker reports and the address the camera sends
  from routinely disagree (per-session rebind, a LAN wider than the /24 the
  endpoint choice assumes, symmetric NAT), and dropping on that mismatch
  strands a stream that is working. A UID that is not 8+ decimal digits yields
  no SSRC; those cameras log a warning at startup and accept any RTP on their
  own socket.
* There is **no encryption and no obfuscation** anywhere in the media path.

---

## Protocol facts from the decompiled app (2026-08-30)

The vendor app (`com.flu.flutter_wifi_camera` 1.13.0, versionCode 100354) was
decompiled with Blutter — the Dart AOT object pool and annotated assembly, not
just strings. Full write-up and evidence in **`DECOMPILATION_REPORT.md`**.

* API base in the app is `http://ipc.gps555.net/api` (TLS omitted in-app; the
  bridge uses `https://ipc.gps555.net/api`).
* Live view is `send-stun-addr` → `notify-live-event(start)` → `stun-addr` →
  plaintext UDP hello. **No `send-cmd` is involved** — see *How it works*.
* `CameraCMDType` (`POST /v1/cmd/send-cmd`) is a device *control* enum:
  `restart` 1, `restore` 2, `light` 3, `sdCard` 4, `formatSDCard` 5,
  `firmwareOTA` 6, `infraredLight` 7, `originHorizontal` 8, `originVertical` 9,
  `ptzUp` 10 … `ptzMoveStop` 18, `speakOn` 19, `speakOff` 20, `lampLight` 21,
  `definition` 22, `ptzReset` 23, `sensitivity` 25. There is no live-view
  member. `GPS555.send_cmd()` exposes these; nothing calls it automatically.
* `CameraEventType` (`notify-live-event?eventType=`) is `keepAlive` 0,
  `start` 1, `pause` 2, `stop` 3, `connected` 4, `relay` 5.
* `ws://ws.gps555.net:7080/ws` is a status-push websocket the app opens; the
  bridge polls the REST device list instead. There is a second websocket on
  `:7090`, a signalling server at `sig.gps555.net:8882`, and a complete
  alternate backend at `gps666.net` — none of which the bridge uses.
* `stunaddr.gps555.net:13478` (`8.130.23.234:13478`) is a TURN-like NAT punch
  relay. The bridge does not traverse it.
* The `relay_ip` field (e.g. `156.246.16.114:554`) **is** a real media
  endpoint — an earlier version of this file said it was not. The path matters:
  `rtsp://<relay_ip>/live/<uid>` or `rtsp://<relay_ip>/rtp/<last 8 of uid>`,
  chosen by a firmware gate. Probing the server root returns an empty
  `DESCRIBE`, which is what led to the wrong conclusion.
* `onlineState` and `mediaState` are strings the app reduces to two booleans and
  nothing more: `isOn` is `onlineState == "1"`, `isFree` is
  `mediaState == "0"`. The non-zero values — `1`, `2` and `3` have all been
  seen — are **not** distinguished anywhere in the app, so read nothing into
  which one appears; they all just mean "busy".
* `connectionState` is `"BR"` on these cameras and no app code parses it. It is
  unrelated to `BmConnectionStateEnum`, which is Bluetooth.
* `libzlmediakit_jni.so` ships for arm64-v8a and armeabi-v7a but **not** x86_64,
  which is why Waydroid on x86_64 cannot run live view.
* There is no encryption or obfuscation anywhere in the protocol.

---

## What this adds over the original

The base bridge was reverse-engineered and submitted by the user. These
reliability improvements were added during deployment:

* **`/health` endpoint** — per-camera fps, frame count, endpoint moves,
  streaming status. For Docker HEALTHCHECK and watchdog scripts.
* **FPS tracking** — rolling 5-second window, logged every 30s for diagnostics.
* **Faster re-resolve** — 2s starvation threshold (was 4s). Detects endpoint
  changes faster.
* **Re-punch on endpoint move** — immediately re-punches after re-resolving a
  moved endpoint, not just at the next interval.
* **Watchdog script** (`ziot_watchdog.py`) — checks bridge + go2rtc health,
  auto-restarts Frigate when a stream is dead **while that camera is live**.
  An asleep battery camera is idle, not a Frigate restart. Reports a camera
  that has never received RTP, and flags a camera running on the relay.
* **Docker support** — Dockerfile included, tested with `--network host`.
* **Cloud online/media state** — device-list `onlineState`/`mediaState` shown
  in `--list-cameras`, `/`, and `/health`; refreshed every 30 s so a wedged
  camera is visible, with `cloud_age_s` so stale flags are visible as stale.
  Optional `only_online` config skips offline cameras at startup.
* **`App send heart for stun`** alongside the hello, as the app does.
* **Corrections from the Dart-level decompilation** (v3) — removed the
  `send-cmd` "20" call, which was `speakOff` rather than a wake step; named the
  `CameraEventType` values and send `stop` on shutdown instead of `keepAlive`;
  pick the LAN or public endpoint by subnet as the app does; reject STUN replies
  with a stale `seqNo`; report `online`/`media_free` using the app's own tests.
* **RTSP relay fallback** — plays the vendor forwarding server when the direct
  path stays down, and returns to direct as soon as it recovers.
* **`--offline`** — roster from `offline_endpoints` instead of the device
  list; a token in the same config still does notify/keepalive (required
  on TXW817). Pins the LAN IP and follows the STUN private port. Punch-only
  entries accept an IP only: the rendezvous socket itself hello-sweeps the
  ephemeral range, and the camera's RTP identifies the port.
* **No-rebind recovery** — while a camera is cloud-offline the same local
  UDP port is reannounced instead of rebound (a 176-rendezvous loop used
  to close the socket the camera would send to). Punch-only cameras
  hello-sweep from the existing socket, at rendezvous and on recovery
  alike; SSRC learning accepts only RTP v2 with PT 0/26, so a hello echo
  cannot poison it.
* **Config errors that retrying cannot fix** — a token-only config
  (no `user_id` in the file or the JWT) used to surface as a `KeyError`
  the boot retry loop caught and repeated forever as a fake cloud outage;
  it now exits 2 naming the fix, and `--list-cameras` says so instead of
  tracebacking. A failed attempt after a wake keeps its backoff: the wake
  sets `_backoff` to 0 for its one-shot bypass, and doubling from 0 had
  pinned recovery at zero interval.
* **Wake on 0→1** — full rendezvous the moment `onlineState` flips, rather
  than sitting in a 60 s backoff through a battery camera's awake window.
* **Probe requires RTP v2 PT 0/26** — an echo of `App send hello` is not a
  hit.
* **`go2rtc.yaml`** — native MJPEG producer (instant live), H.264 only for
  Frigate/WebRTC with a 1 s GOP, AAC+Opus audio. Watchdog matches streams
  by UID in the producer URL.

---

## Limitations
* **Cloud-dependent at runtime.** The 2-second `notify(keepAlive)` is required
  for as long as you want the stream on these cameras — the session dies in
  ~12 s without it. Media is local RTP from the camera; control is not.
  `--offline` only skips the device list; it still uses the token for
  signalling when one is present.
* **Sleeping battery cameras** cannot be woken from the LAN. `onlineState=0`
  and no ARP means the radio is off; the bridge waits for the next vendor
  check-in.
* **Token expiry ~15 days**, manual re-capture. This is the main operational
  chore (including `--offline`).
* **Hardware ceiling: 640×480, ~6-8 fps, MJPEG.** The TXW817 has no hardware
  H.264 encoder. No amount of software gets 1080p out of it, regardless of
  what the listing claimed.
* **Audio is quiet** — a low-gain electret on a cheap board. It decodes
  correctly; there is simply not much level.
* **H.264 is a transcode.** The camera has no encoder; go2rtc (or Frigate)
  must make it. Native MJPEG live view is instant; the shipped `go2rtc.yaml`
  uses a 1 s GOP so WebRTC/MSE start in about a second instead of 5–10 s.
* Untested beyond three cameras on one host.

---

## License

MIT

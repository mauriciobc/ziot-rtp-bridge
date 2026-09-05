# ziot_rtp_bridge

Local MJPEG + audio bridge for **ZIOT / gps555.net** IP cameras (Taixin
TXW817, sold as "X5" / "A9"), so they can be consumed by go2rtc, Frigate,
Home Assistant, or a plain browser instead of the vendor phone app.

Verified working on hardware 2026-08-29 with three cameras streaming
simultaneously (video + audio), firmware `TXW817_A_V1.0.12.32`.

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

What **does** work: perform the vendor cloud's rendezvous yourself, then send
the camera a plaintext UDP hello. It replies by streaming **unencrypted RTP**
directly to your socket. This script does that and re-serves it as MJPEG.

The `relay_ip` server *is* usable, but only at the app's own paths —
`rtsp://<relay_ip>/live/<uid>` or `/rtp/<last 8 of uid>`, not the root. The
bridge falls back to it automatically when the direct path stays down.

> **Media flows LAN-direct.** The cloud is used only to wake the camera and
> learn its address — video and audio never traverse the vendor's relay.
> It is *local media*, but not *cloud-independent*; see Limitations.

---

## Requirements

* **Python 3.10+**, standard library only. No `pip install`.
* The host **must be on the same LAN/subnet as the cameras.** This is a hard
  requirement, not a preference — the bridge talks straight to the camera's
  private IP. A NAT'd container (Docker bridge networking, Waydroid, a VM on
  NAT) will register an unroutable address and receive nothing.
  → Run on the host network, or use `network_mode: host`.
* A **valid account JWT** (see Configuration).
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
  "cameras": []
}
```

| Key | Required | Meaning |
|---|---|---|
| `token` | yes | Account JWT, sent as `Authorization: Bearer …` |
| `user_id` | yes | Numeric account id (`user_id` claim inside the JWT) |
| `port` | no | HTTP listen port, default `8085` |
| `cameras` | no | Allow-list of UIDs. `[]` or omitted = every camera on the account |
| `only_online` | no | Only start cameras the app would call online (`onlineState == "1"`). Checked **once at startup** — a camera that is offline then stays skipped until you restart. Default `false` — cloud statuses fluctuate, so the bridge normally tries every camera and reports state |
| `punch_interval` | no | Seconds between `App send hello` packets. Default `1.0`, matching the app; must be a finite value strictly greater than 0 and strictly below the 2 s starvation threshold. Anything else is logged as an error and replaced with the default — a bad value never stops the bridge from booting |
| `relay_user` / `relay_pass` | no | Credentials for the RTSP relay, if it ever demands them. Unset by default — the relay is not known to authenticate, and the bridge fails loudly rather than guessing |

```bash
chmod 600 ziot_config.json    # it holds an account credential
```

**Startup failures are deliberately two-tier.** The rule is whether retrying
could ever help.

*Fatal, exits `2`:* a config the bridge cannot read (nothing to run), and a
token the cloud rejects on the very first call (no token un-expires itself, and
nothing is serving yet, so this is a config error like any other). Both name
what to fix.

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
rendezvous is entirely token-gated. Re-capture and restart. There is no
refresh-token flow implemented here.

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
| `--list-cameras` | print cameras on the account and exit |
| `--bind-ip IP` | LAN IP to stream from. Set this on multi-homed hosts |

Cameras start in parallel; expect all of them live within ~10 s.

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
      "endpoint_kind": "private",
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
      "media_source": "192.168.1.73:49201",
      "endpoint_moves": 2,
      "last_rx_ago_s": 0.1,
      "addr": "192.168.1.73:49201"
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

`addr` is where the STUN broker says the camera is — where punches and hellos
go. `media_source` is where media actually arrives from. They normally match;
a lasting disagreement means the broker's answer is stale and the direct path
is one-way. `foreign_ssrc` counts datagrams dropped for carrying another
camera's SSRC, `rx_errors` counts packets whose handling raised.

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

The cameras emit MJPEG, which Frigate cannot record to mp4 — so transcode to
h264 in go2rtc. Video and audio are separate URLs; listing both under one
stream name makes go2rtc merge the tracks.

```yaml
streams:
  cat_cam_1:
    - "ffmpeg:http://127.0.0.1:8085/cam/141030191094#video=h264"
    - "ffmpeg:http://127.0.0.1:8085/audio/141030191094#audio=aac"
  cat_cam_2:
    - "ffmpeg:http://127.0.0.1:8085/cam/140979857781#video=h264"
    - "ffmpeg:http://127.0.0.1:8085/audio/140979857781#audio=aac"
  cat_cam_3:
    - "ffmpeg:http://127.0.0.1:8085/cam/140996632758#video=h264"
    - "ffmpeg:http://127.0.0.1:8085/audio/140996632758#audio=aac"
```

If the bridge runs outside the go2rtc container, replace `127.0.0.1` with the
host IP — and remember the bridge itself still needs host networking.

### WebRTC (optional, LAN only)

For lower-latency live view, add WebRTC candidates to `go2rtc.yaml`:

```yaml
webrtc:
  candidates:
    - "stun:192.168.1.100:8555"
    - "stun:192.168.1.100:3478"
```

WebRTC does not work through Cloudflare tunnels without TURN — MSE fallback
is used automatically.

---

## Frigate

Consume the go2rtc restream. Match `detect` to the camera's real output
(**640×480**); claiming more resolution than exists only wastes CPU.

```yaml
go2rtc:
  streams:
    cat_cam_1:
      - "ffmpeg:http://127.0.0.1:8085/cam/141030191094#video=h264"
      - "ffmpeg:http://127.0.0.1:8085/audio/141030191094#audio=aac"

cameras:
  cat_cam_1:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/cat_cam_1
          input_args: preset-rtsp-restream
          roles: [detect, record]
    detect:
      width: 640
      height: 480
      fps: 5
```

The cameras deliver ~6-8 fps; setting `detect.fps` above that gains nothing.

**Note:** Frigate's `go2rtc:` config section configures Frigate's internal
go2rtc instance. If go2rtc is deployed separately, put the `streams:` section
in its own `go2rtc.yaml` instead.

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
subnet. The bridge re-resolves a starved camera's endpoint automatically once
media has been absent for 2 s, at most once every 5 s — watch for
`endpoint moved …` lines, which are expected and healthy.

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
it. Capture a fresh token.

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
WebRTC won't work without TURN. Frigate falls back to MSE, which takes
~5-10s to buffer. The stream is working — just slow to start. You can verify
by checking `http://<host>:1984/api/streams` for active consumers.

**ffmpeg logs `overread 8`.**
Cosmetic. The camera's entropy data has a few trailing bytes ffmpeg's MJPEG
decoder is strict about; every frame still decodes.

**Colors look washed out / tinted.**
The sensor, not the decoder. The camera's chroma quantization table is pinned
near 241 of 255, so it discards most color information. Luma detail is fine.

---

## How it works

1. `GET /api/v1/ipc?terminalFamilyId=<user_id>` — enumerate cameras.
2. `GET /api/v1/ipc/send-stun-addr?...` — register **the exact UDP port the
   bridge will stream from**.
3. `GET /api/v1/ipc/notify-live-event?eventType=1` — wake the camera.
   `eventType` is a `CameraEventType`: `0` keepAlive, `1` start, `2` pause,
   `3` stop, `4` connected, `5` relay.
4. `GET /api/v1/ipc/stun-addr/<uid>` — read the camera's address. The reply
   carries **both** a LAN pair (`IpcPrivateIP:IpcPrivatePort`) and a public one
   (`IpcPublicIP:IpcPublicPort`), plus a `seqNo`. The bridge picks the LAN pair
   when the camera shares its `/24` and the public pair otherwise — the same
   rule as the app's `DeviceStunItem.ipAddress` — and ignores any reply whose
   `seqNo` went backwards.
5. Send the literal UDP bytes `App send hello` to that address (plus
   `App send heart for stun` every fifth punch, as the app does).
6. The camera streams plain RTP back: **PT 26** = JPEG (RFC 2435), 640×480
   ~6-8 fps; **PT 0** = PCMU G.711 audio, 8 kHz mono.
7. `notify-live-event?eventType=0` (keepAlive) every 2 s keeps it alive;
   `eventType=3` (stop) is sent on shutdown.

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
is refreshed every 30 s by a single account-wide poller — the device-list
endpoint returns every camera, so one request serves all of them — and exposed
on `/`, `/health`, and `--list-cameras`. `/health` also carries `cloud_age_s`,
the age of those flags; if the poller starts failing (an expired JWT, say) the
last-known values are kept, `cloud_age_s` climbs, and the log warns after three
consecutive failures. A large `cloud_age_s` means the flags are stale, not that
the camera is unwell.

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
  auto-restarts Frigate when streams die but bridge is alive. Reports a camera
  that has never received RTP (previously silent), and flags a camera running
  on the relay rather than directly.
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

---

## Limitations

* **Cloud-dependent at runtime.** The 2-second keepalive is required for as
  long as you want the stream, so this stops working if the vendor shuts down
  `ipc.gps555.net`. Media is local; control is not.
* **Token expiry ~15 days**, manual re-capture. This is the main operational
  chore.
* **Hardware ceiling: 640×480, ~6-8 fps, MJPEG.** The TXW817 has no hardware
  H.264 encoder. No amount of software gets 1080p out of it, regardless of
  what the listing claimed.
* **Audio is quiet** — a low-gain electret on a cheap board. It decodes
  correctly; there is simply not much level.
* **Live view startup delay** — go2rtc needs ~5-10s to buffer keyframes when
  transcoding MJPEG→H264. Thumbnails are instant.
* Untested beyond three cameras on one host.

---

## License

MIT

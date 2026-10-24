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
| `http_host` | no | Interface the HTTP server binds. Default `0.0.0.0` — set it to the LAN IP (e.g. `192.168.18.6`) to keep the streams off other interfaces |
| `http_token` | no | When set, every route except `/health` requires `Authorization: Bearer <token>` or `?token=<token>` (the query form exists for `<img>`/`<audio>` tags, which cannot set headers). `/health` stays open on purpose: the watchdog polls it unauthenticated |
| `cameras` | no | Allow-list of UIDs. `[]` or omitted = every camera on the account (or every key in `offline_endpoints` when `--offline`) |
| `only_online` | no | Only start cameras the app would call online (`onlineState == "1"`). Checked **once at startup** — a camera that is offline then stays skipped until you restart. Default `false` — cloud statuses fluctuate, so the bridge normally tries every camera and reports state. Ignored under `--offline` |
| `punch_interval` | no | Seconds between `App send hello` packets. Default `1.0`, matching the app; must be a finite value strictly greater than 0 and strictly below the 2 s starvation threshold. Anything else is logged as an error and replaced with the default — a bad value never stops the bridge from booting |
| `relay_user` / `relay_pass` | no | Credentials for the RTSP relay, if it ever demands them. Basic and Digest are both handled; a cached digest challenge is re-derived per request (method and URI are hashed into the response, so it cannot be replayed across requests) |
| `offline` / `offline_endpoints` | no | Roster from LAN targets instead of `GET /v1/ipc`: `{"offline": true, "offline_endpoints": {"<uid>": "192.168.18.75"}}`. With a token, IP only is enough — the listen port rotates and comes from STUN. Punch-only (no token), IP only makes the bridge hello-sweep the ephemeral range from its own socket; `ip:port` from a probe HIT skips the sweep. A token in the same file still does notify/keepalive. See "Running without the device list" |
| `api_base` | no | Base URL of the cloud API. Default `https://ipc.gps555.net/api`. Point it at a local broker (`http://192.168.68.114:80/api`) to keep the rendezvous off the vendor cloud — the media is LAN-local either way; only the signalling moves. See "Local cloud emulation" |

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
port is a miss when it next checks in) and sends `notify(start)`. The
reannounce interval grows 5 → 10 → 20 → 40 → 60 s (capped) — a sleeping
camera checks in rarely, and re-registering every 5 s for hours is cloud
chatter with nothing to catch. When `onlineState` flips 0→1, rendezvous
runs immediately instead of waiting out the grown backoff. Device-list
flags are polled every 5 s while any camera is cloud-offline (otherwise
30 s), and that poll — not the reannounce — is what catches the camera's
short awake window.

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

A silent sweep on this firmware is normal, not a probe bug. To learn
*which* cameras are even powered before sweeping IPs, use Bluetooth:
an awake camera advertises BLE as `ZIOTA_<uid>` for a short window
(verified live 2026-10-22 on the Tuya-module TXW817: service UUID
`0xa201`, e.g. `ZIOTA_140979857781`; it stops advertising after a
connection or about a minute, so a silent scan does not mean the camera
is off — sweep the IP anyway). The advertisement carries no LAN IP, but
the firmware associates with the **reversed BLE address** as its WiFi
MAC (`E0:44:40:60:68:82` → `82:68:60:40:44:e0`), so `--ble` resolves
the LAN IP straight from the ARP table when the kernel has seen the
camera. Needs BlueZ's `bluetoothctl`:

```bash
python3 ziot_offline_probe.py --ble            # list powered cameras by UID + LAN IP
python3 ziot_offline_probe.py --ble 192.168.68.107 --uid 140979857781
```

### Local cloud emulation (the vendor cloud, locally)

`ziot_fake_cloud.py` is the start of removing the vendor cloud (goal and
findings: `DECOMPILATION_REPORT.md` §8–§14). Three things work today:

* **The HTTP API is answered locally.** Response shapes come from a live
  app capture (§11). Register a camera and point the bridge at the broker:

  ```bash
  python3 ziot_fake_cloud.py --http 8089 --sig 0 --stun 0 --ws 0 --dns 0 \
      --camera 141030191094=192.168.68.50:34567
  # ziot_config.json: "api_base": "http://127.0.0.1:8089/api"
  ```

  Verified: the bridge lists the camera, registers its UDP port, wakes it
  and resolves the camera's address entirely from the local broker. What is
  *not* verified is a real camera streaming against it.
* **The camera can be routed to this host without touching the camera.**
  The cloud addresses are fixed (`ipc.gps555.net` → `120.26.121.165`,
  `sig.gps555.net` → `47.110.77.193`, `stun.gps555.net` and
  `ws.gps555.net` → `47.238.125.111`, `stunaddr.gps555.net` →
  `8.130.23.234`), so a static route in the router sends the camera's
  cloud traffic here — no DNS, no camera configuration, no BLE.
  **Verified live (2026-10-23):** with the four routes in place, both
  cameras on the LAN connect to the broker's signalling port on their
  own and send their registration, and the phone's app — pointed at the
  broker by the same routes — asks the broker for the device list and
  registers where it wants the media. The vendor is unreachable from
  this network the whole time.
* **The camera→cloud registration frame is known, byte for byte.** It is
  56 bytes:

  ```
  86 86 | cmd(2) | len(2) | seq(2) | uidLen(2) | uid(12) | body(32) | xor | 0x0D
        0001     002c     0019..    000c        "140979857781"  "TXW817_A_V1.0.12.32"
  ```

  `cmd` 1 is the camera's hello to the signalling server, `len` (44) is
  the uid plus the body, `seq` is the camera's counter and increments on
  every retransmission, and the body is the firmware version, NUL-padded.
  The XOR covers every byte before it and `0x0D` closes the frame — the
  same shape as the camera's BLE frame. `ziot_fake_cloud.py` rebuilds a
  captured frame byte-for-byte, and
  `tests/test_ziot_fake_cloud.py::SignallingFrameTests` pins it.
* **The camera's STUN calls now land on this host.** `stunaddr.gps555.net`
  is one of the routed addresses, so the camera's "what is my address?"
  call arrives at the broker's STUN listener (port 13478) and is
  answered there. This is the route doing the work, not an instruction
  the camera followed.
* **The camera registers with the broker and gets past registration.**
  The broker now answers the camera's registration with **the vendor's
  own answer**, captured live from `sig.gps555.net` on 2026-10-23 and
  reproduced byte for byte (`vendor_registration_answer()`, pinned by
  `SignallingFrameTests`). The body is flat fixed-width fields — a u16
  lead, the relay address in a 20-byte ASCII slot with two ports, the
  STUN host in a 32-byte slot with its port, and the signalling address
  in a 32-byte slot with its port — and every address in it points at
  this host. The camera accepted it and moved on: it stopped
  retransmitting `cmd 0x0001` and started sending `cmd 0x0021`.
* **The whole camera handshake now completes against the broker.** The
  camera sends, and the broker answers with the vendor's exact bytes:

  | camera asks | the broker answers | what it is |
  |---|---|---|
  | `cmd 0x0001` (register) | `cmd 0x8000` | the addresses: relay, STUN, signalling |
  | `cmd 0x0021` (clock) | `cmd 0x8021` | the server's time, a big-endian u32 |
  | `cmd 0x0002` (pullVideo) | `cmd 0x8001` | "go ahead" |
  | `cmd 0x0004` (status) | `cmd 0x8001` | the queried value |
  | `cmd 0x1601` (config) | `cmd 0x8601` | the ports to use |

  Every one of those answers was captured from the real
  `sig.gps555.net` on 2026-10-23 and is pinned byte-for-byte by
  `SignallingFrameTests`. The camera then calls the broker's STUN server
  and the broker learns its media address from that call.
* **The camera's media address is learned from its own STUN call.** The
  camera's Binding Request comes from the port its media socket listens
  on, so the broker records the camera's address that way — the same way
  the vendor's cloud learns it — and the bridge reads it from the
  broker's `stun-addr`.
* **The camera's firmware is in hand.** The vendor publishes a firmware
  catalogue; the camera's own build (`TXW817_A_V1.0.12.32`, in a Taixin
  XIPH container) was downloaded and its strings now describe the
  protocol in the camera's own words — the BLE provisioning frame, the
  camera↔signalling-server protocol (`CUSTOM_PROTOCOL_*`), and the
  stored-parameter lever that lets the signalling server be pointed
  elsewhere. The code region is **Thumb**, and the camera's 40 command
  handlers are reachable from the dispatch table at payload `0x80` — an
  earlier claim that the code "does not decode in any standard
  instruction set" was an artefact of sweeping from the exception table
  instead of from the code. Details in `ziot-fw/REPORT.md` §2.5–§2.6,
  findings in `DECOMPILATION_REPORT.md` §13–§14.

#### The camera pushes UDP when told to; it is not an RTSP publisher

The signalling protocol recovered from the firmware — now written down as
the source of truth, `ziot-fw/SIGNALLING_PROTOCOL.md` — settles the media
path: the camera sends its RTP **out over plain UDP (`sendto`)** to an
address the signalling server gives it. Its strings name the flow —
`spook_rtsp_set_relay_info` (arms the target, **never connects**: §5a of
the doc), `start push, session id:%s`, `relaySession:%s`,
`rtsp://%s:%d/webcam/jpeg_dvp` (the camera's own server path),
`m=video %d RTP/AVP 26` (JPEG) and `m=audio %d RTP/AVP 0` (PCMU). The
verbs `ANNOUNCE`/`RECORD` are **absent from the firmware** (0 grep hits),
so there is no RTSP client leg: `rtsp_p2p_start` builds a URL and then
calls `connect_client_udp_endpoint`, which opens a **UDP RTP socket** and
pushes JPEG (RTP/AVP 26) + PCMU (RTP/AVP 0) — the same RTP as the direct
path, aimed at the armed address.

The vendor's registration answer agrees: the `0x8000` reply carries fixed
arm slots — a 20-byte ASCII IP, a big-endian u16 port at offset 0x18, and
a STUN host + port — which the camera stores (state 3). It does **not**
connect yet. A second, unsolicited **`cmd 0x8005`** (empty body) is the
start signal: it flips the relay-start flag (`dev->0xc8 = 2`) and a state
machine then opens the UDP push toward the armed address (§6–§7 of the
doc). The broker's 462-candidate sweep never tried `0x8005` because it
drew ids from the camera's local AP command space, not the `0x80xx`
signalling space (§8 of the doc).

So the armed receiver must be a **UDP RTP sink** — and the bridge's own
media socket already is one. The broker's `arm_target` puts the bridge's
UDP address (registered viewer first, the viewer's public pair second,
the configured `--relay-port` only when nothing else is known, as an
experiment) into the arm slots and skips the RTSP relay for ingest (§9.3
of the doc). `ziot_relay.py` (RTSP on 8554/8555), built to accept an
`ANNOUNCE`/`RECORD`, is kept only as an experiment: the camera never
speaks RTSP on this leg. The round trip still has machinery behind it —
a synthetic publisher announces, records and streams, and the
**production** `RelayStream` of the bridge opens the relay, plays it and
reassembles the JPEGs (`tests/test_ziot_relay.py`) — but it now
exercises only that experiment path.

> **Update 2026-10-24 — the trigger is recovered.** The camera's firmware
> was disassembled (it is a **C-SKY CK803**, which is why no prior tool
> decoded it). The media path is two server→camera frames: `cmd 0x8000`
> *arms* the UDP target, and an unsolicited **`cmd 0x8005`** (empty body)
> *starts the push* (`CUSTOM_TASK_RelayPro`). Full spec and firmware
> evidence: **`ziot-fw/SIGNALLING_PROTOCOL.md`**.
>
> **State of the code.** The broker now echoes the register answer's
> `seq` byte, arms the bridge's UDP media socket through `arm_target`,
> and builds the `0x8005` trigger frame (`push_relay_trigger`) — but
> holds it behind `--relay-start`, **off by default**: the first live
> test that fired it after `0x8000` and on every keepalive tick made the
> camera reconnect-loop, never reach the connected state, and send zero
> media. With `--relay-start` the fire is no longer immediate either:
> it is scheduled (`RELAY_START_DELAY_S`) one per armed camera, sent next
> keepalive-tick by the broker, and discarded if that camera drops
> before it is due. The trigger stays off until a capture shows the
> camera settling into a session and pushing to the armed address; the
> validation numbers are a `tcpdump` on the armed port and the bridge's
> `rx_datagrams`. Probe note (vendor wiretap, 2026-10-24): re-registering
> every ~15-20 s is the camera's *normal* rhythm — a short transaction
> (connect → register → the four answers → self-close after ~6 s), not a
> fault state; what is *not* normal is the silent-probe limbo that follows a
> session dying mid-cycle (`ziot-fw/SIGNALLING_PROTOCOL.md` §0 holds the
> full findings).

> **Update 2026-10-24 (evening) — why nothing streams against the broker.**
> Findings only; the code has not been changed yet.
>
> * **The camera rejects every STUN answer the broker sends.** Its STUN
>   client requires a standard XOR-MAPPED-ADDRESS (attribute `0x0020`). The
>   broker answers with `0x0001`/`0x0004`/`0x8020`, the shape the vendor
>   gives the *phone*.
> * **So every cycle fails the same way.** The camera retries 4 times from
>   new random ports (50001–59999), gives up, and closes its sig socket.
>   That is the ~6 s "short transaction" described above. It never sends
>   the `0x0003` address report that a working STUN produces, and there
>   are zero of them in any capture.
> * **The direct live view is a different pair of frames:** `0x8101` (the
>   viewer's address) and `0x8004` (play control, value 1 = start). It is
>   not `0x8005`, which is the relay.
> * **The 13:47 phone capture shows the app's first `eventType=5`.** It
>   also shows the broker's STUN answers leaving from the wrong source
>   address, so they never reach the phone.
> * **The app went to relay because the broker's `seqNo` is wrong.** The
>   app waits until `stun-addr` shows the `seqNo` of its own
>   `send-stun-addr`. The broker's shared counter never repeats it. The
>   August vendor captures also show that `IpcPublic*` is the camera's pair,
>   not the viewer's.
> * **The routes should include `47.97.71.82`,** the signalling address the
>   vendor's own `0x8000` and the wiretap use, alongside `47.110.77.193`.
>
> Details and the ordered fix list: `ziot-fw/SIGNALLING_PROTOCOL.md`
> §12–§15.

> **Update 2026-10-24 16:14 — frames, with no vendor cloud.** With the
> `0x0020` STUN attribute and the direct chain in the broker (`0x8101` →
> `0x8004` value 1 → hello punch), camera .107 streams to the bridge at
> 7–8 fps.
>
> * **It stops by itself 36 s after every start.** The firmware's "live
>   heartbeat" is refreshed during play only by the server's `0x8002`, and
>   the broker does not yet turn the bridge's `notify(keepAlive)` into one.
> * **So the stream runs in ~36 s bursts**, each restarted by the bridge's
>   re-rendezvous.
>
> `ziot-fw/SIGNALLING_PROTOCOL.md` §0 (16:14 box), §13.4 and §15 item 6.
>
> **Fixed on `frigate-continuous-stream`, not yet validated live.** The
> broker now forwards each `eventType=0` keepAlive to the camera as
> `0x8002`, so a live view lasts as long as its viewer keeps sending
> keepAlives. The live check: the camera's `0x0002` shows up on sig about
> every 10 s, and no `0x0004` value 0 follows 36 s after a value 1.

`ziot_wiretap.py` was how that link was read: it sits in front of the
vendor's signalling server, forwards the camera's traffic to the real
vendor and logs both directions, so the push that starts a live view
can be read byte for byte — its captures are the evidence behind
`ziot-fw/SIGNALLING_PROTOCOL.md`.


The signalling (8882), STUN (13478) and websocket (7080) listeners now
answer as well as log. The firmware named the protocol
(`CUSTOM_PROTOCOL_ParseData`: `cmd:%04X, len:%d, uidLen:%d`, XOR-checked,
uid-prefixed, with `req cmd` / `cSeq,cmd,ack` / `stun port,ip` /
`relay port,ip` payloads), and the broker implements it on the sig
listener: a register gets the `0x8000` answer, the handshake commands get
their acks, keepalives carry the `0x8001` go-ahead, and the registration
answer arms the bridge's UDP media socket. A DNS hijack mode answers
`*.gps555.net`
with this host and forwards everything else upstream. Ports 53 and 80 bind
as an unprivileged user once the kernel's floor is lowered
(`sudo sysctl -w net.ipv4.ip_unprivileged_port_start=0`; it does not
survive reboot), otherwise they need root; 13478/7080 always bind. The
JWT the broker serves the bridge with is its own (`python3
ziot_fake_cloud.py --mint-token`); the vendor's token is served with a
one-line log warning rather than a rejection, but a clean local setup
mints its own into `ziot_config.json` — back the vendor's up first, it
is the account credential and stays gitignored.

#### Taking the camera off the vendor cloud

Four steps. The host side is scripted; the router side is two taps in the
Deco app.

**1 — Claim the vendor's addresses on this host.** The address must be
*local* here, so the kernel accepts the packet and the reply carries the
vendor's address as its source — a camera that sees a reply from an
unexpected source drops the connection.

```bash
python3 ziot_fake_cloud.py --claim-ips
```

It adds `120.26.121.165/32`, `47.110.77.193/32`, `47.238.125.111/32` and
`8.130.23.234/32` to the interface and prints the route entries. Re-running
it is safe.

The gateway in those entries must not move when an adapter's DHCP lease
changes, so the broker also claims a **fixed address of its own**,
`192.168.68.114`, on the LAN. `ziot-ips.service` does both at boot:

```bash
sudo cp ziot-ips.service /etc/systemd/system/
sudo systemctl enable --now ziot-ips.service
```

**2 — Point the router at this host.** In the Deco app: *More → Advanced →
Static Routing → +*. One entry per address:

| Description | Network destination | Subnet mask | Default gateway | Interface |
|---|---|---|---|---|
| ZIOT ipc | `120.26.121.165` | `255.255.255.255` | `192.168.68.114` | LAN |
| ZIOT sig | `47.110.77.193` | `255.255.255.255` | `192.168.68.114` | LAN |
| ZIOT stun+ws | `47.238.125.111` | `255.255.255.255` | `192.168.68.114` | LAN |
| ZIOT stunaddr | `8.130.23.234` | `255.255.255.255` | `192.168.68.114` | LAN |

The mask is `255.255.255.255` because each entry is a single host. The
gateway is this host, and it must sit on the same subnet as the router
interface that sends the traffic out — hence **LAN**, not WAN: the packets
leave the router's LAN side towards us. The four destinations are public
addresses, so none of them collides with the router's own LAN or WAN.

**3 — Open the host firewall.** The broker's ports must be reachable from the
LAN. If the host runs `firewalld` (as this deployment does), they are **not**
open by default: the interface facing the cameras (`wlan0` here) sits in the
`public` zone, whose default target rejects anything unlisted with `ICMP
admin-prohibited`. The camera's SYN is then dropped before it reaches the
broker — yet the socket still shows `LISTEN` and loopback connects still
work — so the symptom is a camera that **looks asleep**: it retransmits a SYN
every few seconds, never registers, and the broker log stays empty of camera
traffic. Open the four broker ports, runtime and permanent:

```bash
sudo firewall-cmd --zone=public \
  --add-port=80/tcp --add-port=8882/tcp --add-port=7080/tcp --add-port=13478/udp
sudo firewall-cmd --permanent --zone=public \
  --add-port=80/tcp --add-port=8882/tcp --add-port=7080/tcp --add-port=13478/udp
```

Confirm with `firewall-cmd --zone=public --list-ports`. These are the vendor's
ports the camera dials: 80 (API), 8882 (signalling), 13478 (STUN, UDP) and
7080 (websocket) — all four for the full rendezvous, though 8882 alone is
enough to see the camera register. Add `53/tcp`+`53/udp` only if you use the
DNS-hijack path. A different firewall (`nftables`, `ufw`, a cloud security
group) needs the same ports opened. **This was the hidden blocker:** a camera
long written off as an asleep battery unit was in fact knocking on `:8882`
every few seconds and being rejected by the firewall the whole time — check
this before concluding a camera is asleep (`tcpdump -i wlan0 host <cam-ip>`
shows the rejected SYNs and the `admin prohibited` ICMP).

**4 — Run the broker and watch.** The API answers on the vendor's port 80,
the signalling server on 8882, STUN on 13478 and the websocket on 7080. With
the firewall open, a camera on this network registers with the broker —
verified 2026-10-23: two cameras connected to `:8882` and sent their
registration frame (`86 86 | cmd=0001 | len | seq | uidLen | <uid> | firmware
version | XOR | 0d`). That frame is the camera→cloud signalling protocol; the
broker logs it today but does not yet answer it — making it answer is the next
piece of work (`DECOMPILATION_REPORT.md` §13–§14).

```bash
sudo python3 ziot_fake_cloud.py --log fake_cloud.log
```

Running it as a service needs port 80; `AmbientCapabilities=CAP_NET_BIND_SERVICE`
gives an unprivileged service that capability, which keeps the log readable
without running the broker as root. See `ziot-broker.service` in this repo
and `DECOMPILATION_REPORT.md` §11 for the details.

**The broker speaks the vendor's protocol, not an approximation of it.**
Every response shape — the header block, the envelope, the device record's
46 fields and their types, the redirect on the trailing-slash path, the two
different `stun` shapes — was taken from a live app session and is checked
by `VendorConformanceTests`, so the broker and the vendor are
indistinguishable to the app. `DECOMPILATION_REPORT.md` §11 has the full
contract.

Point the bridge at the broker with `api_base`; it can be the vendor's own
address, which is where the camera will look:

```json
{"token": "…", "user_id": 3132031, "api_base": "http://120.26.121.165/api"}
```

Note: this is a *routing* redirect, so the camera keeps its vendor
configuration. It talks to the local broker and nothing else — which is the
point — but it also means a camera that insists on the vendor's behaviour
has not been taught anything new. The alternative, re-provisioning a camera
over BLE with this host as its signalling server, is the cleaner end state.

#### Re-provisioning over BLE — the frame the camera actually wants

The app builds a `wifiConnect` frame (§9) and `--provision` writes it
over raw ATT (§10). The camera's firmware names the fields it parses —
`cmd`, `ssid`, `pwd`, `urlAndPort`, `p1`, `port`, `timezone`, `bind id`,
`param bind id` — of which our frame carries the first four and the
timezone; the last two are the camera's *binding key*, the
16-character string it advertises in its ServiceData. See
`DECOMPILATION_REPORT.md` §13–§14 and `ziot-fw/REPORT.md`.

**The frame was missing its end symbol.** The camera's own answers
settled the format:

```
86 86 | LEN16 | SEQ16 | DLEN16 | payload | XOR8 | 0x0D
LEN16 = 5 + DLEN      (the end symbol is NOT counted)
```

A frame without the trailing `0x0d` draws silence — the camera accepts
the length and then waits for the rest of it — while a frame with it
draws an answer, even if the answer is a rejection (`ffff00` = "command
not understood"). `wifi_connect_frame()` was building the frame without
it, which is why the camera never acted on our provisioning: it was not
ignoring the frame, it was waiting for its last byte.

The camera is therefore a usable oracle: `ffff00` means the frame was
read and the command not understood, silence means the frame never
completed. `att_candidates.py` drives that oracle.

### Docker (recommended)

```bash
docker build -t ziot-bridge .
docker run -d --name ziot-bridge \
  --network host \
  --restart unless-stopped \
  -v /path/to/ziot_config.json:/app/ziot_config.json:ro \
  ziot-bridge
```

If the config's `port` is not `8085`, keep the image's HEALTHCHECK in sync
with `-e BRIDGE_PORT=<port>` — the Dockerfile defaults it to `8085`.

The runtime configuration is deliberately excluded from the build context and
image and must be supplied through the shown read-only mount. Forget it and the
bridge exits 2 with a log line naming the mount it wanted, rather than a bare
`FileNotFoundError`.

**`--network host` is mandatory.** The camera sends UDP directly to the
bridge's IP — Docker bridge networking puts the container behind NAT and the
camera sends to an unroutable address.

### Deploying to a server (broker + bridge)

`compose.yaml` runs the broker (`ziot_fake_cloud.py`) and the bridge from one
image, both on the host network. The host must own the broker's addresses, let
the cameras through its firewall, and be the router's target for the vendor IPs.

#### Addresses

`ziot-addresses.conf` is a systemd-networkd drop-in holding 192.168.68.114 and
the five vendor IPs as /32 addresses. Unlike a one-shot script, they survive a
DHCP renewal or Wi-Fi roam (which wiped them on the dev laptop and dropped both
cameras). The directory name must match the interface's generated file
(`ls /run/systemd/network/`):

```bash
sudo install -D -m 644 ziot-addresses.conf /etc/systemd/network/10-netplan-wlp1s0.network.d/ziot-addresses.conf
sudo networkctl reload
ip -br addr show wlp1s0
```

If the addresses do not appear, `sudo networkctl reconfigure wlp1s0` (brief
network blip).

#### Firewall (ufw)

```bash
sudo ufw allow proto tcp from 192.168.68.0/24 to any port 80,7080,8882
sudo ufw allow proto udp from 192.168.68.0/24 to any port 13478
sudo ufw allow proto udp from <camera-ip>      # one line per camera
```

The per-camera UDP rule is needed because the bridge's media sockets use
ephemeral ports and the camera can change its source port mid-stream, which
conntrack treats as a new flow.

#### Router

In the Deco's DHCP, reserve 192.168.68.114 (it sits inside the pool) and each
camera's address. The static routes are unchanged; see
[Taking the camera off the vendor cloud](#taking-the-camera-off-the-vendor-cloud).

#### Config keys

| Key | Value | Why |
| --- | --- | --- |
| `port` | `8085` | Frigate's go2rtc reads `http://127.0.0.1:8085/cam/<uid>` |
| `api_base` | `http://120.26.121.165/api` | the local broker answers the vendor API |

Do not run the bridge with `--offline`.

#### Bring-up and verification

```bash
docker compose up -d --build
docker logs ziot-broker | grep ' sig '   # cameras reaching the broker
curl -s 127.0.0.1:8085/health
```

#### Moving from another host

Only one host may hold the addresses. On the old host, stop and disable the
broker, the address unit (`ziot-ips.service`) and the bridge, and remove its
addresses *before* the new host claims them. On the new host, remove the old
container first, since compose reuses its name (`docker rm -f ziot-bridge`),
then bring it up and announce the move so the router and peers refresh their
ARP caches (package `iputils-arping`):

```bash
sudo arping -U -I wlp1s0 -c 3 192.168.68.114
```

#### Notes

- **`--dns 0`**: systemd-resolved owns port 53, and the broker's DNS listener
  never received a query; the cameras reach it through the router's static
  routes.
- **Log hygiene**: the broker's logs carry the account token. Compose caps them
  at 3 x 20 MB; do not paste them into issues, and keep `broker-data/` out of
  version control (it is gitignored).

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
  "version": "4",
  "uptime_s": 3600,
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
      "frames_dropped": 3,
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
whose handling raised. `frames_dropped` counts frames whose first or middle
packet never arrived. They are held back rather than served broken,
because a JPEG joined around a hole decodes with smeared blocks that
Frigate's motion detector reads as motion. A few per thousand is normal
Wi-Fi loss; a share in the percent range points at the camera's signal.

`rx_datagrams` counts every datagram that arrived on the direct socket, before
any filtering. It is the one number that separates *"the camera is silent"*
from *"packets arrive and we reject them all"* — two states that look identical
on every other counter. `rx_datagrams: 0` means nothing reached us and the
problem is upstream (camera asleep, not publishing, no L2 route). A climbing
`rx_datagrams` with `frames_total` stuck at 0 means the media is arriving and
being dropped — check `foreign_ssrc` next, and the `dropping RTP with ssrc …`
log line names what it saw against what it expected.

`status` is `"ok"` when at least one camera is streaming, `"degraded"` otherwise.
Use `/health` for Docker HEALTHCHECK or external monitoring (the image ships a
HEALTHCHECK wired to it). `/health` is always unauthenticated — even with
`http_token` set — because a 401 there is indistinguishable from a dead bridge
to every reader; it exposes counters and cloud flags, not media.

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
  # Candidates are host:port pairs, not URIs. "stun:8555" parses as host
  # "stun" and never answers.
  candidates:
    - 192.168.18.6:8555
```

Pin the host's own LAN IP (`192.168.18.6:8555` here) on a multi-homed box. WebRTC does not work through
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
that camera will punch without a token. To tell "asleep, needs its cloud
notify" from "not on this LAN at all", run `python3 ziot_offline_probe.py
--ble`: a powered camera advertises `ZIOTA_<uid>` over Bluetooth LE for
a short window. Nothing on the BLE scan either? The camera is likely
off (or another central is holding its one connection).

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

**ffmpeg logs `bad vlc` or `error count: …`.**
That is a frame with a missing packet. Bridges before
`frames_dropped` existed joined the frame around the hole and served it
broken. The current bridge drops it instead and counts it in `/health`.

**Video plays at a third of real speed when ffmpeg reads `/cam/<uid>`
directly.**
Multipart MJPEG carries no timestamps, so ffmpeg assumes 25 fps against the
camera's ~8. Read it through the shipped go2rtc config, which is the tested
path, or give ffmpeg `-use_wallclock_as_timestamps 1`.

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
seconds, and a new source port is a miss. The reannounce interval grows
to the 60 s cap while the camera stays asleep. When the flag flips 0→1,
rendezvous runs immediately instead of waiting out the grown backoff.

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
* **BLE discovery in the probe** — `ziot_offline_probe.py --ble` scans
  Bluetooth LE and lists powered cameras by their `ZIOTA_<uid>`
  advertisement before you pick a LAN IP to sweep (BlueZ `bluetoothctl`;
  verified live on the Tuya-module TXW817, service UUID `0xa201`).
* **No-rebind recovery** — while a camera is cloud-offline the same local
  UDP port is reannounced instead of rebound (a 176-rendezvous loop used
  to close the socket the camera would send to), on a growing interval
  (5 → 60 s capped) rather than a fixed 5 s. Punch-only cameras
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
* **Bound before bring-up** — the HTTP server binds and serves before the
  device list is fetched and before any camera rendezvous, so `/health`
  answers (with the boot phase) from the first second of the process,
  including during a punch-only port sweep.
* **Hardened edges** — `http_host`/`http_token` optional config (media
  routes authenticated, `/health` deliberately open); a stalled MJPEG
  viewer gets a 30 s write timeout instead of pinning its handler thread
  forever, and handler threads are daemonized; `notify(keepAlive)` and
  `send-stun-addr` carry short timeouts (3 s / 5 s) so a slow cloud call
  cannot straddle the 12 s session death or triple-stall a rendezvous;
  relay digest auth is re-derived per request instead of replaying
  DESCRIBE's header on SETUP and PLAY; a relay `close()` racing its pump
  thread exits cleanly; the watchdog skips malformed camera rows; the
  shipped `go2rtc.yaml` webrtc candidate is a host:port pair (the old
  `stun:8555` parsed as host "stun"); Docker gains a `/health`
  HEALTHCHECK.
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

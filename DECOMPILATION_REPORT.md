# Z-IOT CAM decompilation — validation of the 2026-08-30 strings-only report

**Date:** 2026-08-30
**Subject:** `com.flu.flutter_wifi_camera` 1.13.0 (versionCode 100354)
**Method:** Blutter (Dart AOT → object pool + annotated assembly) on `lib/arm64-v8a/libapp.so`,
jadx on `classes*.dex`, plus cross-architecture string extraction.

This validates the earlier report, which was produced from string tables alone. Each
original claim is marked **Confirmed**, **Corrected**, or **Resolved** (an open question
now answered), with the evidence to check it.

> **These findings have since been applied to `ziot_rtp_bridge.py` (v3);** §7 maps each
> finding to what changed, and lists what remains unverified against a live relay.
>
> Section 1.6 is a correction that turned out to be wrong itself and has been withdrawn —
> it is kept rather than deleted because the reasoning error is worth not repeating.

---

## 0. Provenance — the artifacts are the same build

The earlier teardown used `~/ziot_apk/base.apk` + `split_config.x86_64.apk`. This pass adds
two APKPure bundles of the *same* versionCode.

| Fact | Evidence |
|---|---|
| XAPK base == the previously analysed base | SHA-256 `00f17dd75e939a3f…efcfc9`, byte-identical to `~/ziot_apk/base.apk` |
| arm64 bundle is the same app build | all five `classes*.dex` byte-identical across bundles (`classes.dex` `3419c8e6ddb7ea02`, …) |
| arm64 Dart code matches the other arches | every protocol string below present in all of arm64 / armeabi-v7a / x86_64 `libapp.so`; 30,855 strings common to all three |
| Snapshot | Dart **3.10.8**, `android arm64`, product, compressed-pointers, snapshot hash `1ce86630892e2dca9a8543fdb8ed8e22` |

APKPure serves three distinct bundles for versionCode 100354. The one in this repo
(`_b434f1b7`, 145,326,973 B) carries **armeabi-v7a only**; `_e0b683d3` (115,651,995 B)
carries `config.arm64_v8a.apk` and is the one Blutter needs. Its base APK is a different
*file* size (45,172,335 B) purely from asset compression — the code is identical, as the
dex hashes prove.

---

## 1. Corrections — claims that were wrong

### 1.1 `cmdType 20` is **not** "start live". It is `speakOff`.

This is the most consequential finding, because the bridge sends it on every rendezvous.

The full enum, recovered from the object pool (`objs.txt`, `Obj!CameraCMDType@b002a1`
through `@b00581`; declared at `asm/…/camera_api.dart:4499`):

| idx | name | wire value | idx | name | wire value |
|----:|------|-----------:|----:|------|-----------:|
| 0 | `restart` | 1 | 12 | `ptzRight` | 13 |
| 1 | `restore` | 2 | 13 | `ptzAlwaysUp` | 14 |
| 2 | `light` | 3 | 14 | `ptzAlwaysDown` | 15 |
| 3 | `sdCard` | 4 | 15 | `ptzAlwaysLeft` | 16 |
| 4 | `formatSDCard` | 5 | 16 | `ptzAlwaysRight` | 17 |
| 5 | `firmwareOTA` | 6 | 17 | `ptzMoveStop` | 18 |
| 6 | `infraredLight` | 7 | 18 | `speakOn` | 19 |
| 7 | `originHorizontal` | 8 | 19 | **`speakOff`** | **20** |
| 8 | `originVertical` | 9 | 20 | `lampLight` | 21 |
| 9 | `ptzUp` | 10 | 21 | `definition` | 22 |
| 10 | `ptzDown` | 11 | 22 | `ptzReset` | 23 |
| 11 | `ptzLeft` | 12 | 23 | `sensitivity` | 25 |

24 members. Value 24 is unused. **There is no "start live" command in this enum at all.**

`CameraAPI::requestSendCmd` (`0x721b68`) builds `{"cmdType": …, "uid": …}` and POSTs it to
`/v1/cmd/send-cmd` (`0x721fc0`). The `cmdType` value is read from the enum instance's
string field — the field holding "1".."25" — so the captured `"cmdType":"20"` is
`speakOff`. The conclusion holds under the alternative reading too: if the app sent the
*index* instead, 20 would be `lampLight`. Neither is "start live".

The original report inferred "20 = start live" from a capture taken while starting live
view. The likelier explanation is that the app mutes the speaker when the live view opens.

### 1.2 The app **does** build RTSP URLs from the relay, and they are real media endpoints

The original report's central negative — *"the `relay_ip:554` RTSP servers appear nowhere
in the app's media path"* — is wrong. `CameraInfoModel::relayPath` (`0x724d48`,
`asm/…/camera_info_model.dart:5211`) constructs, from a relay host and the camera uid:

```
_supportRelayPath() true   ->  rtsp://<host>/rtp/<last 8 characters of uid>
_supportRelayPath() false  ->  rtsp://<host>/live/<uid>
```

The gate is `CameraInfoModel::_supportRelayPath` (`0x724f30`):

```dart
bool _supportRelayPath() {
  if (CameraType.fromFields(deviceType ?? "") == CameraType.TaiXinX5)   // "X5"
    return deviceNeedUpdate(version ?? "", "TXW817_A_V1.0.11.52");
  return true;                                                          // every other type
}
```

`deviceNeedUpdate` pulls `V?(\d+(\.\d+)+)` out of both strings and compares
component-wise, returning true when the device is **older** than the target. So for an X5
the `/rtp/` form is the *older*-firmware branch, despite the method's name. The branch
selector is `tbnz w0, #4` at `0x724da0`, which jumps on `false` — Dart's `false` is
`NULL+0x30` and `true` is `NULL+0x20`, so bit 4 distinguishes them.

Your cameras are `X5` on `TXW817_A_V1.0.12.32`, which is **newer** than the pivot, so
`_supportRelayPath()` is false and they take the `/live/<uid>` form:

```
rtsp://<relay_ip>/live/141030191094
```

An earlier revision of this document had this backwards. The bridge therefore tries both
forms and keeps whichever answers, rather than trusting this reading (`relay_urls()` in
`ziot_rtp_bridge.py`). It logs
`转发地址: 使用的流程是 {新|旧} 的 -> {url}` ("forwarding address: using the {new|old} flow"),
which is worth capturing from a real session to settle it empirically.

The earlier probe got an empty `DESCRIBE` because it asked for the server root, not this
path. The relay flow is driven by `CameraNet.requestSendRelay`
(`asm/…/camera_controller_ext_subs.dart:1969`), which calls `requestSendEvent` with
`CameraEventType.relay`, and then `CameraControllerMove.setupRelayVideo` (`:2074`) calls
`relayPath()` and hands the URL to the player.

**Live verdict (2026-08-31): the relay did not serve media even for an online camera.**
With `.73` cloud-online (`onlineState=1`, correct Bearer auth), `--force-relay` fired
`notify(EVENT_RELAY)` and then opened `/live/<uid>` and `/rtp/<last8>`: both TCP
connections were accepted but `OPTIONS` never got an answer (10 s timeout each) — no
SDP, no interleaved RTP. Raw-socket probes against the same relay, with and without a
relay notify, get an instant `OPTIONS 200` from `Server: ZLMediaKit` but an empty
`DESCRIBE`. The relay behaves like a ZLMediaKit instance *waiting for the camera to
publish* (the app's flow is camera → relay → viewer), and these cameras never publish
because their media/session daemon is dead (see §7 verdict below). So the relay is not a
usable fallback for these units: it cannot conjure media from a camera whose session
never starts.

### 1.3 `libzlmediakit_jni.so` is not "arm64-only" — it is missing only from x86_64

It ships in **both** arm64-v8a and armeabi-v7a. Ten libraries are absent from the x86_64
split alone:

| library | arm64-v8a | armeabi-v7a | x86_64 |
|---|:--:|:--:|:--:|
| `libzlmediakit_jni.so` | ✅ | ✅ | ❌ |
| `libnms.so` | ✅ | ✅ | ❌ |
| `libscannative.so` | ✅ | ✅ | ❌ |
| `libpglarmor.so` / `libbuffer_pgl.so` / `libfile_lock_pgl.so` | ✅ | ✅ | ❌ |
| `libapminsighta.so` / `libapminsightb.so` | ✅ | ✅ | ❌ |
| `libtobEmbedPagEncrypt.so` / `libtt_ugen_layout.so` | ✅ | ✅ | ❌ |

(29 libs in arm64, 37 in armeabi-v7a — the extra 8 are `*_neon` ffmpeg variants — 19 in
x86_64.) The operational conclusion is unchanged: Waydroid on x86_64 cannot run live view.
The reason is that the vendor ships no x86_64 build of these libraries, not that they are
arm64-exclusive.

### 1.4 `BmConnectionStateEnum` was misattributed — it is Bluetooth, not the camera

The original §5 listed it alongside the device-list state fields. From the object pool:

```
BmConnectionStateEnum : disconnected(0), connected(1)
BmAdapterStateEnum    : unknown, unavailable, unauthorized, turningOn, on, turningOff, off
```

That is flutter_blue_plus (`Bm` = BluetoothMsg). It has nothing to do with the device
list's `connectionState: "BR"`. The camera's own state enum is `CameraConnectState`
(`asm/…/camera_api.dart:4798`):

```
offLine(0) prepare(1) start(2) p2p(3) relay(4) directConnect(5) stop(6) unknown(7)
```

Note this is a *client-side* connection state, not the `"BR"` string the API returns; no
code in the snapshot parses `"BR"`.

### 1.5 "No `rtsp://` URL is ever constructed in Dart" — wrong as written

Besides §1.2, the snapshot contains `rtsp://0.0.0.0:8554/` — the local ZLMediaKit server
the app serves to its own player. (This string is absent from the earlier x86_64/v7a
exact-match check only because the adjacent string ran together with it.)

### 1.6 *(withdrawn — this correction was itself wrong)*

An earlier revision of this document claimed the app never issues a per-uid STUN GET,
reasoning from the absence of any `stun-addr/<uid>` literal in the string table. That
reasoning was unsound: absence of a literal is not absence of a call.

`CameraAPI::requestLatestStunAddress(deviceUID)` (`0x65312c`) does exactly that GET. It
builds the URL by interpolating `<base>/<uid>` — where `<base>` is a **runtime-initialised
static** (`LoadStaticField(0xcc0)`), not a literal — calls `BaseAPI::getMethod`, and parses
the reply into a `DeviceStunItem`. That model's fields are `IpcPrivateIP`,
`IpcPrivatePort`, `IpcPublicIP`, `IpcPublicPort`, `seqNo`, `updateTime`, `receiveTs`.

So the bridge's `GET /v1/ipc/stun-addr/<uid>` is legitimate app behaviour, and the
original report's step 4 stands. What the base path resolves to at runtime could not be
recovered statically; it is not needed, since the endpoint the bridge uses demonstrably
works.

Two behaviours of that call *were* recovered and are new findings — see §4.6 and §4.7.

---

## 2. Resolved — the two questions the strings could not answer

### 2.1 `mediaState`: the app only ever tests `== "0"`

`mediaState` is `field_2b` of `CameraInfoModel`. Across the entire snapshot it is read in
exactly one place outside `fromJson`/`toJson` — the getter `isFree` (`0x86e020`,
`asm/…/camera_info_model.dart:9264`):

```dart
bool get isFree {
  if (mediaState == null) return false;
  return (mediaState.isEmpty ? "0" : mediaState) == "0";
}
```

So the app's model is binary: **`"0"` = free (no media session), anything else = busy.**
It never distinguishes the non-zero codes from one another. Values `1`, `2` and `3` have
all been seen in the field — `2` was caught by the cloud-state watcher during the v3 live
test, in the transition `cloud state 1/2 -> 0/0` — and the app treats all three
identically. The vendor's server-side meaning of the individual codes is not knowable from
the client, because the client does not use it.

`onlineState` is `field_27`, consumed by `isOn` (`0x65a4d4`, `:3274`):

```dart
bool get isOn {
  if (onlineState == null) return false;
  return (onlineState.isEmpty ? "0" : onlineState) == "1";
}
```

The bridge's `str(onlineState) == "1"` test matches the app exactly.

### 2.2 `CameraEventType` — the `notify-live-event` values

Recovered in full (`asm/…/camera_api.dart:4850`). Index and wire value are identical:

| value | name | used by bridge? |
|---:|---|---|
| 0 | `keepAlive` | ✅ (the 2 s keepalive) |
| 1 | `start` | ✅ (the wake) |
| 2 | `pause` | ❌ |
| 3 | `stop` | ❌ |
| 4 | `connected` | ❌ |
| 5 | `relay` | ❌ — this is what requests the relay session |

All six are used by the app (`camera_controller_ext_subs.dart`). The bridge's use of 1 and
0 is correct and now has names behind it.

---

## 3. Confirmed — claims that held up

| Claim | Status |
|---|---|
| API base `http://ipc.gps555.net/api` | Confirmed — `asm/…/config.dart:439`. In-app it is plaintext HTTP |
| `POST /v1/cmd/send-cmd` with `{"cmdType","uid"}` | Confirmed — `0x721fc0`, body built at `0x721d7c` |
| `GET /v1/ipc/send-stun-addr` | Confirmed — `0x651b54`, `0x726f0c` |
| `GET /v1/ipc/notify-live-event` | Confirmed |
| `App send hello` is plaintext UDP | Confirmed — `StunManager::_startP2PConnect` `0x64f758`, sent as `Uint8List.fromList("App send hello".codeUnits)`, 14 bytes |
| `App send heart for stun` | Confirmed — `StunManager::_sendStunHeart` `0x65039c`, 23 bytes |
| `stunaddr.gps555.net:13478` / `stun.gps555.net:13478` | Confirmed — `asm/…/config.dart` |
| `ws://ws.gps555.net:7080/ws` status push | Confirmed — `config.dart:926` |
| Dart holds all protocol logic; DEX holds none | Confirmed — `mediaState`, `onlineState`, `stun-addr`, `send-cmd`, `gps555`, `notify-live-event`, `terminalFamilyId`, `relay_ip`, `natType` all have **0** occurrences across all five dex |
| ZLMediaKit is player plumbing only | Confirmed — `com.zlmediakit.jni.ZLMediaKit` (classes5.dex) exposes only `setupServer`, `startServer`, `stopServer`, `createMediaPlayer(String url, cb)`, `setupMediaListener`, `releaseMediaPlayer`. No protocol logic. The vendor also shipped `com.zlmediakit.demo.MainActivity` |
| Ad SDKs (APS, Meta Audience Network, AppLovin) | Confirmed — `assets/aps-mraid.js`, `assets/audience_network.dex`, `libapplovin-native-crash-reporter.so` |

---

## 4. New findings the original report did not have

### 4.1 A complete second backend: `gps666.net`

`asm/…/config.dart` selects at runtime between two full stacks:

| role | primary | alternate |
|---|---|---|
| API | `http://ipc.gps555.net` | `http://ipc.gps666.net` |
| status websocket | `ws://ws.gps555.net:7080/ws` | `ws://ipc.gps666.net:7080/ws` |
| second websocket | `ws://ws.gps555.net:7090/ws` | `ws://ipc.gps666.net:7090/ws` |
| signalling | `sig.gps555.net:8882` | `ipc.gps666.net:8882` |

Referenced from `config.dart`, `request.dart` and `application.dart`. The app also has
`/v1/ip/get-country`, so the switch is plausibly regional — not confirmed.

### 4.2 A signalling server the bridge does not know about

`sig.gps555.net:8882`, fetched via `GET /v1/ipc-function/get-sig-ip`
(`asm/…/camera_api.dart`). Its role was not traced.

### 4.3 Timing constants

- The `App send hello` punch loop is `Timer.periodic` at **1 s** (`_startP2PConnect`,
  `Duration@b01731` = 1,000,000 µs). The bridge punches every 1 s — the same
  cadence, app-exact. (An earlier revision punched every 0.5 s; PUNCH_INTERVAL
  is 1.0 s since the app-exact pass.)
- `getDeviceLatestStunAddr` polls at **1.5 s** (`Duration@b017a1`).
- The stun-heart interval is computed at runtime (`AllocateDurationStub` at `0x6505f8`),
  not a constant, so the bridge's fixed 5-tick ratio has no fixed counterpart in the app.

### 4.4 The full device-list schema

`CameraInfoModel.fromJson` field order, which is the authoritative list of what the API
returns: `uid, name, email, pwd, version, familyId, terminalFamilyId, onlineState,
mediaState, natType, wifiSsid, connectionState, createdAt, updatedAt, createBy, updateBy,
sdPolicy, sdState, sd_total, sd_available, led, lampLight, infraredLight, hflip, vflip,
power, signal, shareId, online_time, relay_ip, server_ip, isRemoteServiceExpired,
remoteServiceEndTime, isShowRemoteService, resolution, event_sensitivity`.

### 4.5 41 API paths

Full inventory extracted (login, binding, sharing, cloud events, feedback, dictionaries).
Only the six the bridge touches are relevant here; the rest are in
`out/blutter` if ever needed.

---

### 4.6 The camera's address is chosen by subnet, not fixed

`DeviceStunItem.ipAddress` (`0x652dec`, `asm/…/device_stun_item.dart:617`) calls
`isSameSubnet()` — the mask constant `255.255.255.0` sits in the same function — comparing
the phone's own address with the camera's `IpcPrivateIP`, and logs one of:

```
DeviceStunItem 设备与手机在同一网络，使用局域网 IP:      (same network, using LAN IP)
DeviceStunItem 设备与手机不在同一网络，使用公网 IP:      (different network, using public IP)
```

returning `IpcPrivateIP:IpcPrivatePort` or `IpcPublicIP:IpcPublicPort` accordingly. **This
is the app's primary off-LAN path** — cheaper than the RTSP relay and tried first. The
bridge previously hardcoded the private pair; `_pick_endpoint()` now mirrors this.

### 4.7 STUN answers are freshness-checked

`StunManager::_getLatestStunAddr` logs `获取到的 stun 有效/无效: seqNo -> …,
updateSeqNo -> …, ip -> …` ("stun received valid/invalid"), and
`StunManager::_isValidStunResponse` (`0x6544d0`) screens the response before it is used.
`DeviceStunItem` carries `seqNo` and `updateTime` for this purpose. The bridge now rejects
an answer whose `seqNo` is lower than the last accepted one (`_accept_stun()`), resetting
the tracker on a full re-rendezvous so a camera restart cannot wedge recovery.

## 5. What could not be determined

- The **server-side** meaning of `mediaState` values `1` vs `3`. Not a tooling limit — the
  client genuinely does not use it (§2.1). Only vendor server behaviour or a broader
  observational sample could distinguish them.
- The meaning of the `connectionState` string `"BR"`. No Dart code parses it.
- Which condition selects `gps666.net` over `gps555.net`.
- The role of `sig.gps555.net:8882`.

---

## 6. Reproducing this

```
scratchpad/
  apk/{xapk,xapk64,arm64,v7a,x86_64}/   extracted bundles and per-arch lib dirs
  tools/blutter/                        Blutter @ 4a60ac6, built against Dart 3.10.8
  tools/jadx/                           jadx 1.5.6
  out/blutter/{objs.txt,pp.txt,asm/}    object pool + annotated assembly
```

Blutter built cleanly with GCC 16.2.1 / cmake 4.4.3 / ninja 1.13.2 against system
capstone 5.0.9 and icu 78.3 — no patches, no compiler downgrade. The only missing
dependency was `pyelftools` (supplied via a venv).

---

## 7. What was changed in the bridge

Applied in `ziot_rtp_bridge.py` (v3). See the README for operator-facing detail.

| Finding | Change |
|---|---|
| §1.1 `cmdType 20` is `speakOff` | The `send-cmd` call is gone from `_rendezvous()`. `GPS555.send_cmd()` remains as a real control API, validated against the recovered `CAMERA_CMD` table. |
| §2.2 `CameraEventType` | Named constants `EVENT_KEEPALIVE … EVENT_RELAY`; teardown now sends `stop(3)` where it used to send `keepAlive(0)`. |
| §4.6 subnet-based endpoint | `_pick_endpoint()` picks the LAN pair when the camera shares our /24, else the public pair, logging the decision as the app does. |
| §4.7 STUN freshness | `_accept_stun()` rejects lower `seqNo`; `seqNo`/`updateTime` exposed on `/health`. |
| §1.2 relay | `RelayStream`, an RTSP-over-TCP client using interleaved RTP, engages after `RELAY_AFTER_FAILURES` failed direct attempts (or immediately with `--force-relay`), preceded by `notify-live-event(relay)`. Both URL forms are tried. |
| §2.1 `mediaState` | `cloud_is_free()` / `cloud_is_on()` implement the app's `isFree` / `isOn` exactly, including the empty-string ⇒ `"0"` rule the old `str(x) == "1"` got wrong. Raw flags are still reported alongside the booleans. |
| §4.3 punch interval | Defaults to the app's 1 s, overridable via `punch_interval`. |

### Still open

* **(resolved 2026-08-31) Which relay URL form actually plays — neither did.** A live
  `--force-relay` run against camera `.73` while it was cloud-online
  (`onlineState=1`, `140979857781`) fired `notify(EVENT_RELAY)` and tried
  `rtsp://156.246.16.114:554/live/140979857781` and `/rtp/79857781`: no media on either
  form. The bridge reported this as "relay accepted but never answered", which was a
  bad error message on our side, not what happened — `RelayStream` did not name the
  stalled request, so an `OPTIONS`-then-`DESCRIBE` sequence that got through the first
  step read as total silence. Re-tested 2026-08-31 with the message fixed: the relay
  answers `OPTIONS 200` in ~0.2–0.4 s for every URL form (root, `/live/…`, `/rtp/…`,
  with and without our User-Agent) and then **stalls on `DESCRIBE`**. That matches the
  raw-socket checks exactly — instant `OPTIONS 200` (`Server: ZLMediaKit…`), empty
  `DESCRIBE`, with or without a relay notify — so the two observations agree once the
  reporting bug is removed. The relay looks like a ZLMediaKit publisher-wait: the app's relay flow
  is camera → relay → viewer, and these cameras never publish because their
  media/session daemon fails to start after boot (the 2026-08-30 boot-watch: a rebooted
  camera re-registers once with the cloud, then never keepalives, never opens a media
  socket, and ignores wake events — the session daemon never comes up). §1.2's "working
  off-LAN fallback" therefore does not hold for these units — the relay cannot
  manufacture media a camera never sends.
* **Whether the relay needs credentials.** Nothing in the snapshot authenticates to it.
  The client handles Basic and Digest and fails loudly rather than guessing;
  `relay_user` / `relay_pass` config keys are ready if it turns out to need them.
* The `gps666.net` selector, the role of `sig.gps555.net:8882`, and the server-side
  meaning of the non-zero `mediaState` codes, of which `1`, `2` and `3` have been
  observed (§5).

---

## 8. The camera's Bluetooth pairing surface — live RE, 2026-10-22

Recovered by scanning and connecting to unit `140979857781` in pairing mode
(rebooted with no WiFi configured). No decompilation involved — everything
below is read off the air.

### Advertisement

* Device name: `ZIOTA_<uid>` — the UID is broadcast, no auth. This is what
  `ziot_offline_probe.py --ble` uses.
* Service UUID `0000a201-…` in the advertisement. In **pairing mode** the
  advertisement additionally carries ServiceData on `0xa201` whose payload is
  literal ASCII: `key` + a 16-char secret (observed `keyyfg5y34yqxqgd`).
  **That is the device's binding/registration secret, broadcast in the
  clear** to anyone in BLE range. It matches the per-device `pwd` field in
  the device-list schema (§4.4) in shape; the cloud presumably knows the
  same value. Implication for a cloud-free deployment: the camera already
  carries its credential locally — a local broker can know it too, by
  reading the pairing advertisement once.
* The **WiFi MAC is the reversed BLE MAC**: BLE `E0:44:40:60:68:82`,
  associated WiFi `82:68:60:40:44:e0`. Six bytes matching in reverse is not
  chance; `ziot_offline_probe.py --ble` resolves the LAN IP from the ARP
  table with this (a second unit should be checked before hardening this
  into a claim).
* BLE only advertises in a short window at boot / in pairing mode. A paired,
  online camera advertises nothing.

### GATT database (btgatt-client, full)

* `0x1800` Generic Access: device name (`0x2a00`), appearance (`0x2a01`).
* Vendor service `36000001-0001-0002-0003-010203040506`:
  * `36000002` @ handle `0x0008`, props `0x0c` (write, write-without-response)
    — the command channel.
  * `36000003` @ handle `0x000a`, props `0x12` (notify) + CCCD `0x2902` — the
    response channel. Subscription succeeds; the camera pushes nothing until
    a valid command is written.

### Connection behavior

* Outside pairing mode the camera **refuses** the connection
  (`Connection refused` / hangs) — it was likely held or bonded by another
  central (the phone running the vendor app).
* In pairing mode the connection succeeds and services resolve, but
  `bluetoothctl` sessions are killed by the camera ~1 s after
  `ServicesResolved` — before an attribute listing returns. `btgatt-client`
  holds the link indefinitely: the drop is triggered by bluetoothctl's
  discovery/attr sequence, not by a fixed timeout.
* Writes to the command channel were **not** attempted: no point fuzzing a
  protocol whose client side is recoverable from the vendor APK's Dart code
  (the BLE bits the decompilation saw as `BmConnectionStateEnum`), and the
  camera sat in the user's provisioning window.

### What this buys the cloud-free goal

1. Provisioning (a write to the command channel) is how the vendor app
   delivers SSID, password, and signalling address. The payload format
   was recovered from the app's Dart snapshot afterwards — see §9.
2. The cleartext binding key (`key…`, ServiceData) is the likely
   camera→cloud credential; a local broker emulator needs exactly this to
   authenticate the camera.
3. Still the hard unknown: the camera↔cloud protocol itself (registration,
   `notify(start)` delivery, keepalive). Requires an encrypted WiFi capture
   of a wake — monitor mode + PSK decryption — which no amount of BLE work
   substitutes.

## 9. The provisioning frame format — reconstructed from the app, 2026-10-22

The payload format §8 called unknown was reconstructed from the app's Dart
AOT snapshot (Blutter annotated assembly), not guessed. It has **not**
been observed on the air — no capture of a real provisioning exists.
`CustomBleProtocolPacket` (`app/config/custom_ble_protocol_packet.dart`)
builds one frame shape for every command.

```
86 86 | LEN16 | SEQ16 | DLEN16 | payload | XOR8
```

* `LEN16` = `5 + DLEN` — it counts everything after the LEN field
  (SEQ + DLEN + payload + XOR).
* All multi-byte fields are big-endian.
* `XOR8` is the XOR of every preceding byte (`0 .. 8+DLEN-1`).
* There is **no `0x0D` terminator on requests** — that byte belongs to the
  camera's *response* frames only.

`wifiConnect` payload (command `00 01`), in this exact order:

```
00 01 | <len><ssid utf-8> | <len><pwd utf-8> | <len><sigAddr utf-8> |
<tz minutes, big-endian> | [regCode 4B, big-endian, optional]
```

* Negative timezone minutes are biased by `+0x8000`, not two's complement
  (`_handleApRequest` masks the high and low byte separately and appends
  them high-first).
* `sigAddr` is `host:port` — the signalling server the camera registers
  with. The app fetches it from the cloud (`icpSigAddress`) and stores it in
  the frame; a local deployment substitutes its own broker.
* `regCode` is `stringToUint32(sha256(text))`: SHA-256 of the digest's
  bytes 0, 2, 4 and 6 packed big-endian into a uint32. The app derives it
  from the *phone's* UUID plus a timestamp, so it is a nonce, not a
  credential. `getApCommand` appends it for every real device and omits it
  only for the placeholder device `"FFFFFFFFFFFF"`.

**Byte-level cross-check.** A frame reconstructed by hand from the
assembly (`getApCommand` → `sendApInfoRequest` → `_handleApRequest`) is
reproduced exactly by `ziot_fake_cloud.py`'s `wifi_connect_frame()`. That
checks the builder against the reading of the assembly; it does not prove
the camera accepts the bytes:

```
built:   868600350001003000010d486f757365206f66204361747309313233343536373839133139322e3136382e36382e3131343a383838327f4c59
by hand: 868600350001003000010d486f757365206f66204361747309313233343536373839133139322e3136382e36382e3131343a383838327f4c59
```

and the regCode variant likewise (`…7f4cc43459ad55`, LEN `0x39`), with the
worked example `sha256("a1b2c3d4-e5f6-7890-abcd-ef1234567890" +
"2026-10-22 15:00:00.000")` → `c4 34 59 ad`.

**Chunking.** `DeviceBluetoothAddController.splitSend` writes the frame in
20-byte pieces: chunk *i* is `sublist(i*20, min(i*20+20, len))`. There is
no per-chunk header. The app asks for MTU 1024 first and falls back to 20
when the negotiated MTU is smaller.

## 10. Talking to the camera — raw ATT instead of the GATT layer, 2026-10-22

`bluetoothctl`/`btgatt-client` stall on these cameras (one service, then
silence) and BlueZ's client holds the link, so a raw connect is refused
while it is attached. Raw ATT over L2CAP CID 4 connects and is fast enough
to land inside the short pairing window — the connect *is* the trigger.

**Correction (same day).** The first ATT scripts read PDUs byte by byte.
L2CAP is `SOCK_SEQPACKET`: one `recv()` is one PDU, and a short `recv()`
*discards the rest of the datagram*. Every multi-byte PDU was therefore
truncated to its first byte, and the same scripts wrote ATT handles
big-endian (they are little-endian) and a 4-byte CCCD value (it is 2
bytes). Findings below are marked by whether they survive that.
`ziot_fake_cloud.py` now reads whole PDUs and has tests pinning the wire
bytes. Re-run live on unit `140979857781` (still advertising, never
joined) with that transport, see "Live result with the corrected
transport" below.

* **Survives — the first byte of each reply.** Classifying a reply by
  its opcode byte is valid even when truncated. Probing handles with a
  1-byte write on unit `140979857781`: `0x0001`/`0x0002` answered with an
  Error Response; `0x0003`–`0x0007` and `0x0009`–`0x000b` answered with a
  Write Response; `0x000c` and above answered with an Error Response. A
  write to **`0x0008`** was answered by `0x1b` — a Handle Value
  Notification — followed by the Write Response. So that handle makes the
  camera speak. The notification *body was never read*.
* **Does not survive — "reads are refused", the MTU value, the CCCD
  handle.** The opcode bytes of the read replies were Error Responses, but
  the error code and handle were truncated away, so *why* reads fail is
  unknown (a "read not permitted" on write-only handles would be normal).
  The MTU response was recorded as `03 17 00` in one run and `03 02 17` in
  another, which is what truncation looks like; server MTU 23 is plausible
  but unconfirmed. Whether `0x000b` is the notify CCCD is inferred from
  the acknowledgements only.
* **Survives — the outcome.** After writing the frame to `0x0008` (twice
  on `140979857781`, once on `140996632758`) the camera kept advertising
  and never appeared on the home LAN. A camera that took WiFi credentials
  would be expected to stop advertising, so the frame was not acted on.
  The truncated CCCD write may mean notifications were never enabled,
  but that cannot explain the absent join.

### Still open

* **Live result with the corrected transport.** MTU exchange `03 17 00`
  (server MTU 23 — now read whole, not inferred); CCCD write at `0x000b`
  acknowledged; all three frame chunks written to `0x0008` acknowledged
  (`13`); then **no notification at all** in a 15 s window, no WiFi join,
  and the camera kept advertising. So the `1b` seen during the 1-byte
  handle probe was a reaction to that probe, not a reply to the frame:
  the camera does not answer the frame. In one run the camera also sent
  its own `02 17 00` (an Exchange MTU *Request*) where the CCCD response
  belonged; answering it with `03 17 00` made the camera reply
  `01 03 17 00 06` — Error Response, request opcode `0x03`, "Request Not
  Supported" — so it must be skipped, not answered.
* **Why the frame is ignored is unknown.** Candidates, none tested with
  a working read path: the command byte or payload differs from the
  assembly reading; the `regCode` is required; the camera expects the
  write on a different handle than `0x0008` (the UUIDs were never read);
  the pairing state machine has timed out although the camera still
  advertises. The cheapest discriminator is a capture of the real app
  provisioning once (`btmon`, needs the sudo rule already in place),
  which would show the true handle and bytes in one go.
* **Whether the frame is what the firmware expects.** The frame is
  reproduced from the app's *assembly* (§9), never observed on the air,
  so a wrong command byte, the `0x0D` terminator or the `regCode` remain
  possibilities. A `regCode` variant was tried once with the broken
  transport; it proves nothing either way.
* **The AP-mode socket.** The app connects to device port **16532**, which
  was refused on `140996632758`'s AP. Ports 7070 and 8266 accepted TCP and
  never answered anything sent (the frame raw, its hex text, an HTTP GET).
  An earlier note here said this socket speaks hex text; that was wrong —
  `_convertHex16Msg` only formats a log line in `socket_handler.dart`
  (`0x53cfb4` feeds `LogD`).
* **Which handle is `36000002`.** UUIDs were never read from this
  firmware; the write handle is inferred from behaviour.

## 11. A live app session captured on the wire, and what the broker does with it

`PCAPdroid_29_ago._15_13_27.pcap` (2026-08-29, phone side, 721 packets)
is a real app session against the vendor cloud and is the ground truth
for the HTTP API. PCAPdroid sees only the phone's own traffic, so it
contains **no camera traffic** and says nothing about the camera↔cloud
protocol.

What it shows:

* Plain HTTP on port 80 to `ipc.gps555.net` (`120.26.121.165`). The app's
  first `GET /api/v1/ipc/?terminalFamilyId=…` (trailing slash) draws a
  `301`; it retries without the slash.
* Every reply is `{"requestId":…,"code":200,"msg":"OK","data":…}`.
  `send-stun-addr` and `stun-addr` return `IpcPublicIP/Port`,
  `IpcPrivateIP/Port` and a `seqNo` that rises by one per call (114, 115
  in the capture); `stun-addr` adds `updateTime`. `notify-live-event` and
  `send-cmd` return `data: "ok"`; `share-list` returns `[]`;
  `current-package` returns `packageStatus "3"`.
* A separate UDP exchange with `stunaddr.gps555.net:13478`: a 20-byte
  request and a 56-byte reply from two local ports, before the REST
  calls. The broker does **not** implement it, and the bridge has never
  needed it.

`ziot_fake_cloud.py`'s `HttpApi` answers exactly those shapes, and the
bridge's new `api_base` option sends its signalling there. Verified on this
host: `--list-cameras` listed the camera from the local broker, and a full
bridge run did `send-stun-addr` → `notify` → `stun-addr` → punch against it
(`/health`: `endpoint_kind: private`, `addr` taken from the broker). The
camera address used was a placeholder, so no media flowed; **a real
camera streaming against the local broker has not been demonstrated.**

Lab topology used for the AP-mode probing: `wlan0` (internal) stays on the
home LAN; `wlan1` is the USB ZyDAS ZD1211B adapter (`0ace:1215`, needs the
`zd1211/` firmware from linux-firmware) and associates with a camera's own
open AP `ZIOTA_<uid>` (`192.168.1.0/24`, camera at `192.168.1.1`). A
camera's AP carries the same MAC as its STA side, so `ip neigh` always
shows it on `192.168.1.1`; a join to the home LAN must be looked for on
`192.168.68.x`, not by MAC alone.

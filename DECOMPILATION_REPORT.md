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

### The vendor's wire contract, in full

Reassembled from the capture (the phone's two connections carry the whole
session) and reproduced by the broker. `VendorConformanceTests` in
`tests/test_ziot_fake_cloud.py` checks each point against the broker, so
the contract cannot drift silently.

**Headers** (nginx 1.24.0 on Ubuntu):

```
HTTP/1.1 200 OK
Server: nginx/1.24.0 (Ubuntu)
Date: <RFC 1123>
Content-Type: application/json; charset=utf-8
Content-Length: <n>
Connection: keep-alive
Access-Control-Allow-Origin: *
Cache-Control: no-cache, no-store, max-age=0, must-revalidate
Expires: Thu, 01 Jan 1970 00:00:00 GMT
Last-Modified: <RFC 1123>
X-Content-Type-Options: nosniff
X-Xss-Protection: 1; mode=block
```

**Envelope:** `{"requestId": <uuid>, "code": 200, "msg": …, "data": …}`.
`msg` is `"OK"` everywhere except the package record, which answers
`"查询成功"`.

**The device list** is `{"count", "pageIndex", "pageSize", "list",
"showAd"}` — and `count` is **0** in the vendor's own answer even though
`list` holds the camera. The field is not populated; the app and the
bridge read `list` and never look at `count`, so the broker reproduces the
vendor's value rather than inventing one.

**The device record** carries 46 fields in a fixed order, from `uid` to
`unReadEventCount`. The types matter to the app's parser:
`onlineState`/`mediaState` are **strings** (`"1"`, `"0"` — the app
compares them as text), `isShowRemoteService`/`isRemoteServiceExpired`
are **booleans**, `familyId`/`terminalFamilyId`/`createBy`/`updateBy`/
`unReadEventCount` are **numbers**. The broker emits the same set, in the
same order, with the same types.

**The two endpoints answer with different shapes**, which is easy to get
wrong: `send-stun-addr` returns `receiveTs`, `stun-addr` returns
`updateTime` instead (both carry `IpcPublicIP/Port`,
`IpcPrivateIP/Port` and `seqNo`).

**The STUN server answers too.** `stunaddr.gps555.net:13478` is a plain
STUN server, and the camera needs its answer: it is how the camera learns
the address and port to register with. The captured exchange is a bare
20-byte Binding Request (`0001 0000 <magic> <txid>`) answered with a
Binding Success carrying three attributes:

| Attribute | Meaning | In the capture |
|---|---|---|
| `0x0001` MAPPED-ADDRESS | the peer as the server sees it | `177.221.120.116:64668` |
| `0x0004` | the server's own address | `172.16.110.64:13478` |
| `0x8020` | the peer again, XOR-obfuscated | port and address XORed with the magic cookie |

`0x8020` is the vendor's own attribute (XOR-MAPPED-ADDRESS is `0x0020`;
they use the comprehension-optional range). Its **address** decodes back to
the peer's public address with the RFC 5389 mask; its **port** is the
NAT-mapped port, which the vendor's server could only know from its NAT
and which changed between the two captured requests. The broker answers
with the peer's source port in that field, which is the same value on a
LAN.

The broker's STUN answer matches the vendor's byte for byte in the header,
the cookie, the transaction id, `0x0001` and `0x0004`; only the `0x8020`
port differs, and it is the one field a server cannot derive.

**The trailing slash draws a 301.** `GET /api/v1/ipc/?terminalFamilyId=…`
answers `301 Moved Permanently` with `Location: /api/v1/ipc?terminalFamilyId=…`
and a 71-byte HTML body; the app follows it and asks again without the
slash. The broker reproduces the redirect rather than answering directly.

Lab topology used for the AP-mode probing: `wlan0` (internal) stays on the
home LAN; `wlan1` is the USB ZyDAS ZD1211B adapter (`0ace:1215`, needs the
`zd1211/` firmware from linux-firmware) and associates with a camera's own
open AP `ZIOTA_<uid>` (`192.168.1.0/24`, camera at `192.168.1.1`). A
camera's AP carries the same MAC as its STA side, so `ip neigh` always
shows it on `192.168.1.1`; a join to the home LAN must be looked for on
`192.168.68.x`, not by MAC alone.

## 12. Taking an already-paired camera off the cloud — routing, not re-pairing, 2026-10-23

The BLE provisioning path (§9, §10) is the clean end state: a camera
re-provisioned with our own signalling server registers with us and never
mentions the vendor. It is not working yet.

But a camera that is *already paired* does not need it. Two facts make a
camera avoidable without touching the camera:

* The vendor's addresses are fixed and few: `ipc.gps555.net` →
  `120.26.121.165`, `sig.gps555.net` → `47.110.77.193`, `stun.gps555.net`
  and `ws.gps555.net` → `47.238.125.111`, `stunaddr.gps555.net` →
  `8.130.23.234` (resolved 2026-10-23).
* The camera resolves them by name and connects to them by address, so a
  static route in the router sends that traffic to us instead.

Two details make this work rather than merely redirect packets:

1. **The address has to be local on our host.** `ip addr add <vendor
   ip>/32 dev wlan0` makes the kernel accept the packet and put the
   vendor's address in the reply's source field. A camera that sees the
   reply come from an address it did not expect drops the connection, so
   claiming the address is what preserves the illusion. The broker's
   listeners already use the vendor's ports (80, 8882, 13478, 7080).
2. **DNS is not involved.** The camera resolves the names normally through
   the router and gets the real addresses; the router then diverts the
   packets. The DNS hijack in `ziot_fake_cloud.py` is a second path to the
   same place and is not needed for this.

Router side (TP-Link Deco: *More → Advanced → Static Routing*), one entry
per address, mask `255.255.255.255`, gateway = this host, **interface
LAN** — the packets leave the router's LAN side towards us, and the
gateway must be on the same subnet as the interface that sends them out:

| name | destination | mask | gateway | interface |
|---|---|---|---|---|
| ZIOT ipc | `120.26.121.165` | `255.255.255.255` | `192.168.68.114` | LAN |
| ZIOT sig | `47.110.77.193` | `255.255.255.255` | `192.168.68.114` | LAN |
| ZIOT stun+ws | `47.238.125.111` | `255.255.255.255` | `192.168.68.114` | LAN |
| ZIOT stunaddr | `8.130.23.234` | `255.255.255.255` | `192.168.68.114` | LAN |

The destinations are public addresses, so none collides with the router's
own LAN or WAN (TP-Link rejects those).

Host side is scripted: `python3 ziot_fake_cloud.py --claim-ips` claims the
four addresses and prints these entries; `ziot-broker.service` runs the
broker.

**The gateway address must be fixed, not the DHCP lease.** The first
attempt used the adapter's DHCP address as the gateway — and that adapter
disappeared mid-session (the internal one went away and the USB one took
over), taking the claimed vendor addresses with it and silently returning
the vendor's addresses to the real vendor. The broker now claims its own
fixed address, `192.168.68.114`, independent of which adapter is up, and
`ziot-ips.service` re-claims everything at boot. A route pointing at a
moving address is a route that works until it silently does not.

Running the broker as a service needed one non-obvious step: port 80.
`AmbientCapabilities=CAP_NET_BIND_SERVICE` with `User=`/`Group=` gives an
*unprivileged* service that capability, so the broker keeps running as the
user and its log file stays readable — running it as root instead put the
log where only root could read it, and `systemd`'s private `/tmp` made the
first attempt's log vanish entirely.

**Status.** Verified on this host: all four vendor addresses answer on the
vendor's port 80 with the local API's answers, and the broker's signalling
port answers. **Not yet verified:** a camera actually registering with the
local broker, because the camera on hand is asleep (a battery camera that
was silent for 100 s of capture) and the router routes have not been
installed. Once it does register, its registration traffic is the missing
camera→cloud protocol — the same trick the app capture played in §11, and
the last piece needed for the broker to answer a camera rather than only a
bridge.

**Status update, later on 2026-10-23.** The host side is in place and
verified: all four vendor addresses are claimed on the home LAN
(`wlan0` carries `120.26.121.165/32`, `47.110.77.193/32`,
`47.238.125.111/32`, `8.130.23.234/32` and the broker's own fixed
`192.168.68.114/32`), and `ziot-ips.service` and `ziot-broker.service`
are both active. The DHCP lease of the active adapter has since moved
to `192.168.68.113` — exactly the failure the fixed broker address was
introduced to survive, and the vendor addresses stayed claimed.

The camera still has not registered. The broker log is unambiguous
about it: every entry in it is either our own `curl` or the phone's app
(`User-Agent: curl/8.23.0` against the four vendor addresses, and the
app's `notify-live-event` / `stun-addr` calls). Not one packet is from
the camera. The camera is asleep (a battery camera that was silent for
100 s of capture) and it has never come up on this network.

## 13. The camera's own firmware — obtained, and it names the protocol, 2026-10-23

The app's Dart snapshot holds the vendor's firmware catalogue URL. That
catalogue is the camera's firmware, published by the vendor, versioned
per device type — and it is the one artifact that states the protocol
in the camera's own words.

```bash
curl -s http://ipc.gps666.net/api/v1/firmware-version | head -c 400
```

The response lists **355 entries across 17 device types**
(`TXW817_A` … `TXW817_L_D`, `XF16`, …), each with `version`, `url`,
`crc` and `isLatest`. Our camera reports `TXW817_A_V1.0.12.32` in its
device record, and that exact build is in the catalogue, so the binary
is the camera's own firmware and not an approximation of it:

```bash
curl -o TXW817_A_V1.0.12.32.bin http://ipc.gps666.net/otabin/TXW817_A_V1.0.12.32.bin
# 466960 bytes
```

Note `ipc.gps666.net` is reachable while `ipc.gps555.net` is not, from
this network. The app's API host is the latter — which is why the broker
has to impersonate the address rather than the name (§12).

### 13.1 The container: Taixin XIPH

The file is not a filesystem image. Its header:

| offset | value | meaning |
|---|---|---|
| `+0x00` | `69 5a 00 1c` | magic `iZ`, header size `0x1c` |
| `+0x04` | `18 00 00 00` | load address `0x18000000` |
| `+0x0c` | `00 0c 00 00` | header size `0xC00` |
| `+0x10` | `10 14 07 00` | payload length `0x71410` |

`0xC00 + 0x71410 = 0x72010 = 466 960` — the whole file, so the image is
a 0xC00-byte header plus a flat 0x71410-byte payload loaded at
`0x18000000`. The payload holds code, a vector table of pointers into
`0x180xxxxxx`, literal pools, and readable strings. `binwalk 3.1.0`
recognises only an AES S-box in it; it does not understand XIPH.

### 13.2 The payload's shape — and the code that would not decode

The payload's structure is legible even where its code is not:

| payload offset | content |
|---|---|
| `0x00000`–`0x0007c` | 32-entry handler table (`0x18000a68`, `0x180009b8`, `0x18000a00`) |
| `0x00080`+ | dispatch table of **string pointers**, 40 entries, `0x28` apart |
| `0x5c000`–`0x62000` | the strings (entropy 5.1–5.2 — plain text) |
| `0x70000`+ | padding (entropy 3.8) |

The literal pools at `0x15bb8`–`0x15bec` (the BLE field list) and
`0x17018`–`0x17038` (the same fields again) are little-endian.

**The code section decodes in no standard instruction set.** Tried ARM
(LE/BE), Thumb (LE/BE), MIPS32 (LE/BE) and x86-64 with Capstone 5.0.7
and `llvm-objdump`, plus halfword-swapped variants of each, across the
whole payload and at the handler table's own entry points: no function
prologue, no stack setup, no sensible branch targets anywhere. Entropy
is 6.8–7.0 bits/byte across the code region — compiled-code territory,
not ciphertext — so it is most likely a vendor-specific encoding.

An earlier note here called the architecture MIPS, from a vector table
that "looked like" MIPS and a ratio of 1 802 JAL-class words to 159
BL-class ones. The ratio was chance: MIPS JAL is a 1-in-8 opcode, so
any 460 KB blob yields thousands of them. **The architecture is
undetermined**, and everything below rests on the camera's own strings
and its own answers on the wire — not on disassembly.

### 13.3 What the firmware says — the protocol in the camera's own words

The camera's own log strings are the specification. Payload offsets
below; load address `0x18000000`.

**The BLE provisioning frame** (`0x5f050`–`0x5f2a9`):

```
error, parameter is invalid
error,packet head is invalid!!!
BLE totalLen:%d, offset:%d !!!
error,packet length is wrong!!!
pkt length:%d, dataLen:%d!!!
error,packet xor check is wrong!!!
cmd:%d!!!
ssid:%s!!!
pwd:%s!!!
urlAndPort:%s!!!
p1:%s
port:%d
timezone:%d
bind id:%d
param bind id:%d
unknow command:%d!!!
```

The frame carries **nine** fields, in that order: `cmd`, `ssid`, `pwd`,
`urlAndPort`, `p1`, `port`, `timezone`, `bind id`, `param bind id`.
The literal pool backing this function sits at payload `0x15bb8`–
`0x15bec`, and the pool entries appear in exactly that order — the
compiler's emission order, which is the source order.

> That was the 2026-10-23 reading. §14.0 below corrects it: the pool
> belongs to a parser that serves **two** commands, and the `wifiConnect`
> subset is `cmd, ssid, pwd, urlAndPort, timezone[, regCode]`.

Two other strings settle how the frame is framed: `BLE totalLen:%d,
offset:%d` means the camera **reassembles chunks** until it holds the
whole frame (so a partial write is not an error, it is a wait), and
`packet xor check is wrong` confirms the trailing XOR byte.

**The cloud signalling protocol** (`0x5ff00`–`0x60400`):

```
CUSTOM_PROTOCOL_SendReq / CUSTOM_PROTOCOL_DataCheck / CUSTOM_PROTOCOL_ParseData
cmd:%04X, len:%d, uidLen:%d!
req cmd:%04X, seq:%d len:%d
cSeq:%d, cmd:%d, ack:%d
seq:%d, stun port:%d stun ip:%s
seq:%d, relay port:%d relay ip:%s
error: uid  or uidLen is invalid
uid:%s
totalDataLen:%d, offset:%d
XOR check fail
pSignallingSendMsgFunc is NULL
```

So the camera↔signalling-server protocol is: a `cmd` (16-bit), a
`len`, a `uid` and its length, an XOR byte, and a sequence number —
the same family as the BLE frame, which is why one parser serves both
(`custom_wifi_ble_data_parse` is the BLE instance of it).

**The parameters, and the lever that matters** (`0x5fbd5`–`0x60017`):

```
CUSTOM_PARAM_Init / SetDefaultParam / SaveInfo / GetSsidPwd / GetSigUrl
ssid:%s ,password:%s
parameter init ok / paramVersion / magic_num
custom uid:%s / uidLen:%d
```

`CUSTOM_PARAM_GetSigUrl` is the lever: **the signalling server address
is a stored parameter**, written at provisioning time. Telling the
camera "your signalling server is *us*" is therefore a parameter write,
not a firmware change — which is exactly what the BLE path has to
achieve.

Also present: `custom_wifi_ble_bind_req`, `custom_wifi_ble_parse_error_res`,
`uble_gatt_*` (the firmware carries its own GATT stack, which is why
the camera's GATT database cannot be enumerated), the OTA path
(`ota url:%s:%d`, `file lenth:%d`, `file crcHex:%04X, crcOec:%d`,
`serVersion:%s, devVersion:%s`, `device need to be updated`) and the
event-upload path (`public ip:%s port:%d`, `%s/%d/%d/%d/%d-%d.AVI`).

The full write-up, with the container parse and the string inventory,
is in **`ziot-fw/REPORT.md`**.

## 14. The BLE provisioning frame — what the camera wants, 2026-10-23

§9 reconstructed the frame from the app's assembly. §13 shows the
camera's own field names. Mapping one onto the other:

| firmware field | the frame we send | |
|---|---|---|
| `cmd` | `00 01` | ✓ |
| `ssid` | ssid | ✓ |
| `pwd` | pwd | ✓ |
| `urlAndPort` | sigAddr (`host:port`) | ✓ |
| `p1` | — | another command (§14.0) |
| `port` | — | another command (§14.0) |
| `timezone` | timezone | ✓ |
| `bind id` | — | another command (§14.0) |
| `param bind id` | `regCode` (when present) | different thing |

The four fields we do not send are the reading this section started
from, and §14.0 below supersedes it: the app's own `wifiConnect`
builder has six fields, so `p1`, `port`, `bind id` and `param bind id`
are not part of it at all. Where they *are* used is the camera's
**binding key** — the camera advertises `key` plus 16 ASCII characters
in its ServiceData on `0xa201` (`keyyfg5y34yqxqgd` observed on unit
`140979857781`), and §8 already recorded that this string is the
camera's own binding/registration secret.

### 14.0 Settled by the app's own builder (2026-10-24, Blutter)

The nine-field question above is now **closed, by the app's code**: the
app's `wifiConnect` payload has **six fields, the last optional, and no more**, so
`p1`, `port`, `bind id` and `param bind id` are *not* fields of
`wifiConnect` — they belong to the other commands that share the
camera's parse function.

The builder was read out of the app's AOT snapshot (Blutter, Dart
3.10.8, snapshot `1ce86630892e2dca9a8543fdb8ed8e22` — the same build
this section analyses). `DeviceBluetoothAddController.getApCommand`
calls `CustomBleProtocolPacket::sendApInfoRequest`, which calls
`_handleApRequest` (the payload) and `combineRequestPackage` (the
frame):
```
sendApInfoRequest(1, routeName, wifiPwd, urlAndPort, tz, regCode)
        │        │     │         │          │      │      └─ the nonce
        │        │     │         │          │      └──────── the phone's tz offset
        │        │     │         │          └─────────────── 2 bytes, big-endian
        │        │     │         └────────────────────────── the sig address
        │        │     └──────────────────────────────────── the WiFi password
        │        └────────────────────────────────────────── the SSID ("routeName")
        └─────────────────────────────────────────────────── cmd 1 = wifiConnect
```

`_handleApRequest` UTF-8-encodes exactly **three** strings and
appends them length-prefixed, then the timezone (2 bytes) and the
regCode (4 bytes):

```
payload = 00 01 | <len><ssid> | <len><pwd> | <len><urlAndPort> | <tz:2> | [regCode:4]
```

`combineRequestPackage` builds the frame around it, with
`LEN16 = DLEN + 5`:

```
86 86 | LEN16 | SEQ16 | DLEN16 | payload | XOR8 | 0D
```

**This is byte-for-byte the frame `wifi_connect_frame()` builds.** Our
frame's *shape* was never the problem.

The regCode is confirmed a **nonce, not a credential**:
`DeviceAddData.stringToUint32(text)` is
`sha256(text)` with the digest's bytes 0, 2, 4 and 6 packed
big-endian into a uint32, and the text is the **phone's** UUID
(`DeviceUuid::getUUID`, or the app's saved UUID when the camera is
already in its device list) interpolated with
`DateTime._getCurrentMicros()`. The app appends it for every real
device and omits it only for the placeholder `"FFFFFFFFFFFF"`.

So the only differences between our frame and the app's are the
**values**: the `urlAndPort` (ours is this host's LAN address, the
app's is the cloud's `icpSigAddress`) and the regCode nonce itself.

The app's API surface also names the binding endpoints, which the
broker does not serve: `/v1/ipc/bind`, `/v1/ipc/unbind`,
`/v1/ipc/set-pwd`, `/v1/ipc/reset-pwd`, `/v1/ipc/send-code`,
`/v1/ipc-function/get-sig-ip`. The app keeps the camera's bind
password in its own storage (`init:deviceBindPwd`,
`get:deviceBindNewPwdPlaceHolder`, `deviceBindPwd`).

Where the four extra firmware names come from: the camera's BLE parse
function serves more than one command, and the app sends more than one
request. `sendApInfoRequest` is `wifiConnect` (cmd `1`); the app
also has `CustomBleProtocolPacket::sendDevicePullRequest`, which wraps a
second payload builder, `_handleSetupAPData`, and goes through the same
`combineRequestPackage`. That second builder is where the setup data
(`p1`, `port`, the binding ids) lives. The camera prints the field names
of *both* parsers into one literal pool, which is why §13's inventory
looks like a single nine-field frame. It is not.

**What this means for unit `.107`.** The frame-shape theory is dead, so
the silence of `.107` since 20:39 is not "our frame was malformed". The
frame it received was ours, built in the app's shape. What differed is
inside it, and
two candidates remain:

1. the `urlAndPort` value — the app stores the cloud's `icpSigAddress`;
   we wrote `sig.gps555.net:8882` (which resolves here) and, in earlier
   attempts, `192.168.68.114:8882`;
2. the absence of the regCode — `att_try.py` sends `reg_code=0`, and
   `wifi_connect_frame()` then omits the four bytes. The app appends
   them for every real device and only omits them for the
   `"FFFFFFFFFFFF"` placeholder.

Both are *values*, not layout, and both are testable without guessing:
re-provision with a regCode and with the vendor's own `urlAndPort`, and
compare the camera's behaviour against §14.1's oracle.

The end symbol (§14.1) was the real layout defect, and it is fixed. The
regCode gap is now closed as well: `string_to_uint32()` reproduces
`DeviceAddData.stringToUint32`, and `att_try.py` builds its frame with a
real nonce (`sha256(hostId + micros)`), so what goes over BLE is the
app's frame field for field. What is left is to confirm it by *doing* it:
re-provision a camera with our broker as the signalling server and watch
it register.

### 14.1 The camera's error frame is a usable oracle — and it exposed our own bug

Raw ATT over L2CAP CID 4, one connection per probe:

| write to `0x0008` | the camera's answer |
|---|---|
| 1 byte `00` | `86 86 0008 0001 0003 ffff00 0a 0d` |
| 2 bytes | *silence* |
| the 57-byte §9 frame | *silence* |

Decoded against the firmware's own format: magic `86 86`, `LEN`
counting the bytes after itself (`2+2+3+1 = 8`), `SEQ`, `DLEN`, the
payload, the XOR byte (`0x0a`) and the end byte `0x0d`. The payload
`ff ff 00` is the camera saying **command not understood** — a status
frame, and the same shape the app's `readBluetoothWriteResult` parses.

**The end symbol was the bug.** Running the same probe with and without
the trailing `0x0d` settles it:

| frame | camera's answer |
|---|---|
| `… payload XOR` (no end symbol) | *silence* |
| `… payload XOR 0d` | `payload=ffff00 tail=0b0d` — it answers |
| end symbol present but counted in `LEN` | *silence* |

So the request frame is

```
86 86 | LEN16 | SEQ16 | DLEN16 | payload | XOR8 | 0x0D
LEN16 = 5 + DLEN      (SEQ + DLEN + payload + XOR -- the end symbol is NOT counted)
XOR8  = xor of every preceding byte
```

and `wifi_connect_frame()` in `ziot_fake_cloud.py` was building it
**without the end symbol**. That is why the camera never acted on our
provisioning: it was not ignoring the frame, it was waiting for the
last byte of a frame it had already accepted the length of
(`BLE totalLen:%d, offset:%d` — it reassembles until it has the whole
thing). The builder now appends `0x0d`, and
`tests/test_ziot_fake_cloud.py` pins both the byte and the rule that
`LEN` does not count it.

Two conclusions:

1. **Silence is not acknowledgement.** §10 recorded that "the camera
   acknowledged every write but did not act on it". The camera never
   acknowledged our frame at all — the read that suggested otherwise
   was the broken PDU reader of §10, and the silence was the camera
   waiting for a byte we never sent.
2. **The camera can grade our frames.** A frame it understands produces
   something other than `ffff00`; a frame it does not produces
   `ffff00`; a frame missing its end symbol produces silence. That is a
   complete oracle for the payload layout, and `att_candidates.py`
   drives it.

The nine-field question from §13.3 is **closed by §14.0**: the app's
`wifiConnect` builder has six fields, the app provisioned this camera
successfully with them, and `_handleSetupAPData` — the other request that
shares the framing — is where `p1`, `port` and the two `bind id`s live.

## 15. The camera talks to the broker — the signalling protocol, captured, 2026-10-23

With the router's static routes in place, both cameras on the LAN
(`192.168.68.107` = uid `140979857781`, `192.168.68.108` = uid
`141030191094`) connected to the broker's signalling port (8882) on
their own and started talking. The phone's app, pointed at the broker by
the same routes, asked for the device list and registered a viewer
address (`192.168.68.106:52868`) for `140979857781` through
`send-stun-addr` — all of it against the local broker, with the vendor
unreachable. The static-route route works; the cameras come to us.

### 15.1 The frame, read off the wire

Every camera frame is 56 bytes:

```
86 86 | cmd(2) | len(2) | seq(2) | uidLen(2) | uid(uidLen) | body(32) | xor | 0x0D
        0001     002c     0019..    000c        "140979857781"  version  ..
```

* `cmd` = `0x0001` — the camera's registration (hello) to the signalling
  server, constant;
* `len` = 0x002c = 44 — the bytes that follow the header: the uid (12)
  plus the body (32), also constant;
* `seq` — the camera's own counter, **incrementing on every
  retransmission** (0x19, 0x1a, 0x1b …), about one frame per second;
* `uidLen` = 12, then the uid as ASCII;
* `body` — a fixed 32-byte area, NUL-padded, holding the firmware
  version `TXW817_A_V1.0.12.32` (19 bytes + 13 NULs);
* `xor` — the XOR of every byte before it;
* `0x0D` — the end symbol, the same one the BLE frame ends with.

**The two middle fields were the open question**, and the constant-vs-
incrementing behaviour across retransmissions settles it. Across 1 836
captured frames from one camera, the four header u16s are:

| field | value | behaviour |
|---|---|---|
| 1 | `0x0001` | constant — the command |
| 2 | `0x002c` = 44 | constant — the length of what follows (uid 12 + body 32) |
| 3 | 10, 11, 12, … | **increments on every retransmission** — the sequence |
| 4 | `0x000c` = 12 | constant — the uid length |

So the order is the BLE frame's — magic, LEN, SEQ — and the third field
is the sequence number. `ziot_fake_cloud.py` rebuilds a captured frame
byte for byte, and
`tests/test_ziot_fake_cloud.py::SignallingFrameTests` pins it.

The camera keeps retransmitting because the broker does not answer it at
the application layer yet.

### 15.2 What the server is supposed to answer

The firmware names three messages, in the order they appear in the
binary:

```
req cmd:%04X, seq:%d len:%d        a request from the server
seq:%d, stun port:%d stun ip:%s    where the camera should punch
cSeq:%d, cmd:%d, ack:%d             an acknowledgement
```

They read like log lines, and the first impulse is to send exactly that
text. **That is wrong**, and the camera proved it: more than two
thousand text answers in every plausible spelling (with and without the
uid, with and without the comma, with the vendor's own STUN address and
with the viewer's, padded and unpadded) produced no reaction at all —
the camera kept retransmitting `cmd 0x0001` through every one of them.

The proof that they are *templates for parsed fields* rather than wire
bytes is the sibling string in the same firmware:

```
cmd:%04X, len:%d, uidLen:%d!
```

That one describes the frame header that was read straight off the wire
(`86 86 | 0001 | 002c | …`) — binary u16s, not text. So the reply body
is **binary**, and the search is over how the fields sit and under which
command id. The broker now answers with packed integers.

**A correction worth making explicit.** The camera's STUN calls now
arrive at this host, and it is tempting to read that as the camera
obeying the punch instruction. It is not (yet): `stunaddr.gps555.net`
is one of the four routed addresses, so the camera's "what is my
address?" call is diverted here by the router regardless of what the
signalling connection said. The instruction's effect has to be measured
as *the camera sending media to the viewer's address*, which has not
happened yet.

### 15.3 What the app does with all this

The decompiled app (Blutter, `app/request/socket_handler.dart`) answers
the other half:

* the app connects to the camera over **TCP `localGatewayIP:16532`** and
  speaks the same `CustomBleProtocolPacket` frames;
* the commands are `enum CmdAPType` — `wifiConnect 01`, **`pullVideo
  02`**, `light 03`, `infraredLight 04`, `switchStatus 05`, `restart 06`,
  `reset 07`, `sdState 08`, `sdReset 09`, `sdVideoRecorde 10`,
  `originHorizontal 11`, `originVertical 12`, `phoneTimestamp 13`,
  `appVoice 14`, `motorControl 15`, `speakControl 16`, `lampLight 17`,
  `deviceType 18`, `definition 19`, `pushVideo 20`;
* the app's answer to `pullVideo` is what carries the video URL, and the
  `handleDeviceDirectResponse` map has keys `cmd`, `result`, `state`,
  `uid`, `deviceType`, **`url`**, `rtpPush`, `sd_card`, `sd_status`,
  `sd_total`, `sd_available`, `sd_progress`, `light`, `infrared_light`,
  `origin_h`, `origin_v`, `lampLight`, `definition`;
* the app's own BLE frame is `86 86 | LEN | SEQ | DLEN | payload | XOR`
  and its BLE *reply* is `… | XOR | 0x0D` — the end symbol the app
  checks for and the one §14.1 found the camera requires.

So the "no cloud" system has three parts, and two of them are now known
in their exact bytes: the HTTP API (§11), the camera's signalling frame
(this section), and the BLE provisioning frame (§14). What is missing is
the *content* of the signalling answers — which body the camera accepts
— and then the punch itself.

### 14.2 Corrections to earlier claims in this report

* **§9's frame is byte-correct *as the app builds it* and wrong *for
  the camera*.** It was never observed on the air, and the firmware's
  field list is the camera's own statement of what it wants.
* **§10's "the camera acknowledged every write"** was the broken PDU
  reader (§10) reporting a truncated first byte as a response.
* **§10's GATT results** ("the camera refuses GATT discovery", "write to
  `0x000c` rejected with invalid attribute value length") were read with
  that same broken reader. L2CAP over `SOCK_SEQPACKET` truncates on a
  short `recv()` and discards the rest of the datagram, so every
  multi-byte PDU was read as its first byte; ATT handles are
  little-endian, and the CCCD value is two bytes, not four. `0x000c`
  does not exist — the real answer is `01 12 0c 00 0a`, *attribute not
  found*.
* **The API host looked unreachable** when the answer was that *our own
  broker* was answering it.

### 14.3 What is left, in order

1. **The camera must wake up.** Everything below needs it awake and
   advertising; it is a battery camera that was silent for 100 s of
   capture.
2. **The frame itself is settled** (§14.0): it is six fields, exactly
   what we send, and the regCode nonce now goes out with it. What to test
   next is *values* — the `urlAndPort` the camera stores and the
   `regCode` — not layouts, against the oracle of §14.1.
3. **Implement `CUSTOM_PROTOCOL`** in the broker: `cmd:%04X, len:%d,
   uidLen:%d` plus XOR plus uid, answered per
   `CUSTOM_PROTOCOL_ParseData`'s vocabulary (`req cmd`, `cSeq/cmd/ack`,
   `stun port/ip`, `relay port/ip`). This is the camera↔signalling-server
   protocol that §12 is still waiting to see, and the firmware now
   describes it well enough to implement without the camera.

## 16. The firmware disassembled — the push is `0x8005`, 2026-10-24

> The canonical, double-checked write-up of this protocol is
> **`ziot-fw/SIGNALLING_PROTOCOL.md`** (source of truth). This section is the
> running-record summary; where they differ, that document wins.

The firmware's code "decoded in no standard ISA" because the earlier passes
tried the wrong CPU. **The TXW817 core is a C-SKY CK803** (Taixin's product
page + teardowns), ABIv2, little-endian, image loaded at `0x18000000`.
Capstone and Ghidra have no C-SKY support — which is the entire reason §13.2
could not decode it. With the chip vendor's own GNU toolchain
(`csky-abiv2-elf-objdump`, from `Taixin-Semiconductor/XuanTie-CSKY-Toolchains`,
mirror in `cjacker/wm_sdk_w80x`) the payload disassembles cleanly:

```
csky-elfabiv2-objdump -D -b binary -m csky -EL --adjust-vma=0x18000000 payload.bin
```

The reset vector at `0x18000a68` is textbook CK803 startup (`psrclr ee,ie`,
control-register setup, a bss-clear loop, init `bsr`s, `br .`), and `lrw`
literal loads resolve, so string references are recoverable. The Taixin
TXW81x SDK is also in hand as C source (`divadiow/txw81xcam`); its
`sdk/app/spook` is the RTSP/RTP/JPEG engine the firmware's `spook_rtsp_*`
is built on, but the gps555 cloud protocol below is Z-IOT's own and is only
in the stock binary.

### 16.1 The signalling parser and dispatcher

`CUSTOM_PROTOCOL_ParseData` (code at `~0x18018094`) scans for the `86 86`
magic (`cmpnei r3, 134`) and reads the header exactly as §15.1 found:
offset 2-3 `cmd`, 4-5 `len`, 6-7 `seq`, 8-9 `uidLen`, then the uid. Every
received frame is handed to a dispatcher, **`CUSTOM_TASK_CmdProHandle`**
(code at `0x1801b1fc`), which switches on the 16-bit `cmd` (held in r5) —
a compiler binary-search over the server→camera command ids
(`0x8000`, `0x8001`, `0x8002`, `0x8004`, `0x8005`, `0x8021`, `0x8101`,
`0x8201`-`0x8206`, `0x8301`, `0x8601`, `0x9003`, …). This is confirmed
against a known case: `cmd 0x8001` → handler `0x1801b66c`, which parses
`cSeq:%d, cmd:%d, ack:%d` — the ack frame. (C-SKY `cmpnei rX,imm` sets
C=(rX≠imm); `bf target` is taken when rX==imm.)

### 16.2 `cmd 0x8000` *arms* the relay (handler `0x1801b55a`)

For `cmd 0x8000`, the handler reads the frame body at these fixed offsets
(this is the body of the registration answer, §2.6):

| body offset | field | stored to |
|---|---|---|
| 0-1   | u16 seq | — |
| 2-21 (20B) | relay IP, ASCII | `memcpy(dev+76, body+2, 20)` |
| 0x18-0x19 | u16 **relay port** | `dev->0x60` |
| 0x1a-0x39 (32B) | stun host, ASCII | `memcpy(dev+166, body+0x1a, 32)` |
| 0x3a-0x3b | u16 stun port | `dev->0xc6` |

It then sets `dev->0x2c = 3` and calls **`spook_rtsp_set_relay_info`**
(`0x180132d4`), which builds the publish URL
`rtsp://<relay_ip>:<relay_port>/live/<name>` and hands it to
`rtsp_relay_info_set` (`0x18012538`). **It only stores the target — it does
not connect.** Note the camera takes the relay port from body offset
**0x18** (the vendor's *second* port slot, 10000 in the capture), not 0x16.
The broker already writes the relay port into both 0x16 and 0x18
(`vendor_registration_answer(..., relay_port, relay_port)`), so the armed
URL is correct (`rtsp://192.168.68.114:8554/live/<uid>`). This also explains
§2.10: even against the real vendor, a `0x8000` with a real relay never made
the camera publish — `0x8000` only arms.

### 16.3 `cmd 0x8005` *starts* the push (handler `0x1801b8f0`) — the last piece

`cmd 0x8005` routes to `0x1801b8f0`, which logs **`CUSTOM_TASK_RelayPro`**
and **`rtsp_p2p_start`**, sets `dev->0xc8 = 2`, and kicks the relay task
(`bsr 0x18038938`, arg 1), tearing down any previous session via `dev->0xcc`.
**The handler reads nothing from the frame body** — `0x8005` is a pure
trigger. The relay task then connects to the armed URL and publishes
(`CUSTOM_TASK_RelayPro` → `start push, session id:%s`,
`rtsp://%s:%d/live/%s`, `m=video RTP/AVP 26` / `m=audio RTP/AVP 0` over
`RTP/AVP/TCP`).

`0x8005` is `CameraEventType.relay` (eventType 5, §2.2): in the vendor flow
the app's `notify-live-event?eventType=5` makes the cloud send the camera
`0x8005`. So the complete media trigger is two frames over sig:

1. **`0x8000`** (register answer, already sent) — arms the relay target.
2. an unsolicited **`0x8005`** (empty body) — starts the push: the camera
   `sendto`s UDP RTP to the armed address (the `CUSTOM_TASK_RelayPro`
   strings are log tags, not a task — **§6 of `ziot-fw/SIGNALLING_PROTOCOL.md`**).

### 16.4 Why the 462-candidate sweep missed it

The broker's blind sweep used the camera's *own* request ids (`0x02`,
`0x14`, `0x20`, `0x4e`, … the `CmdAPType`/dispatch-table range) OR'd with
`0x8000`, with elaborate relay/stun bodies. The actual trigger is a bare
**`0x8005`** with no body, sent *after* a correct `0x8000` arm. It was never
in the candidate set, and no body shape could have substituted for it.

### 16.5 The broker changes this implied

Implemented in `ziot_fake_cloud.py` (2026-10-24):

* The `0x8000` register answer **echoes the camera's `seq`** in the body's
  first two bytes (`vendor_registration_answer`, capture-confirmed: body
  lead values 1, 4, 6, 7 track the register) and arms a real target:
  `arm_target()` puts the bridge's UDP media socket — or a viewer's public
  pair; `--relay-port` only as an experiment — into the relay slots, so the
  answerer is a UDP RTP sink, not `ziot_relay.py`.
* `push_relay_trigger` builds exactly that frame —
  `86 86 | 80 05 | 00 0c | <seq> | 00 0c | <uid> | <xor> | 0d` (len counts
  the uid only; empty body; same XOR/`0x0d` framing as every other frame) —
  **but is held behind `--relay-start` and off by default**. The first live
  test that fired it after `0x8000` and on every keepalive tick made the
  camera reconnect-loop and never reach the connected state, with zero
  media. The validated sequence to look for is a camera that settles into
  one sig session and pushes to the armed port — watched via
  `tcpdump` on that port, then the bridge's `rx_datagrams`.

All of §16 is recovered from the stock firmware by static disassembly and is
pending live confirmation against the camera; no vendor contact is involved.

## 17. The real blocker is the STUN answer; the direct path is `0x8101` + `0x8004`, 2026-10-24 evening

> Running-record summary. The full write-up, with addresses and disassembly,
> is **`ziot-fw/SIGNALLING_PROTOCOL.md` §12–§15**; where they differ, that
> document wins. Nothing in the code has been changed for these findings yet.

Inputs: the opencode lab runs in `/tmp/opencode` (`labv7.py`/`labv9.py` and
their logs), the broker log `ziot-broker.err`, the vendor wiretap
`~/ziot-wiretap.log`, the phone capture `PCAPdroid_24_out._13_47_51.pcap`,
and `fw.asm` (the full CK803 listing of `payload.bin`).

### 17.1 Findings

* **The camera can never use the broker's STUN answer.** Its STUN client
  (`CUSTOM_TASK_StunXorAddr`, `0x180190e8`) accepts only a Binding Success
  that carries a standard **XOR-MAPPED-ADDRESS, attribute `0x0020`**. The
  broker sends `0x0001`/`0x0004`/`0x8020` (the §11 shape, which was the
  vendor's answer to the *phone's* RFC 3489 request) and no `0x0020`. The
  camera's request is RFC 5389 (magic cookie) with a fixed transaction id.
* **What a failed STUN does.** `CUSTOM_TASK_ReportLocalAddr` (`0x1801b054`)
  runs after every `0x8021` clock answer. It tries STUN 4 times, each from
  a new random RTP port `rand() % 9999 + 50001`, then logs
  `stun function, device need reboot` and **closes the sig socket**. The
  broker log matches exactly:
  * bursts of ≤4 requests per 16 s cycle;
  * a new port for every request;
  * every port between 50035 and 59924.

  The "short transaction" of §16/`SIGNALLING_PROTOCOL.md` §0 is this
  failure path, not normal behaviour.
* **A successful STUN would produce `cmd 0x0003`,** the camera's local and
  STUN-learned ip:port (47-byte body). There are **zero** `0x0003` frames in
  the vendor wiretap and in the broker log.
* **Direct live view uses three frames.**
  * **`0x8101`** (`CUSTOM_TASK_P2PConnReq`) carries the viewer's public and
    private ip:port, in 20-byte ASCII slots at body 20 and 42 with u16 ports
    at 40 and 62. It re-runs STUN + `0x0003`, chooses a pair by the app's
    own subnet rule, and starts the P2P thread. That thread binds the RTP
    port, sets `peerToPeerFlag = 1`, and receives the viewer's hellos.
  * **`0x8004`** (`CUSTOM_TASK_playCtrl`) carries a u16 at body `0x14`:
    1 PlayLiveview (only when `peerToPeerFlag == 1`, else
    `p2p is not finish`), 2 PlayPause, 3 stop. Those are the
    `CameraEventType` start/pause/stop values, which is why there is no
    `0x8003`.
  * **`0x8002`** stamps the camera's liveness tick: **(inferred)** the
    keepalive.

  `0x8005` remains the relay start.
* **The dispatch case list in §16.1 / `SIGNALLING_PROTOCOL.md` §4 was
  slightly wrong.** `0x8102` and `0x8302` are only binary-search bounds;
  `0x0091` and `0x0092` are real cases.
* **Lab v7/v8 used a sum checksum** (already recorded in
  `SIGNALLING_PROTOCOL.md` §0), so they neither parsed camera frames nor sent
  valid ones. v8's log is `labv7.py` re-run. **Lab v9's single `0x8005` went
  into a stale socket**: it had been idle 2 min while newer cycles ran on
  other sockets. It was not a live session.
* **Phone capture, 13:47.**
  * It is the first wire record of **`eventType=5`**. The app went
    `send-stun-addr` → 4× `stun-addr` → relay → speakOff → stop, with no
    `eventType=1` and no hello to the camera.
  * Its STUN requests reached the broker and were answered, but the answers
    never reached the phone. They left from `192.168.68.111` (the kernel's
    route source) instead of the vendor address the request was sent to,
    because the broker's STUN socket is bound to `0.0.0.0`. The app fell
    back to `stun.12voip.com`.
  * No camera was on the broker's sig port during the capture: lab v8 held
    `.114:8882`. The bridge was overwriting the phone's viewer registration
    for the same uid.

### 17.2 Corrections to earlier sections

* §16.3 and `SIGNALLING_PROTOCOL.md` §0 present `0x8005` as *the* media
  trigger. It is the **relay** trigger. The direct path, the one the
  2026-08-29 streams used, is `0x8101` + `0x8004`.
* §2.2's `CameraEventType` gains its wire counterparts (inferred, §13.6 of
  the protocol document): keepAlive `0x8002`, start/pause/stop `0x8004`
  with value 1/2/3, relay `0x8005`. `send-stun-addr` maps to `0x8101`.
* §11's STUN shape (`0x0001`, `0x0004`, `0x8020`) is right **for the phone
  only**. For the camera it is the reason nothing works.
* §15.3 calls the camera's `0x0002` "pullVideo". In the sig protocol it is
  the camera's heartbeat; it is also sent on every 5th `0x8002`.

### 17.3 What it implies

Ordered, in `SIGNALLING_PROTOCOL.md` §15:

1. A `0x0020` XOR-MAPPED-ADDRESS in the broker's STUN answer to the camera.
   The cheapest live check is whether `0x0003` appears and the sig session
   stops closing at ~6 s.
2. Replies sent from the address the request was sent to.
3. Parse `0x0003` and serve `stun-addr` from it.
4. Send `0x8101` on `send-stun-addr`, with the bridge's LAN address in both
   pairs.
5. `0x8004`/1 on start (after the P2P thread is up), `0x8002` on keepAlive.
6. Lab hygiene: one owner of `:8882`, one viewer per uid, and route
   `47.97.71.82`, the sig address the vendor's `0x8000` and the wiretap
   actually use.

### 17.4 The vendor-era captures, re-read

`PCAPdroid_27_ago._21_00_10.har` and `PCAPdroid_29_ago._15_13_27.pcap`
(in `~/Downloads/Telegram Desktop`) are two working live views against the
real vendor. Read against the firmware, they settle three things
(`SIGNALLING_PROTOCOL.md` §14.1):

* **`seqNo` is per registration**, not "+1 per call" as §11 put it. Each
  `send-stun-addr` gets a new N (186, 187, 188). `stun-addr` reaches N
  about 3 s later, once the camera has reported. The app punches only when
  `stun-addr` shows its own N. The broker's shared counter never does,
  which is why the 13:47 app session went straight to relay.
* **`IpcPublicIP/Port` is the camera's STUN pair.** Its ports never match
  the phone's. The broker's "public pair = viewer's" reading of the 29 Aug
  capture was wrong.
* **The app's order is hellos first, then `eventType=1`**, with
  `mediaState` turning 3 in between. In the firmware `mediaState` is the
  camera's own `0x0004` status report, item 2 = `dev->0x2f4`. It is 3 once
  the P2P socket is up after `0x8101` and 0 after StopPlay
  (`SIGNALLING_PROTOCOL.md` §13.7). That answers §5's open question about
  the server-side meaning, at least for "3".

Smaller points:

* The camera's `IpcPrivatePort` (52508, 53693) stayed fixed across
  reports, and 53693 is the first RTP port both cameras drew today.
* The app's keepAlive cadence is 5 s.
* The vendor answered `send-cmd` with an object (`uid`, `timeId`, `msg`),
  not `"ok"` as §11 says.

### 17.5 First frames through the broker, and the 36 s stop (2026-10-24 16:14)

With the `0x0020` STUN attribute and the direct chain in the broker
(`0x8101` → `0x8004` value 1 → hello punch), camera .107 streams to the
bridge with no vendor cloud:

* **The STUN fix worked.** Every cycle brings a `0x0003` report, and the
  camera keeps one sig socket open instead of closing it at ~6 s.
* **The bridge receives the camera's own RTP**, video and audio, at
  7–8 fps. The first frames arrived at about 16:14:30.
* **The relay was not needed.**

It stops by itself **exactly 36 s after each start**. The firmware's
media state machine (`0x1801a628`) logs `live heartbeat timeout, stop
live` when the tick at `dev->0x34` is 36 s old.
* While a live view plays, only the server's `0x8002` refreshes that
  tick. The P2P thread, which stamps it for every viewer datagram, has
  exited by then.
* The broker only logs the bridge's `notify(keepAlive)` and never sends
  `0x8002`.
* So the camera reports `mediaState` 3 → 0 → 1 → 0, and the bridge
  restarts it after 10 s of silence: about 36 s on, 14 s off.

The fix is to send `0x8002` for each `eventType=0`; the camera ignores
its body.

The 36 s check runs only while the sig link is up. With no broker
running (16:14:32–16:16:25) the camera streamed for 2 minutes and
stopped the moment it reconnected.

The same trace settles `mediaState` (§5, §17.4):
* **The wire id is 3**; the call argument is 2. A single report reads
  `01 00 03 00 VV`.
* **Values:** 0 stopped, 1 direct live, 2 relay live, 3 P2P socket
  waiting for the hello, 4 the other StopPlay branch.

Camera .108 has been off the network since 16:14:26, so its zero frames
are not evidence against the path.

Details and addresses: `ziot-fw/SIGNALLING_PROTOCOL.md` §0 (16:14 box),
§13.4, §13.5, §13.7, §15 items 6, 7, 9 and 10.

**Live runs, 22:28–23:41** (`ziot-fw/SIGNALLING_PROTOCOL.md` §16):
- The `0x8002` heartbeat held `.108` for 16 unbroken minutes.
- The bridge's ~60 s retry fires a fresh `0x8101` that stops a view still
  coming up. The broker's 6 s fallback play still fires after it.
- `.107` also fails once without that collision.
- The vendor app now skips P2P and asks for relay straight away.
- The unknown-command `vendor-ack` body is 6 bytes. The vendor's 1,138
  wiretap acks are all 5 bytes, but they answer only `0x0002`/`0x0004`.
  The vendor's answer to `0x0005` was never captured.
- Two lab conditions mimicked protocol faults: the laptop idle-suspending,
  and the cameras sending but not receiving.

## 18. The app's own connect flow, decoded, 2026-10-25

Full write-up with addresses: `ziot-fw/SIGNALLING_PROTOCOL.md` §17. From
the same Blutter snapshot. The headline findings:

* **`eventType=4` (`connected`) is what these units send at live start.**
  `CameraAPI.requestSendConnectState` sends `start` (1) only when
  `CameraInfoModel.supportCacheStun()` is true, and that is
  `deviceNeedUpdate(version, "TXW817_A_V1.0.11.46")` for an X5 — i.e. only
  for firmware *older* than V1.0.11.46. On `TXW817_A_V1.0.12.32` the app
  sends `connected` (4), never `start` (1). The broker must map 4 like 1.
* **The cached-stun fast path is dead code for our units**
  (`isCacheStun()` requires that same `supportCacheStun()`), and with it
  the "app remembers the P2P failure" theory of the 23:37 relay fallback.
  What remains proven: the punch times out → callback `0` → log
  `公网 IP 获取异常, 走转发` → `CameraConnectState.relay`. The exact
  23:37 entry point (no `send-stun-addr` was ever sent) is still open.
* **The punch needs ~10 datagrams back from the camera** before the app
  logs `P2P Serve 连接设备: 成功 success` (a counter seeded at 20,
  decremented per received datagram, success at ≤ 9). Viewer punch port is
  `Random().nextInt(10001) + 50000`; the punch repeats every 1 s.
* **`natType` is never read** by the app (only `fromJson`/`toJson`).
* **`GET /v1/ipc-function/get-sig-ip`** (param `supportLoadBalance=1`) is
  parsed as `code == 400` → `data` string → `SpUtil.putString(
  "icpSigAddress", …)`. `icpSigAddress` is the `urlAndPort` the BLE
  `wifiConnect` frame carries (§14.0), so serving that endpoint makes the
  app provision cameras with the bridge's sig address.
* **`lang`** is a `POST /v1/login` body field; dictionaries come from
  `/v1/lang/applang` (answer shape unrecovered; app continues without it).
* `handleActionForPrepare` refuses to connect unless the device row's
  `mediaState` reads `"0"` (`isFree`), logging `设备繁忙, 请稍后再试`.
* Teardown (`requestSendStop`) sends `eventType=3` (stop) — `pause` (2)
  only for CacheStun devices — matching §7's bridge change.

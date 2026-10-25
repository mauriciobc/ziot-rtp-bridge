# ZIOT camera: firmware, protocol, and the path off the cloud

**Date:** 2026-10-23
**Status:** the camera firmware was obtained and analysed; the broker now
speaks the vendor's HTTP API byte-for-byte; the camera still has not been
made to talk to it. This document is the working record of what is
verified, what was wrong earlier, and what is still open.

---

## 1. What was asked

> The goal is NO APP and NO CLOUD.

That means the camera must never talk to the vendor again: it has to
register with a broker we run, and stream to a bridge we run. The mobile
app is not part of the final system.

Two ways to get a camera off the cloud:

| Route | Mechanism | Status |
|---|---|---|
| **A. Re-provision over BLE** | tell the camera "your WiFi is X, your signalling server is *us*" | frame format recovered, write transport working, **camera still ignores the frame** |
| **B. Routing redirect** | the camera keeps its vendor configuration; the router sends its cloud traffic to us | **routes installed, broker answering — camera has not registered yet** |

Route B needs no BLE at all and is the one to validate first. Route A is
the cleaner end state (the camera is *told* about us rather than being
tricked by a router).

---

## 2. The firmware

### 2.1 Where it came from

The vendor's own firmware catalogue, found by reading the app's Dart
snapshot (`libapp.so` from `split_config.x86_64.apk`):

```
GET http://ipc.gps666.net/api/v1/firmware-version
→ {"code":200,"msg":"查询成功","data":{"count":355,"list":[
     {"deviceType":"TXW817_A","version":"TXW817_A_V1.0.12.32",
      "url":"http://ipc.gps666.net/otabin/TXW817_A_V1.0.12.32.bin",
      "path":"/home/rayjay/ipc/otabin/TXW817_A_V1.0.12.32.bin",
      "crc":...,"isLatest":0}, ...]}}
```

355 entries, 17 device types (`TXW817_A` … `TXW817_L_D`, `XF16`).
`TXW817_A_V1.0.12.32` is **exactly the version our camera reports**
(`version` field of the device record), so the binary is the camera's own
build, not an approximation.

Downloaded: **466 960 bytes**, `http://ipc.gps666.net/otabin/TXW817_A_V1.0.12.32.bin`.

Note: `ipc.gps555.net` (the API host the app uses) is unreachable from
this network — every one of its ports is filtered. `ipc.gps666.net` and
`web.gps555.net` are reachable, which is how the catalogue was found.
The broker therefore must impersonate the `gps555` names via the router's
static routes (route B) — the DNS hijack alone is not enough because the
app's API host resolves to `120.26.121.165`.

### 2.2 Container format

Taixin **XIPH**. Header at offset 0:

```
+0x00 69 5a 00 1c   magic "iZ" + 0x1c
+0x04 18 00 00 00   load address 0x18000000
+0x0c 00 0c 00 00   header size 0xC00
+0x10 10 14 07 00   payload length 0x71410
```

`0xC00 + 0x71410 = 0x72010 = 466 960` — the whole file. So the image is
a 0xC00-byte header plus a 0x71410-byte payload loaded at `0x18000000`.
The payload is **not** a filesystem: it is a flat code+data image with
readable strings, a vector table of pointers into `0x180xxxxxx`, and
literal pools.

### 2.3 The payload's shape (and what could not be decoded)

The payload's layout is legible even where its code is not:

| payload offset | content |
|---|---|
| `0x00000`–`0x0007c` | 32-entry handler table: `0x18000a68` (reset), `0x180009b8` (all others), `0x18000a00` |
| `0x00080`+ | dispatch table of **string pointers**, 40 entries spaced `0x28` apart, pointing into the string region |
| `0x5c000`–`0x62000` | the strings themselves (entropy 5.1–5.2, i.e. plain text) |
| `0x70000`+ | padding (entropy 3.8) |

The literal pools at `0x15bb8`–`0x15bec` (the BLE field list) and
`0x17018`–`0x17038` (the same fields again) are little-endian: the pool
entry for `"BLE totalLen:%d, offset:%d !!!"` is the bytes `cf f0 05 18`
= `0x1805f0cf` read little-endian.

**The code section does not decode in any standard instruction set.**
Tried: ARM (LE and BE), Thumb (LE and BE), MIPS32 (LE and BE) and
x86-64, with Capstone 5.0.7, `llvm-objdump`, and a halfword-swapped
variant of each — over the whole payload and at the vector table's own
entry points. Nothing produces coherent code: no function prologue, no
stack setup, no sensible branch targets. Entropy is 6.8–7.0 bits/byte
across the code region (compiled code is typically 6.5–7.0; encrypted
data is ~8.0), so the region is neither plain code nor ciphertext — most
likely a vendor-specific encoding or an obfuscation this session did not
break.

> **Superseded — see §2.5.** That conclusion was an artefact of the
> method, not a property of the binary. The entropy figure quoted above
> (6.8–7.0) is the signature of *compiled code*, not of protection;
> ciphertext measures 7.95–8.0 with a flat histogram. The "no standard
> ISA decodes it" result came from a single linear sweep that started at
> payload offset 0 — the exception table — and stopped at the first word
> Capstone refused. §2.5 re-measures the payload region by region and
> finds the code where it actually is.

An earlier note in this file called the architecture MIPS, on the
strength of a vector table that "looked like" MIPS and a word-count
ratio of JAL-class to BL-class words. That ratio was a coincidence of
random bytes (MIPS JAL is a 1-in-8 opcode, so a data blob produces
thousands of them), and the table is better explained as an ARM-style
exception table. **The architecture is undetermined.** The protocol facts
below come from the camera's own strings, which are unambiguous, and
from the camera's own answers on the wire.

`binwalk 3.1.0` sees only an AES S-box in the container — it does not
understand XIPH. REA's `inspect_firmware_regions` needs
`REA_BINWALK_COMMAND` set for the calling server, which is not configured
here; the container was parsed by hand instead.

### 2.4 What the firmware says (its own log strings)

These are the camera's own diagnostics, and they are the protocol
specification. Addresses are payload offsets; the load address is
`0x18000000`.

#### The BLE provisioning frame (payload `0x5f050`–`0x5f2a9`)

```
error, parameter is invalid
error,packet head is invalid!!!
BLE totalLen:%d, offset:%d !!!          <- chunk reassembly
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

So the BLE provisioning frame carries, in this order: **cmd, ssid, pwd,
urlAndPort, p1, port, timezone, bind id, param bind id** — nine fields.
The literal pool for this function is at payload `0x15bb8`–`0x15bec`, and
the pool entries appear in exactly that order, which is the compiler's
emission order and therefore the source order.

> **Correction (2026-10-24, from the app's builder).** These nine names
> are not the fields of one frame. The camera's BLE parse function serves
> **two** app requests — `wifiConnect` (cmd `1`) and the setup/pull
> request — and the compiler emitted both parsers' literals into one
> pool. The app's `wifiConnect` payload is six fields (`cmd`, `ssid`,
> `pwd`, `urlAndPort`, `timezone`, optional `regCode`); `p1`, `port`,
> `bind id` and `param bind id` belong to the other command. See
> `DECOMPILATION_REPORT.md` §14.0 for the recovered builder.

#### The cloud signalling protocol (payload `0x5ff00`–`0x60400`)

```
CUSTOM_PROTOCOL_SendReq
CUSTOM_PROTOCOL_DataCheck
CUSTOM_PROTOCOL_ParseData
XOR check fail
cmd:%04X, len:%d, uidLen:%d!
error: uid  or uidLen is invalid
uid:%s
totalDataLen:%d, offset:%d
req cmd:%04X, seq:%d len:%d
cSeq:%d, cmd:%d, ack:%d
seq:%d, stun port:%d stun ip:%s
seq:%d, relay port:%d relay ip:%s
```

This is the protocol the camera speaks to the **signalling server**
(`sig.gps555.net:8882`) — the server the vendor's own app hands to the
camera at provisioning time as `icpSigAddress`, and which our broker
must impersonate.

#### Wi-Fi, parameters and the cloud

```
CUSTOM_PARAM_Init / SetDefaultParam / SaveInfo / GetSsidPwd / GetSigUrl
ssid:%s ,password:%s
parameter init ok / paramVersion / magic_num
custom uid:%s / uidLen:%d / new addr uid write / old and new addr uid write
custom signalling receive thread
pSignallingSendMsgFunc is NULL
sigServerStatus
ota url:%s:%d
file lenth:%d / file crcHex:%04X, crcOec:%d
serVersion:%s, devVersion:%s
device need to be updated
```

`CUSTOM_PARAM_GetSigUrl` is the key one: **the signalling server address
is a stored parameter**, set at provisioning time. That is exactly the
"no cloud" lever — get the camera to store *our* sig URL.

#### The BLE stack

`uble_gatt_*`, `uble_recv_l2cap_data`, `uble_value_set`,
`CUSTOM_BLE_ParseData`, `CUSTOM_BLE_Start`, `custom_wifi_ble_data_parse`,
`custom_wifi_ble_bind_req`, `custom_wifi_ble_parse_error_res`.

So the firmware has its own GATT stack, which is why the vendor's GATT
database is unusual (read requests refused) and why a GATT client
cannot enumerate it.

### 2.5 Re-measuring the payload: where the code actually is

**This section supersedes the "genuinely protected" verdict of §2.3.**
The claim rested on two measurements, and both were artefacts of how
they were taken.

**Entropy was read backwards.** Real encryption (AES, any stream cipher)
produces 7.95–8.0 bits/byte with a flat byte histogram. Compiled code
produces 6.4–7.0 with a strongly skewed histogram. The morning quoted
6.8–7.0 as evidence of protection; that is the code figure. Measured
per region, with the histogram's chi-square against uniform:

| payload region | H (bits/byte) | chi²/n | 0x00 share | what it is |
|---|---|---|---|---|
| `0x0000`–`0x1000` | 3.990 | 77.9 | 55.2% | exception table + pointer tables |
| `0x1000`–`0x5c000` | 6.999 | 3.303 | 9.01% | **not code, not ciphertext** |
| `0x5c000`–`0x62000` | 5.116 | 10.140 | 4.24% | format strings |
| `0x62000`–`0x6c000` | 6.559 | 5.356 | 11.29% | **the application code** |
| `0x6c000`–`0x71410` | 5.977 | 11.811 | 18.81% | tables (AES S-box at `0x6f738`) |
| *random 64 KB (reference)* | *7.997* | *0.004* | *0.39%* | *ciphertext would look like this* |

The disputed `0x1000`–`0x5c000` region has `chi²/n = 3.30` where
ciphertext has 0.004, and `0x00` at 9.0% where ciphertext has 0.39%.
It is **not encrypted**. Its top bytes are `0x00` 9.01%, `0x20` 3.45%
(space), `0x6c` 2.62% ('l'), `0xff` 2.24% — the space and 'l' rates
are ASCII rates, and the region's 16-bit units repeat (`0xe3ff` 5751
times, `0x1806` 1689 times) where code would not. It decodes to 67.6%
printable when read as text. Whatever it is, it is a **data section**,
not protected code.

**The ISA sweep was done on the wrong bytes.** Capstone's `disasm()`
stops at the first word it cannot decode, so a linear sweep from offset
0 measured the exception table, not the code. Re-measured with
`skipdata` enabled, per region, against a random baseline of the same
bytes, the `0x62000`+ region decodes cleanly as **Thumb**, and the
`0x1000` region's 40 dispatch targets decode as Thumb too.

> **Superseded 2026-10-24 — see `ziot-fw/SIGNALLING_PROTOCOL.md`.** The code is
> **C-SKY CK803**, not Thumb (capstone has no C-SKY, so the Thumb "decode" here
> and the "undecodable" verdict of §2.3 are both artefacts of the wrong ISA).
> The table at payload `0x80` is the **interrupt vector table**: its 40 entries
> are ISRs (`nie; ipush; push r15; bsr <enter>; <load callback>; jsr; bsr
> <exit>; ipop; nir`), not command handlers. The paragraph below is kept for
> history; do not treat its byte reading as correct.

**The code is Thumb, and the dispatch table is at payload `0x80`.**
The table holds 40 pointers; the targets are 0x28 (40) bytes apart; and
every target opens with the same 8 bytes:

```
60 14   str   r0, [r1, #4]
62 14   str   r2, [r1, #4]
d0 14   str   r0, [sp, #16]
00 e0   b     <body>
```

That is a Thumb function prologue in a switch/case over 40 command
ids. So the camera's command dispatch **is** statically recoverable,
and §2.4's "the cmd id itself is only in the encrypted code" is wrong.

**What the firmware's own words say the push carries.** Two log sites
name the fields of the same command:

```
0x60662  seq:%d, relay port:%d relay ip:%s
0x606a3  seq:%d, stun port:%d stun ip:%s
0x6117b  relay port:%d relay ip:%s          (a second parse path)
```

and the consumer is named too: `spook_rtsp_set_relay_info` (0x5dd1c),
`rtsp_relay_info_set` (0x5d22b), `rtsp://%s:%d/live/%s` (0x5dd63),
`CUSTOM_TASK_RelayPro` (0x63074). **The camera is an RTSP publisher**:
it publishes to a relay, it does not serve. That is the decisive
architectural fact, and it is what §10 turns into a working design.

### 2.6 The vendor's 0x8000 answer, read from the captured frame

The registration answer was re-parsed from the wiretap capture
(315 real `0x8000` frames from the vendor's own server, 2026-10-23)
rather than from the morning's description. The body is 94 bytes:

```
000  00 01 31 35 36 2e 32 34 36 2e 31 36 2e 31 31 34  ..156.246.16.114
010  00 00 00 00 00 00 02 2a 27 10 73 74 75 6e 2e 67  .......*'.stun.g
020  70 73 35 35 35 2e 6e 65 74 00 00 00 00 00 00 00  ps555.net.......
030  00 00 00 00 00 00 00 00 00 00 34 a6 34 37 2e  ..........4.47.9
040  37 2e 37 31 2e 38 32 00 00 00 86 86 80 00 00 6a  7.71.82........j
```

Read as the flat-slot layout §2.4 describes, the fields land exactly:

| offset | field | value in the capture |
|---|---|---|
| 0 | u16 lead | 1 |
| 2 | relay ip, 20 bytes | `156.246.16.114` |
| 22 | u16 relay port | 554 |
| 24 | u16 relay port 2 | 10000 |
| 26 | stun host, 32 bytes | `stun.gps555.net` |
| 58 | u16 stun port | 13478 |
| 60 | sig ip, 32 bytes | `47.97.71.82` |
| 92 | u16 sig port | 8882 |

So the layout is confirmed, and the relay the vendor hands the camera is
`156.246.16.114:554` (service) / `:10000` (media) — **a TCP port the
camera connects to as an RTSP publisher**. The broker currently puts
the *viewer's UDP port* in those two slots, which no RTSP client can
use; that is a real defect, and §10 fixes it. The apparent "tags"
`0x02` and `0x34` are the relay port's high byte and the '4' of
"47.97.71.82", as §2.4 already noted.

---

### 2.7 The camera's command table, recovered from the firmware

> **Superseded 2026-10-24 — this whole section is wrong.** The ids `0x20–0x31,
> 0x36–0x49, 0x4E–0x4F` below were derived by disassembling the `0x80` table as
> C-SKY command handlers, but that table is the **interrupt vector table** (§2.5
> correction). The real command ids are the signalling `0x80xx` set
> (`SIGNALLING_PROTOCOL.md §4`) and the local `CmdAPType` set (`0x01–0x14`); the
> media-start command is `0x8005`, not anything in `0x20–0x4F`. The broker's
> `_PUSH_CMD_IDS` were seeded from this mistaken table, which is why its sweep
> never worked. Kept for history.

The dispatch table at payload `0x80` holds 40 pointers whose targets are
40 bytes apart, each opening with the same eight Thumb bytes:

```
60 14   str   r0, [r1, #4]
62 14   str   r2, [r1, #4]
d0 14   str   r0, [sp, #16]
00 e0   b     <body>
```

Decoding each handler on its own 16-bit grid and collecting the
immediates gives each one a **unique pair of command ids**, and the
pairs run in sequence:

| handler | ids | | handler | ids |
|---|---|---|---|---|
| `[20]` | `0x20`, `0x21` | | `[30]` | `0x3A`, `0x3B` |
| `[21]` | `0x22`, `0x23` | | `[31]` | `0x3C`, `0x3D` |
| `[22]` | `0x24`, `0x25` | | `[32]` | `0x3E`, `0x3F` |
| `[23]` | `0x26`, `0x27` | | `[33]` | `0x40`, `0x41` |
| `[24]` | `0x28`, `0x29` | | `[34]` | `0x42`, `0x43` |
| `[25]` | `0x2C`, `0x2D` | | `[35]` | `0x44`, `0x45` |
| `[26]` | `0x2E`, `0x2F` | | `[36]` | `0x46`, `0x47` |
| `[27]` | `0x30`, `0x31` | | `[37]` | `0x48`, `0x49` |
| `[28]` | `0x36`, `0x37` | | `[38]` | `0x4E`, `0x4F` |
| `[29]` | `0x38`, `0x39` | | | |

So the camera's accepted command ids include `0x20`–`0x31`, `0x36`–`0x49`
and `0x4E`–`0x4F`, and `0x0020` — the id the broker already answers as
"pushVideo" — is the first of them.

**What the push carries.** Two log sites name the fields of the same
command, with the same `seq`:

```
0x60662  seq:%d, relay port:%d relay ip:%s
0x606a3  seq:%d, stun port:%d stun ip:%s
```

They are adjacent in the string table and share the `seq`, so they are
two halves of one frame: **the push carries the relay address and the
STUN address together**, each as an ip plus a big-endian u16 port, in
the same flat style as the vendor's `0x8000` answer. A punch that
carries only one of them is missing half the command.

That is the gap in the rotation of §10: the candidates so far have been
single-address bodies. The next shape to try is the pair.

### 2.8 What the app's own capture says the live-view does

The app's capture (`PCAPdroid_29_ago._15_13_27.pcap`, 721 packets,
phone side, 2026-08-29) is the only record of what the *viewer* does
when the user presses live. Read out of it with a stdlib pcap parser,
the sequence is exactly four requests:

```
POST /api/v1/cmd/send-cmd        {"cmdType":"20","uid":"141030191094"}
GET  /api/v1/ipc/send-stun-addr?appId=…&publicIp=177.221.120.116
     &publicPort=64550&privateIp=192.168.18.30&privatePort=55795&uid=…
GET  /api/v1/ipc/notify-live-event?appId=…&eventType=1&uid=…
GET  /api/v1/ipc/141030191094
```

and the vendor's replies to the second one are:

```json
{"IpcPublicIP":"177.221.120.116","IpcPublicPort":65003,
 "IpcPrivateIP":"192.168.18.75","IpcPrivatePort":53693,
 "receiveTs":1788027219,"seqNo":115}
```

**The reply mixes the two ends.** The *public* pair is the viewer's —
the app's own NAT address, echoed back — while the *private* pair is
the **camera's** (`192.168.18.75:53693`). The broker had them the
other way round: it answered the public fields with the camera's
address. Fixed in `_stun()`.

Two further facts fall out of the same capture:

* `cmdType "20"` is `speakOff` (the app's own enum, §1.1 of the
  decompilation report), so the `send-cmd` in the live-view path is
  **not** what starts the video.
* the order matters: the viewer's address is registered
  (`send-stun-addr`) **before** `notify-live-event(eventType=1)`. The
  camera is told where to send only after the viewer has said where
  it is listening — so the broker's `push_start` on `eventType == "1"`
  is the right trigger, and the address it must carry is the one just
  registered.

### 2.9 The `0x8601` configuration answer was two bytes out

Reading the vendor's own frames out of the signalling tap rather than
the log's ASCII column exposed a real defect in the broker.

The vendor answers the camera's `cmd 0x1601` (its configuration
request) with `cmd 0x8601` and a body of **43 bytes**: 37 NUL bytes
and three big-endian u16s.

```
offset 37..38   27 10 = 10000    the relay port the vendor uses
offset 40..41   00 0f = 15       ?
offset 42..43   00 0f = 15       ?
```

(The byte that looks like a varying timestamp after those three is the
frame's own XOR check, which changes with the sequence number — reading
it as payload is what made this look like a 45-byte body.)

The broker was sending **35** NUL bytes, so the three ports sat two
bytes earlier than the camera expects. Fixed in
`vendor_config_answer()`; the broker's answer is now byte-for-byte the
vendor's (the XOR aside).

This is the kind of defect the log column hid: the ASCII column
truncates at 96 bytes, so the frame looked 118 bytes and the two NULs
were invisible.

### 2.10 What the vendor's own bytes do — and do not — do

The fair question about the broker is whether the vendor's frames
actually *open* the camera. The answer, measured, is:

**The vendor's frames do not open this camera either.**

* The app's capture (`PCAPdroid_29_ago._15_13_27.pcap`) is a live-view
  session: it carries `send-stun-addr` and `notify-live-event(eventType=1)`
  against the real vendor. It contains **zero RTP packets** and **zero
  RTSP connections** — the app asked for the video and the vendor never
  delivered media to it.
* The vendor's relay (`156.246.16.61:554`) answers `OPTIONS 200` from
  `Server: ZLMediaKit` but its `DESCRIBE` is empty: a relay waiting for
  a publisher that never publishes.
* The signalling tap recorded 2 871 vendor frames and **no push of any
  kind** — only `0x8000`, `0x8021`, `0x8001` and `0x8601`, the answers
  to the camera's five questions.

So "reproduce the vendor byte for byte" is necessary but not
sufficient: it would reproduce a session that produces no media either.
That reframes the goal. The end state is not "the broker answers like
the vendor" but "**the camera publishes to a relay we run**", which
needs something the vendor's own flow does not do for these units.

**What the broker now does byte for byte.** Both answers were
re-captured from the live vendor (the tap's own log truncated its hex
column at 96 bytes, which is what hid the difference) and compared:

| frame | vendor | broker | |
|---|---|---|---|
| `0x8000` registration answer | 118 bytes, body 94 | identical | ✓ |
| `0x8601` configuration answer | 148 bytes, body 43 | identical | ✓ |
| `0x8021` clock answer | 28 bytes | identical | ✓ |
| `0x8001` go-ahead | `00 03 00 02 00` / `00 04 00 04 00` | identical | ✓ |

Two real defects were found and fixed by that comparison:

* the `0x8000` body was 96 bytes where the vendor's is 94 — the
  signalling address slot was 32 bytes with a port of its own, and the
  frame's `len` field was 22 bytes short;
* the `0x8601` body was 41 bytes where the vendor's is 43 — two NUL
  bytes short, so the ports sat two bytes earlier than the camera
  expects.

And one thing that looked like a bug and is not: the `lead` field of
the `0x8000` is a **counter** (the captures show 1, 4, 6, 7, 11, 16,
31, 55 …), not a constant.

### 2.11 The two local paths, measured against the live cameras

Both routes that avoid the cloud were probed on the real cameras on
2026-10-24.

**The camera's own TCP port (16532) is closed.** The app connects to
`localGatewayIP:16532` and speaks the same `CustomBleProtocolPacket`
frames, with `pullVideo 02` as the command that returns the video URL.
A full sweep of all 65 535 TCP ports on both cameras found **no open
TCP port at all** — 16532, 7070 and 8266 (the ones the decompilation
report mentions) are all closed. That port is only up while the camera
is in its AP/provisioning mode.

**The BLE command channel is reachable but the camera refuses the
connection.** The camera advertises itself and is visible:

```
Device E0:44:40:60:68:82  ZIOTA_140979857781
  UUID: Unknown  (0000a201-0000-1000-8000-00805f9b34fb)
```

and `btgatt-client` answers `Connection refused` — exactly what the
decompilation report recorded: outside pairing mode the camera refuses
the connection. So the BLE route needs the camera in pairing mode, which
is a physical action (reset button).

**What the firmware says about pairing mode.** The mode is commanded
by the firmware's own `custom_wifi_*` layer, not by the signalling
protocol: `custom_wifi_workmode_switch`, `switch mode:%d`, `change wifi
mode`, `wifi set ap mode`, `ap mode restart`, `sta mode restart`. So the
AP mode — and with it the open BLE channel — is entered by the camera's
own WiFi state machine.

**And the BLE frame's field list is confirmed in the binary.** The
literal pool at payload `0x17018` is the provisioning frame's field
table, and it reads exactly as the report describes:

```
0x18017018  'custom_wifi_bind_req'
0x1801701c  'ssid:%s!!!'
0x18017020  'pwd:%s!!!'
0x18017024  'urlAndPort:%s!!!'
0x18017028  'p1:%s'
0x1801702c  'port:%d'
0x18017030  'timezone:%d'
0x18017034  'bind id:%d'
```

next to `custom_wifi_data_parse` — so it is the BLE parser, not a
signalling command. That means "make the camera publish" cannot be
commanded from the signalling server; it has to come from the BLE
channel (in pairing mode) or from the camera's own session daemon.

### 2.12 The SDK: the architecture, and the media format in source

The vendor publishes an SDK for this chip family — `github.com/divadiow/txw81xcam`
— and it settles two questions that no amount of static analysis of the
blob could.

**The architecture is C-SKY**, not any of the ISAs that were tried:

```
project/utilities/gcc_csky.ld     MEMORY { FLASH: ORIGIN = 0x18000000, … }
sdk/chip/txw81x/vectors.S         the trap vector table
csky/                              the toolchain and the kernel
```

So the payload's code is **CKCore** — an ISA no disassembler in this
session had support for, which is why "no standard instruction set
decodes it" was true and uninformative at the same time. The `0x18000000`
load address in the link script matches the container header, so the
container was parsed correctly all along.

**The camera's RTP/JPEG format is in source**, in
`sdk/app/spook/rtp-jpeg.c` — the same `jpeg_process_frame` the
firmware's own strings name:

```c
/* Main JPEG header */
vhdr[0] = 0;      /* type-specific: non-interlaced frame */
vhdr[1..3]        /* fragment offset */
vhdr[4] = type;
vhdr[5] = 255;    /* Q: dynamic quant tables */
vhdr[6] = width;  /* in eighths of a pixel — see below */
vhdr[7] = height;
```

and the width is divided before it is written:

```c
out->width  >>= 3;
out->height >>= 3;
```

which is exactly what the bridge's reassembler assumes
(`width, height = p[6] * 8, p[7] * 8`). The media path is therefore
**confirmed against the vendor's own source**, not inferred.

The SDK does not carry the vendor's cloud-signalling layer (it has the
RTSP *server* side, `sdk/app/spook/`, but not `spook_rtsp_set_relay_info`
or the `CUSTOM_TASK_*` dispatcher of this camera's firmware), so the
push format is still not in it.

---

## 3. What the camera actually answers on BLE





This was the decisive experiment of the day. Raw ATT over L2CAP CID 4,
one connection per probe:

| write to `0x0008` | camera's answer |
|---|---|
| 1 byte `00` | `86 86 0008 0001 0003 ffff00 0a 0d` |
| 2 bytes | *silence* |
| 57-byte frame (`86 86 …`, **no** `0d` terminator) | *silence* |

Decoding the one-byte answer against the firmware's own format
(`CUSTOM_PROTOCOL`: `cmd:%04X, len:%d, uidLen:%d`):

```
86 86 | 0008 | 0001 | 0003 | ffff00 | 0a | 0d
magic   len=8   seq=1  dlen=3  payload  xor  end
```

`LEN` counts the bytes after the LEN field (`2+2+3+1 = 8`), the last
byte is the XOR of everything before it (`0x0a`), and the frame ends with
`0x0d`. The payload `ff ff 00` is the camera saying **"command not
understood"** — a status frame, exactly the shape the app's
`readBluetoothWriteResult` parses.

So the camera:

* **answers** a malformed one-byte write with an error frame;
* **waits** for more data when the head is `86 86` — "BLE totalLen:%d,
  offset:%d" means it reassembles chunks until it has the whole frame;
* never answered our 57-byte frame, because it was **incomplete**: the
  request frame ends with `0x0d`, and ours did not. The *layout* was
  right — the firmware's nine names span two commands (correction in
  §2), and the app's `wifiConnect` carries six, exactly what we send.

Note the camera's error frame has the same `86 86` framing as the app's
request frame, and it ends with `0x0d` — the byte the app's parser
checks (`data[9+dlen] == 0x0D`). The app's *request* frames omit the
trailing `0x0d`; the camera's *replies* include it.

---

## 4. The BLE frame we send — correct, but it was incomplete

Reconstructed from the app's Dart assembly (Blutter), the frame is:

```
86 86 | LEN16 | SEQ16 | DLEN16 | payload | XOR8 | 0D
LEN16 = 5 + DLEN          (counts SEQ + DLEN + payload + XOR, not the end symbol)
payload = 00 01 | <len>ssid | <len>pwd | <len>sigAddr | tz(2, +0x8000 if negative)
                 | [regCode(4, optional)]
```

This is byte-verified against the app's own captured frame (both the
plain and the regCode variant, reproduced exactly by
`ziot_fake_cloud.py`'s `wifi_connect_frame()`).

The firmware's field list says the camera expects: `cmd, ssid, pwd,
urlAndPort, p1, port, timezone, bind id, param bind id` — but, as the
correction in §2 notes, that list spans **two** commands. For
`wifiConnect` the fields are the first four plus `timezone` and the
optional `regCode`.

Mapping what we send onto what it wants:

| firmware field | we send | note |
|---|---|---|
| cmd | `00 01` | ✓ same meaning (wifiConnect) |
| ssid | ssid | ✓ |
| pwd | pwd | ✓ |
| urlAndPort | sigAddr (`host:port`) | ✓ (matches the `icpSigAddress` field) |
| p1 | — | other command (setup/pull), not `wifiConnect` |
| port | — | other command (setup/pull), not `wifiConnect` |
| timezone | tz | ✓ (minutes, negative biased `+0x8000`) |
| bind id | — | other command (setup/pull), not `wifiConnect` |
| param bind id | — | other command (setup/pull), not `wifiConnect` |

The two "bind id" fields are the camera's **binding key** — the camera
advertises `key` + 16 ASCII chars in its ServiceData (`keyyfg5y34yqxqgd`
observed on unit `140979857781`), and §8 of the decompilation report
already notes this is the camera's own binding/registration secret. The
app reads it from the advertisement. It is *not*, however, part of the
`wifiConnect` frame: the app's `regCode` (a nonce,
`sha256(phoneUuid + micros)`) is what it appends there.

**Correction to the earlier conclusion:** the frame shape was never the
defect — `wifi_connect_frame()` reproduces the app's frame byte for byte,
as §4's byte-level comparison already showed. The end symbol was the
real defect (see `DECOMPILATION_REPORT.md` §14.1), and it is fixed. What
remains is value-level: the `urlAndPort` we choose and the missing
`regCode` (`att_try.py` passes `0`, which omits the four bytes).

---

## 5. The broker (route B) — what works

`ziot_fake_cloud.py` answers the vendor's HTTP API with shapes taken
from a live app capture (`PCAPdroid_29_ago._15_13_27.pcap`, phone side,
721 packets, `ipc.gps555.net` = `120.26.121.165`):

* **headers** — `Server: nginx/1.24.0 (Ubuntu)`, `Content-Type:
  application/json; charset=utf-8`, `Access-Control-Allow-Origin: *`,
  `Cache-Control: no-cache, no-store, max-age=0, must-revalidate`,
  `Expires: Thu, 01 Jan 1970 00:00:00 GMT`, `Last-Modified`,
  `X-Content-Type-Options: nosniff`, `X-Xss-Protection: 1; mode=block`;
* **envelope** — `{"requestId": <uuid>, "code": 200, "msg": …, "data": …}`;
* **device list** — `{"count", "pageIndex", "pageSize", "list", "showAd"}`,
  with the vendor's own quirk that `count` is **0** while `list` holds the
  camera;
* **device record** — 46 fields in the vendor's order, `onlineState`/
  `mediaState` as **strings**, `isShowRemoteService`/
  `isRemoteServiceExpired` as **booleans**, counters as numbers;
* **`send-stun-addr`** returns `IpcPublicIP/Port`, `IpcPrivateIP/Port`,
  `receiveTs`, `seqNo`; **`stun-addr`** returns the same minus
  `receiveTs` plus `updateTime` — the two endpoints differ, and that
  difference was a bug until the capture was read;
* **the 301** — `GET /api/v1/ipc/?…` answers `301 Moved Permanently`
  with `Location:` and a 71-byte HTML body; the app follows it;
* **the package record** answers `msg: "查询成功"`, not `"OK"`;
* **STUN** — `stunaddr.gps555.net:13478` is a real STUN server and the
  camera needs its answer to learn its own address. Our answer matches the
  vendor's byte for byte (same header, cookie, transaction id,
  `MAPPED-ADDRESS`, server-address attribute) except the `0x8020`
  attribute's port, which is the vendor's NAT-mapped port — a value no
  server can derive; ours is the peer's source port, the same value on a
  LAN.

`VendorConformanceTests` in `tests/test_ziot_fake_cloud.py` pins every
one of these points, so the contract cannot drift silently.

The bridge talks to the broker through the new `api_base` config key and
can be pointed at the vendor's own address:

```json
{"token": "…", "user_id": 3132031, "api_base": "http://120.26.121.165/api"}
```

Verified: `--list-cameras` lists the camera from the local broker, and a
full bridge run performs `send-stun-addr` → `notify` → `stun-addr` →
punch against it (`/health`: `endpoint_kind: private`, `addr` taken from
the broker). No media flowed, because the address was a placeholder.

---

## 6. Router setup (route B)

The camera's cloud traffic is diverted by static routes in the Deco
(*More → Advanced → Static Routing*), one per vendor address, mask
`255.255.255.255`, gateway = the broker host, **interface LAN**:

| Name | Destination network | Subnet mask | Default gateway | Interface |
|---|---|---|---|---|
| ZIOT ipc | `120.26.121.165` | `255.255.255.255` | `192.168.68.114` | LAN |
| ZIOT sig | `47.110.77.193` | `255.255.255.255` | `192.168.68.114` | LAN |
| ZIOT stun+ws | `47.238.125.111` | `255.255.255.255` | `192.168.68.114` | LAN |
| ZIOT stunaddr | `8.130.23.234` | `255.255.255.255` | `192.168.68.114` | LAN |

The host must **own** those addresses, or the camera's packets are
dropped: `ip addr add <vendor ip>/32 dev <home-lan-iface>` for each.
`ziot-ips.service` does this at boot, on whichever interface holds the
home LAN (it must not be the camera's own AP).

The broker's own address is fixed at `192.168.68.114` (a `/32` on the
home-LAN interface) so the router's gateway never moves when an
adapter's DHCP lease changes. `ziot-broker.service` runs the broker with
`AmbientCapabilities=CAP_NET_BIND_SERVICE` so it can bind port 80 while
still running as an ordinary user with a readable log.

---

## 7. What is still missing, and the order to close it

1. **The camera never registers.** The routes are in, the broker answers,
   but the camera has not talked to it. Most likely the camera is
   asleep (a battery camera that was silent for 100 s of capture), or it
   needs its cloud to be reachable to complete a wake sequence.
   *Next:* watch the broker log with the camera woken; the signalling
   port 8882 is where the camera will appear, and the firmware's
   `CUSTOM_PROTOCOL_ParseData` tells us the frame shape
   (`cmd:%04X, len:%d, uidLen:%d`, XOR-checked, uid-prefixed).
2. **The BLE frame's field list — resolved, see §4's correction.** The
   nine names span two commands; `wifiConnect` is six fields and our
   builder matches the app's byte for byte. The camera accepted our
   frame (status `0x00`). What is still open is the *values* we send
   (`urlAndPort`, the omitted `regCode`).
3. **The signalling protocol implementation.** `CUSTOM_PROTOCOL_*` is
   readable in the firmware; the broker needs to answer it.

---

## 8. Corrections to earlier work in this session

* **ATT PDU reading was broken.** The first scripts read PDUs byte by
  byte; L2CAP is `SOCK_SEQPACKET`, so a short `recv()` *discards* the
  rest of the datagram. Every multi-byte PDU was truncated to its first
  byte — which is why a write looked like it produced a `1b` notification
  and why the MTU exchange looked like `03 17 00` instead of `03 02 17 00`.
  Also: ATT handles are little-endian, and the CCCD value is **two**
  bytes, not four.
* **"The camera refuses GATT discovery"** was measured with the broken
  reader; the firmware confirms the camera implements its own GATT stack
  (`uble_gatt_*`), and the vendor's report §8 already recorded that the
  database could not be enumerated.
* **"A write to `0x000c` is rejected with invalid attribute value
  length"** — that handle does not exist; the error was
  `01 12 0c 00 0a` = *attribute not found*.
* **The claim that the app's frame was byte-verified** held only for the
  *builder against the assembly*. The frame was never observed on the
  air, and the firmware's field list appeared to contradict it — but the
  contradiction was the field list's, not the builder's: those nine names
  are two commands' worth of literals. The app's builder has since been
  recovered in full (Blutter, `DECOMPILATION_REPORT.md` §14.0) and it
  agrees with `wifi_connect_frame()`.
* **The vendor's API host appeared unreachable** when the answer was that
  *our own* broker answered it (the local claim made the address
  resolve to us).
* **The DNS hijack answer was malformed** — the question section
  omitted QTYPE and QCLASS and `ANCOUNT` did not match the records
  sent, so every reply was rejected as malformed. Now fixed and pinned
  by tests.

---

## 9. Files

| Path | What |
|---|---|
| `ziot_fake_cloud.py` | the broker: HTTP API, DNS hijack, STUN, BLE provisioning frame |
| `ziot_rtp_bridge.py` | the bridge; `api_base` points its signalling at the broker |
| `ziot-broker.service` | broker service (port 80 via `CAP_NET_BIND_SERVICE`) |
| `ziot-ips.service` | claims the vendor addresses on the home-LAN interface |
| `ziot-fw/TXW817_A_V1.0.12.32.bin` | the camera's firmware (466 960 B) |
| `ziot-fw/payload.bin` | its XIPH payload, loaded at `0x18000000` (MIPS) |
| `ziot-fw/findfn.py`, `ziot-fw/disasm2.py` | firmware string/code lookup helpers |
| `att_tool.py`, `att_probe.py`, `att_prov.py`, `att_try.py`, `att_listen.py`, `att_arm.py` | raw-ATT tools for the camera's BLE |
| `DECOMPILATION_REPORT.md` §8–§12 | the running decompilation record |
| `ziot_relay.py` | the local RTSP relay the camera publishes to |
| `ziot_wiretap.py` | the signalling tap that captures the vendor's own push |
| `tests/test_ziot_relay.py` | the relay's round trip, publisher to reader |

---

## 10. The path off the cloud that the firmware itself points to

The decisive fact is in §2.5: **the camera is an RTSP publisher**. Its
own string table names every step of that flow:

| payload offset | string | what it is |
|---|---|---|
| `0x5dd1c` | `spook_rtsp_set_relay_info` | takes the relay {ip, port, name} from the server |
| `0x5dd63` | `rtsp://%s:%d/live/%s` | the URL it publishes to |
| `0x5d6fc` | `start push, session id:%s` | it opens the publish session |
| `0x5d889` | `relaySession:%s` | the session it gets back |
| `0x5d830` | `rtsp://%s:%d/webcam/jpeg_dvp` | the path it serves to peers |
| `0x5cd08` | `m=video %d RTP/AVP 26` | JPEG video |
| `0x5cc00` | `m=audio %d RTP/AVP 0` | PCMU audio |
| `0x5d40a` | `RTP/AVP/TCP` | the transport it offers |

And the vendor's registration answer (captured byte for byte, §2.6)
hands the camera a **relay address** in a fixed slot: a 20-byte ASCII IP
and two big-endian u16 ports — `156.246.16.114`, 554, 10000.

So the camera's media path needs no push command at all. The server
tells the camera where to publish; the camera connects there as an RTSP
client and streams. That is the whole design, and it is the vendor's
own design — reproduced, not invented.

Two consequences for the broker, both now implemented:

* **`0x8000` must carry a TCP address the camera can connect to.**
  The broker used to put the *viewer's UDP media port* in that slot,
  which no RTSP client can use. `ziot_fake_cloud.py` now takes
  `--relay-port` and puts the local relay's address there.
* **the device list must carry the relay address.** The bridge reads
  `relay_ip` from the device-list row and plays
  `rtsp://<host>:<port>/live/<uid>`; the row now carries
  `relay_ip` as `host:port`.

`ziot_relay.py` is the local relay: it accepts the camera's `ANNOUNCE`
(with its SDP) and `RECORD`, and serves `DESCRIBE`/`SETUP`/`PLAY` to
the bridge over RTSP interleaved. It is a transport swap only — the
media is RTP with JPEG video (PT 26) and PCMU audio (PT 0), exactly
what the bridge's reassembler and µ-law decoder already expect, so no
payload is touched.

The round trip is proven in `tests/test_ziot_relay.py`: a synthetic
publisher announces, records and streams; the **production**
`RelayStream` of the bridge opens the relay, plays it, and reassembles
the JPEGs.

### What this needs from the camera

One thing the camera must do on its own: open the publish session. Its
own flow is `ANNOUNCE` → `RECORD` → interleaved RTP, and the firmware
strings show the RTSP server side (`handle_DESCRIBE`, `handle_SETUP`,
`rtsp_handle_msg`) alongside the publisher side. If the camera needs a
server push before it publishes, that push is the same unknown as
before — but the *format* of the push is no longer needed to get
media, because the relay address alone may be enough to make it
publish.

### 10.1 Where it stands, measured

The chain is built and verified end to end, except for the one link
that needs the vendor's own push:

| link | state | evidence |
|---|---|---|
| camera → broker's signalling port | **works** | the camera registers and runs the whole handshake (`0x0001` → `0x0021` → `0x0002` → `0x0004` → `0x1601`) against the local broker; 43 registrations on 2026-10-24 alone |
| broker → camera answers | **works** | byte-for-byte the vendor's own frames, re-captured from the live vendor (§2.10) |
| broker → camera's push | **unproven** | 462 candidates (42 command ids × 11 body shapes) fired at the live camera; none made it publish |
| broker → device list carries the relay | **works** | `relay_ip: "192.168.68.114:8554"` in the device-list row |
| bridge → reads `relay_ip` | **works** | `rtsp://192.168.68.114:8554/live/<uid>` |
| bridge → relay | **works** | the relay answers the bridge's `DESCRIBE` with 404 — reachable, nothing published yet |
| relay → camera publishes | **not reached** | no connection on the relay's port |
| camera → media | **not reached** | `rx_datagrams: 0` |
| camera's own TCP port 16532 | **closed** | a full 65 535-port sweep found no open TCP port on either camera |
| camera over BLE | **refuses the connection** | it advertises `ZIOTA_140979857781` on `0xa201`, and `btgatt-client` answers `Connection refused` outside pairing mode |

So the open question is exactly one thing: **what makes the camera
open a publish session.** Everything either side of it is in place and
measured, and the two local routes that could answer it — the camera's
own TCP port and its BLE command channel — are both closed on these
units, the first because the camera is in station mode and the second
because it refuses connections outside pairing mode.

> **Resolved 2026-10-24 — see `ziot-fw/SIGNALLING_PROTOCOL.md`.** The
> firmware was disassembled (C-SKY CK803; the "undecodable" verdict of
> §2.3 was just the wrong ISA — capstone/Ghidra have no C-SKY). The
> camera opens the publish session on an unsolicited **`cmd 0x8005`**
> over sig:8882 (handler `0x1801b8f0` starts `CUSTOM_TASK_RelayPro`);
> `cmd 0x8000` beforehand *arms* the relay URL (handler `0x1801b55a` →
> `spook_rtsp_set_relay_info`). The §10 candidate sweep missed it because
> it used the camera's local AP command ids (`0x20–0x4F`), not the
> `0x80xx` signalling ids. That document is now the source of truth.

Two findings from the day's captures narrow it further:

* the app's own live-view sequence (§2.8) is `send-cmd(speakOff)` →
  `send-stun-addr` → `notify(eventType=1)` → device detail. The
  broker already answers all four, in that order, with the vendor's
  own bytes.
* the camera sends only five commands, always the same ones
  (`0x0001`, `0x0021`, `0x0002`, `0x0004`, `0x1601`). It never asks
  for a URL, so it is waiting for the server to tell it where to
  publish.

The one thing the captures cannot supply is the push the vendor sends
at that moment: the app's capture is from the phone's side and carries
only the HTTP API, and the signalling tap captured 2 871 vendor frames
without ever seeing a live-view request (`0x8000`, `0x8021`, `0x8001`
and `0x8601` only). Catching it needs a live-view pressed against the
tap — which `ziot_wiretap.py` does.

---

1. **The camera never registers.** The routes are in, the broker answers,
   but the camera has not talked to it. Most likely the camera is
   asleep (a battery camera that was silent for 100 s of capture), or it
   needs its cloud to be reachable to complete a wake sequence.
   *Next:* watch the broker log with the camera woken; the signalling
   port 8882 is where the camera will appear, and the firmware's
   `CUSTOM_PROTOCOL_ParseData` tells us the frame shape
   (`cmd:%04X, len:%d, uidLen:%d`, XOR-checked, uid-prefixed).
2. **The BLE frame's field list — resolved, see §4's correction.** The
   nine names span two commands; `wifiConnect` is six fields and our
   builder matches the app's byte for byte. The camera accepted our
   frame (status `0x00`). What is still open is the *values* we send
   (`urlAndPort`, the omitted `regCode`).
3. **The signalling protocol implementation.** `CUSTOM_PROTOCOL_*` is
   readable in the firmware; the broker needs to answer it.

---

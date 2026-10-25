# ZIOT / gps555 camera↔cloud signalling protocol — source of truth

**Status:** 2026-10-24. Recovered by static disassembly of the stock camera
firmware (`TXW817_A_V1.0.12.32`), cross-checked against live wiretap captures
of the real vendor and against the Taixin TXW81x SDK source. Where a claim is
an inference rather than a directly observed fact it is marked **(inferred)**.
Nothing here required contacting the vendor.

This document is the authoritative description of the protocol the camera
speaks to its signalling server on TCP port 8882, and in particular **what
makes the camera send media**. It supersedes the scattered notes in
`README.md`, `DECOMPILATION_REPORT.md` (§11–§16) and `ziot-fw/REPORT.md` where
they disagree.

---

## 0. The answer in one paragraph

> **⚑ Update 2026-10-24 22:28 - camera .108 streams, end to end.**
> The direct chain works, with the vendor's own protocol shapes. Measured
> on camera `141030191094` (`.108`), 8.0-8.4 fps, continuous:
>
> 1. **The `0x8001` ack must carry the camera's own counter.** The body
>    is `cSeq:2 | cmd:2 | ack:1`, and in **all 1134** acks of the 2 h
>    vendor wiretap the body's first u16 equals the camera's *latest*
>    sequence number for that command -- 535 x `00 03 00 02 00` and 535
>    x `00 04 00 04 00`, with a tail of `00 04 00 02 00`, `00 05 00 04
>    00`, `00 08 00 02 00`. The frame's *header* sequence echoes the
>    frame being answered, and can be one behind. Ours were constant,
>    so no ack we sent ever matched; the camera answered by re-asking
>    (thirty `0x0002`s in a burst) and then going silent.
> 2. **The play follows the camera's own report.** The camera's `0x0004`
>    carries the media state (wire id 3); **3 = "P2P socket bound,
>    waiting for the viewer"** -- the precondition `0x8004` PlayLiveview
>    checks (§13.3). The `0x8004` must not be fired on a timer.
> 3. **The hello comes first, at the camera's freshest RTP port.** The
>    `0x8101` makes the camera re-run STUN, draw a new RTP port and
>    report it (`0x0003`); the hello goes there *before* the play,
>    because PlayLiveview closes the socket the camera was waiting on
>    (§13.2, §13.3, §14.1).
> 4. **Unknown commands get the vendor's shape, not the candidate
>    sweep.** A camera command with no vendor answer (`0x0005` and
>    friends) used to get one of the sweep's frames, including a
>    `0x8001` with **no body at all** -- which the camera's
>    `cSeq/cmd/ack` parser cannot read. It is now a `0x8001` with the
>    camera's number and its command id. (The camera's `0x0005` is
>    *not* fatal: `.108` sends one every session and keeps streaming.)
> 5. **The `0x8002` heartbeat keeps the view open.** `re_rendezvous`
>    stayed at 84 for the whole run: no 36 s stop, no bursts.
>
> The wire, for the record (`.108`, 22:28):
>
> ```
> 22:28:17  0x0001 register        -> 0x8000 arm + 0x8101 viewer
> 22:28:19  0x0004  id3 = 3        (P2P socket bound)
> 22:28:19  STUN from :50293      (the new RTP port)
> 22:28:21  hello x3 -> :50293    (the fresh port, first)
> 22:28:21  0x8004 play-liveview  (p2p bound)
> 22:28:22  0x0004  id3 = 0 -> 1   (direct live)
> ```
>
> Verified end to end: the bridge's `/cam/<uid>` serves baseline
> **640x480** JPEGs (66 frames in 8 s) and `/audio/<uid>` serves a RIFF
> WAV stream.
>
> **Camera `.107` still fails, and the difference is on its side.** Its
> handshake completes and it reports the same states (`id3 = 3`, then
> `0` then `1` = direct live), but its frames then stop for ~30 s,
> arrive as a 14-frame burst, and the session dies. A healthy camera
> settles into one `0x0002` every 10 s and stays. Its own TCP ports
> are all dead, i.e. its RTSP/spook media engine is not running -- the
> §0 15:01 finding. A power cycle is what revives it.

> **⚑ Update 2026-10-24 16:14 — first frames through the broker, and the
> 36 s stop.** Observed live on camera .107 (`140979857781`). Details in
> §13.4, §13.5, §13.7 and §15.
>
> 1. **The §15 fixes worked.** The broker now puts `0x0020` in its STUN
>    answer. It runs the direct chain: `0x8101` with the viewer pair, then
>    `0x8004` value 1, then a hello punch.
>    * Every cycle now brings a `0x0003` report.
>    * The camera holds one sig socket (`:50665`, from 16:16:27 on).
>    * The bridge receives video and audio from the camera's RTP port at
>      7–8 fps. The first frames arrived at about 16:14:30.
>    * The relay (`0x8005`) was not needed.
> 2. **The camera stops by itself exactly 36 s after each start.** Its log
>    says `live heartbeat timeout, stop live` (`0x1801a628`).
>    * The timer is `dev->0x34`. Once PlayLiveview is in, only `0x8002`
>      refreshes it, because the P2P thread that also stamped it has
>      exited.
>    * The broker logs the bridge's `notify(keepAlive)` (every 2 s) and
>      sends nothing on. So `mediaState` goes 3 → 0 → 1 (playing) → 0
>      every ~50 s.
>    * The bridge sees the stream die after 10 s and starts it again: about
>      36 s on, 14 s off.
> 3. **The next fix:** send each `eventType=0` on to the camera as an
>    `0x8002`. The camera does not read the body (§15 item 6).
> 4. **Camera .108 (`141030191094`) proves nothing either way.** It has
>    been off the network since 16:14:26 (no sig, no ping), so its zero
>    frames say nothing about the path. At 16:12:52 it did report
>    `mediaState` 1.

> **⚑ Update 2026-10-24 evening — the blocker, and the direct path.** Static
> disassembly, checked against every capture we hold (details in §12–§15). It
> supersedes the rest of §0 and §7 where they disagree.
>
> 1. **The camera rejects every STUN answer the broker sends.** Its STUN
>    client (`CUSTOM_TASK_StunXorAddr`, `0x180190e8`) accepts a Binding
>    Success only if it carries a standard **XOR-MAPPED-ADDRESS, attribute
>    `0x0020`**. The broker answers with `0x0001`/`0x0004`/`0x8020`, the shape
>    the vendor gave the *phone's* RFC 3489 request, and no `0x0020`. So every
>    attempt fails. The camera retries 4 times, each from a fresh random port
>    in 50001–60000. Then it logs `stun function, device need reboot` and
>    **closes its own sig socket**. That is the "~5-6 s short transaction"
>    below: it is the STUN-failure path, not the camera's normal rhythm (§12).
> 2. **The wire agrees: the camera has never sent `0x0003`.** After a
>    successful STUN the camera reports its local and STUN-learned addresses
>    with `cmd 0x0003` (`CUSTOM_TASK_ReportLocalAddr`). Neither the 2 h vendor
>    wiretap nor the broker log holds a single `0x0003`. So the camera's STUN
>    failed throughout the wiretap too, and the wiretap's cadence is the
>    failure cadence. The wiretap forwarded only TCP 8882. The STUN host it
>    handed the camera, `stun.gps555.net` → `47.238.125.111`, is one of the
>    routed addresses, so **(inferred)** the broker answered it, not the
>    vendor.
> 3. **Direct live view is `0x8101` + `0x8004`, not `0x8005` (§13).**
>    * **`0x8101`** (`CUSTOM_TASK_P2PConnReq`) carries the viewer's public and
>      private ip:port. It re-runs STUN + `0x0003`, picks one of the two
>      pairs by the app's subnet rule, and starts the P2P thread. That thread
>      binds the RTP port and waits for the viewer's hello.
>    * **`0x8004`** (`CUSTOM_TASK_playCtrl`) carries a u16 at body offset
>      `0x14`: **1 = PlayLiveview**, **2 = PlayPause**, **3 = stop**, which
>      are the app's `CameraEventType` start/pause/stop. PlayLiveview acts
>      only once the P2P thread is up; otherwise it logs `p2p is not finish`.
>    * **`0x8002`** stamps the camera's liveness timer. It is the live-view
>      keepalive: without it, play stops at 36 s (§13.4, confirmed 16:16).
>    * **`0x8005`** stays what it was: the relay start.
> 4. **The 13:47 phone capture is the first wire record of `eventType=5`.**
>    It also shows the broker's STUN replies never reaching the phone (wrong
>    source address). It says nothing about the camera, because no camera was
>    connected to the broker's sig server while it ran (§14).
> 5. **Why the app never started there:** it waits for `stun-addr` to show
>    the `seqNo` of its own `send-stun-addr`, and the broker's shared counter
>    never does. The vendor-era captures settle this (§14.1). They also show
>    that `IpcPublic*` is the camera's pair, not the viewer's, and that
>    `mediaState` "3" is the camera's own "P2P socket up" report (§13.7).
>
> §15 lists what this implies. Its items 1, 3 (parse) and 5 have since gone
> into the broker; the 16:14 box above has the result.

The camera runs its own RTSP **server** and, when told to, **sends its RTP out
over UDP** to an address the signalling server gives it. Two server→camera
frames over sig:8882 make that happen, in two steps:

1. **`cmd 0x8000`** (the registration answer, which the broker already sends)
   *arms* the relay target: the camera copies a relay IP + port and a STUN host
   + port out of the frame body at fixed offsets, stores them (via
   `spook_rtsp_set_relay_info`), and sets its session state (`dev->0x2c`) to 3.
   It does **not** connect yet.
2. an unsolicited **`cmd 0x8005`** (empty body) *gives the start signal*: its
   handler sets a relay-start flag (`dev->0xc8 = 2`) and clears any stale
   session fd. A state machine then runs `rtsp_p2p_start`, which opens a
   **UDP RTP** socket to the armed relay endpoint and sends JPEG (RTP/AVP 26) +
   PCMU (RTP/AVP 0) there — the same RTP the camera serves on its own RTSP path
   `/webcam/jpeg_dvp`, pushed over UDP.

**Correction (validated 2026-10-24):** the camera→relay leg is **plain UDP RTP
(`sendto`), not an RTSP ANNOUNCE/publish** — the verbs `ANNOUNCE`/`RECORD` are
absent from the firmware; only the server-side `OPTIONS/DESCRIBE/SETUP/PLAY`
exist. So it behaves exactly like the direct path, just aimed at the relay
address. And `0x8005` does **not** "start a task" — `CUSTOM_TASK_RelayPro` is a
log tag, and `0x8005` only flips state flags that a separate state machine acts
on (§6).

`0x8005` is semantically the relay start (its handler logs "start relay"),
matching the app's `CameraEventType.relay` (eventType 5) — see §6 for why the
match is semantic, not a numbering rule. The broker's 462-candidate sweep never
sent a bare `0x8005` because it drew ids from the wrong tables (§8), which is why
nothing ever streamed.

> **⚠ Validation status (2026-10-24): the end-to-end chain is NOT yet proven on
> hardware.** The `0x8005` *handler* is confirmed by static disassembly (below),
> and the broker now sends `0x8005` on every keepalive tick
> (`ziot_fake_cloud.py::push_relay_trigger`, wired into `keepalive()`). But a
> live test against camera `.108` — with `0x8005` firing every ~2 s — produced
> **zero** media: no UDP RTP arrived at the relay address (`192.168.68.114:8554`)
> in a 20 s UDP-sink test, `rx_datagrams` stayed 0, and the camera sits in a
> **register/reconnect loop** — it keeps retransmitting `0x0001` and re-opening
> the sig socket (`:50367`→`:50368`, seq resetting to 1) instead of settling
> into a session. So "send `0x8005` → camera streams" is **not** validated.
> Treat §6/§9 as the current best hypothesis, not a working recipe, until a live
> capture shows the camera sending media. Next suspects: the camera is **not
> accepting the broker's `0x8000`** (hence the re-registration), the `0x8005`
> seq/timing may be disrupting the session, and the state-machine edge in §6 is
> untraced.
>
> **Correction (later 2026-10-24).** The per-tick wiring described above has
> itself been removed: firing the unsolicited `0x8005` on every keepalive tick
> turned out to be what reproduced the reconnect loop, so `push_relay_trigger`
> is now held behind `--relay-start` (**default off**) and fires at most once
> per armed connection. That rules out the seq/timing suspect above. The
> standing suspects are the camera not accepting the broker's `0x8000` and the
> untraced state-machine edge in §6 — the register answer's seq echo (§5, body
> offset 0) has also since been fixed in the broker.
>
> **Wiretap + manual-lab findings (2026-10-24 afternoon).** *Superseded in
> part (evening): the "short transaction" in this box is the camera's
> STUN-failure path, not its normal behaviour (§12).* The
> vendor's own sig stream (`ziot-wiretap.log`, 2 h capture, 3122 camera→vendor,
> 2871 vendor→camera frames) settles the session model: a sig session is a
> **short transaction, not a live channel** — connect, `0x0001` register ~1 s
> later (boot-shaped `seq=1` on fresh tasks, higher seq later: 6/16/28/31),
> the four answers below, then **the camera closes the TCP itself ~5-6 s after
> connect**, idles ~10 s, and repeats — **every ~9-18 s per camera, for hours**.
> The vendor **never spoke first** (no welcome arm; the register is the opener)
> and **never pushed anything unsolicited**: every vendor→camera frame is one
> of `0x8000/0x8021/0x8001/0x8601` — exactly the four answers. Empty
> connect→instant-close probes happen against the vendor too, so silent probes
> are normal camera behavior. Same-day manual lab: an unsolicited
> **`0x8000`+`0x8005` sent into a silent probe connection is completely
> ignored** (five attempts, byte-identical frames) — consistent with the §3
> state gate — and **5 minutes of continuous correct serving did not revive
> the register cadence** after a session was dropped mid-cycle. Every observed
> cadence revival followed a camera network-task restart (both cameras' stun
> seq counters reset at 13:00:40) which had followed both planes going
> unanswered for ~3 min. The morning "reconnect loop" cadence (registers ~15-23
> s apart, 12:08-12:42) matches the vendor-era rhythm and is not by itself a
> fault state.
>
> **Lab-builder checksum bug (found 14:36, 2026-10-24).** The wire XOR is an
> **XOR-reduce of every byte before the checksum** (`ziot_fake_cloud.py`
> `_xor`, verified bit-exact against vendor `0x8001/0x8021/0x8601` frames and
> camera frames in the broker log) — **not** sum-mod-256. A hand-rolled lab
> builder used the sum, so every lab answer sent on 24 Oct 12:1x-13:47 failed
> the camera's XOR check at `0x18017ea0`, and a sum-checking lab parser would
> have dropped valid camera frames. Consequence: the "silent-probe limbo"
> findings above are **partly invalid** — labs v3-v8 provably served invalid
> frames. Lab v9 (XOR-correct builder, `:8882` held 8 min): `.107` registered
> **43 times (~every 11 s), 29 completed cycles** (connect → register → the
> four answers → camera self-closes ~5-7 s), some sessions stayed hot ~20-140
> s with `0x0021` keepalives and re-register bursts (seq 1→3 within 1 s). The
> vendor-exact arm was alternated per cycle between the vendor's own port pair
> (`0x16`=554, `0x18`=10000) and the bridge's media port in both slots —
> one `0x8005` fired at 14:38:46 — **no UDP RTP arrived at :36562, :36640 or
> :10000**. *Correction (evening):* that `0x8005` did not go into a hot
> session. The lab's trigger loop picked the first armed, unfired connection
> for the uid, which was the stale `:50365` socket. It was opened 14:36:32,
> carried its last camera frame at 14:36:46, and was never read by the camera
> again while eight newer cycles ran on other sockets; it closed at 14:38:52.
> So v9 never put `0x8005` into a live session, and every v9 cycle was
> already being torn down by the STUN failure (§12) regardless. Media-start is
> still unreproduced; the remaining known-unknowns: what makes the camera run
> `rtsp_p2p_start` after `dev->0xc8=2` (§6 edge), and the app-side flow (a
> phone-app capture at 13:47 is being analysed).
>
> **App-plane capture decoded (PCAPdroid_24_out._13_47_51.pcap, 13:47-13:48,
> and the successful-era captures PCAPdroid_27_ago._21_00_04.txt/.har).** The
> app never speaks `86 86` and never touches `:8882`; its control plane is
> plaintext REST to `ipc.gps555.net:80` (= `120.26.121.165`, claimed) and its
> own STUN binding to `:13478` (phone ports 12345/57416/50000). The
> successful-era flow: the app punches a STUN socket and reports its mapping
> with `send-stun-addr?publicIp=..&publicPort=..&privateIp=..` (the mapped
> public port changes per call — those are its punch attempts); the cloud
> answers with the **camera's** tracked mapping (`IpcPublicIP/IpcPublicPort/
> IpcPrivateIP/IpcPrivatePort/seqNo` — the camera's stun counter, rising
> ~1/2 s); the app then polls `stun-addr/<uid>` every ~2 s. **`notify-live-event?
> eventType=1` is start-live, `eventType=0` sent every 5 s is the live-session
> heartbeat while watching** (Aug capture; `eventType=5` appears only in the
> 24 Oct capture, before any stream — semantics unconfirmed). While live the
> device row's `mediaState` reads "3" (the arm state reflected in the vendor
> DB) and "0" when idle; the row also carries `relay_ip: "156.246.16.114:554"`
> and `server_ip: "47.110.77.193"` (the claimed sig server). The app also
> sends `POST /api/v1/cmd/send-cmd` with body `{"cmdType":"20","uid":"..."}` —
> semantics unknown; the broker answers "ok". A separate app-side channel
> exists to `47.252.25.16:9377` (unclaimed, so it reached the real vendor):
> one ~950 B encrypted session every ~15 s with 9 B ACKs — the relay-pull side,
> with **no RTP ever flowing to the phone** in any capture.
>
> **Live-chain result (15:01, 24 Oct, first full local run).** With the broker
> on all planes (`--relay-start`, XOR-correct builder) and the user's phone
> app open through the redirect: both cameras registered and were armed at
> the bridge's live viewer socket (`192.168.68.114:55658` for `.107`,
> `:33223` for `.108` — the arm's port slots carry the registered viewer);
> the phone's `eventType=1` made the broker fire `0x8005` into `.107`'s live
> session (14:58:42, seq=7) — and **46 s of tcpdump showed zero UDP datagrams
> from either camera**. Both cameras refuse TCP on every media port (80, 554,
> 8080, 8899, ...): **the cameras' RTSP/spook media engine is not running** —
> which explains `rtsp_p2p_start`'s missing push (§6: it must look up the
> `/webcam/jpeg_dvp` media session) and matches the vendor's own view
> (`mediaState: "0"` in the 13:48 device row; the Aug successful era read
> "3"). The cameras' sig and stun planes are healthy. Next lever: a camera
> power-cycle to bring the media engine back, then re-run the chain.

---

## 1. Hardware and how to read the firmware

* **CPU: C-SKY CK803** (Taixin TXW817, 32-bit C-SKY ABIv2, little-endian).
  This is why every earlier disassembly attempt failed and the firmware was
  wrongly called "undecodable": capstone and Ghidra have **no** C-SKY support.
* Firmware image: Taixin **XIPH** container; the payload is loaded flat at
  **`0x18000000`**. The binaries live at **`~/ziot-fw/payload.bin`** (and
  `~/ziot-fw/TXW817_A_V1.0.12.32.bin`); the repo's `ziot-fw/` holds only the
  `.md` docs, not the binaries.
* Disassembler: the chip vendor's own GNU toolchain — the binary is
  `csky-elfabiv2-objdump` (equivalently `csky-elfabiv2/bin/objdump`), from
  `Taixin-Semiconductor/XuanTie-CSKY-Toolchains`;
  mirror asset `csky-elfabiv2-tools-x86_64-minilibc-*.tar.gz` in
  `cjacker/wm_sdk_w80x`). Reproduce the full listing with:

  ```bash
  csky-elfabiv2-objdump -D -b binary -m csky -EL \
      --adjust-vma=0x18000000 ziot-fw/payload.bin > fw.asm
  ```

  `lrw` (load-relative-word) literal loads resolve in the listing, so string
  references are recoverable (e.g. `lrw r0, 0x1805dd63` loads the pointer to
  `"rtsp://%s:%d/live/%s"`).
* C-SKY branch convention used throughout: `cmpnei rX, imm` sets the C flag to
  `(rX != imm)`; `bt` branches when C is set (true), `bf` branches when C is
  clear — so **`cmpnei rX, imm` immediately followed by `bf L` jumps to L when
  `rX == imm`.** This was validated against a known case (§4).
* The Taixin TXW81x SDK is available as C source (`divadiow/txw81xcam`); its
  `sdk/app/spook` is the RTSP/RTP/JPEG engine the firmware's `spook_rtsp_*`
  functions are built on. The gps555 cloud protocol below is **not** in that
  SDK — it is Z-IOT's own application layer and exists only in the stock
  binary.

---

## 2. The wire frame

Every frame, both directions, is:

```
86 86 | cmd(2) | len(2) | seq(2) | uidLen(2) | uid(uidLen) | body | xor(1) | 0x0D
```

* All multi-byte header fields are **big-endian**.
* `cmd` — 16-bit command id. Camera→server ids have the top bit clear
  (`0x0001`, `0x0002`, `0x0004`, `0x0021`, `0x1601`); server→camera ids have
  the top bit set (`0x8000`, `0x8001`, `0x8005`, `0x8021`, `0x8601`, …).
* `len` — the uid byte count plus the body byte count. It **excludes** the
  2-byte `uidLen` field itself, the `xor` and the `0x0D`. Example: the camera's
  `0x0001` register frame has `len = 0x2c = 44 = 12 (uid) + 32 (body)`; an
  empty-body frame has `len = 0x0c = 12`.
* `seq` — a sequence counter. The camera increments it on every
  retransmission.
* `uidLen` — always `0x000c = 12`; `uid` is the 12 ASCII decimal digits.
* `xor` — XOR of **every** byte before it (from the first `0x86`).
* `0x0D` — end marker; the firmware waits for it before acting on a frame
  (`ziot-fw/REPORT.md §14.1`).

The parser is `CUSTOM_PROTOCOL_ParseData` (code near `0x18018094`): it scans
for the `86 86` magic (`cmpnei r3, 134` on two consecutive bytes) and reads
`cmd` at body offset 2-3, `len` at 4-5, `seq` at 6-7, `uidLen` at 8-9.

---

## 3. Receive path and the state gate

`ParseData` (the variant at `0x18018094`, which carries the
`CUSTOM_PROTOCOL_ParseData` tag) scans a received buffer for `86 86` frames
and, per frame, invokes **`CmdProHandle`** (`0x1801b1fc`), whose pointer it
holds at RAM `0x20001ee8` (`lrw r11, 0x20001ee8` at `0x180180fc`). Both that
slot (`= CmdProHandle`) and a second callback slot `0x20001eec` (`= the
enqueue 0x18018740`) are installed by the registration call at `0x1801ac0c`
(`lrw r1, 0x1801b1fc; lrw r0, 0x18018740; bsr 0x18017eb4`). XOR is checked at
`0x18017ea0`.

The enqueue `0x18018740` gates on the session state `dev->0x2c`
(`dev` = the device/session struct at RAM `*0x20001ef4`):

```
18018774: ld.w  r3, (r0, 0x2c)   ; state
18018776: cmphsi r3, 2           ; state >= 2 ?
18018778: bf    error            ; state < 2  -> drop
1801877a: cmpnei r3, 5           ; state == 5 ?
1801877c: bt    enqueue          ; state in {2,3,4,>=6} -> enqueue -> CmdProHandle
1801877e: error                  ; state == 5 -> drop
```

So a received frame reaches `CmdProHandle` only when the connection state is
≥ 2 (and ≠ 5). The sig connection raises the state to "connected" before any
answer arrives, so the registration answer `0x8000` **does** reach
`CmdProHandle`. `CmdProHandle` logs `req cmd:%04X, seq:%d len:%d` and
`CUSTOM_TASK_CmdProHandle data:` for each frame it dispatches.

---

## 4. The server→camera command dispatch (`CmdProHandle`, `0x1801b1fc`)

`CmdProHandle` switches on the 16-bit `cmd` (held in r5) via a compiler
binary search. Known arms:

The complete set of command ids the dispatch matches (every `cmpnei r5, imm`
followed by a branch to a handler) is: `0x0091`, `0x0092`, `0x8000`,
`0x8001`, `0x8002`, `0x8004`, `0x8005`, `0x8021`, `0x8101`, `0x8201`,
`0x8202`, `0x8203`, `0x8206`, `0x8207`, `0x8301`, `0x8601`, `0x9003`. **There is
no `0x8003`.** *(Corrected 2026-10-24 evening: `0x8102` and `0x8302`, listed
here before, appear only as `cmphsi` range bounds of the binary search and are
not cases; `0x0091`/`0x0092` are cases.)* The ones relevant to media:

| cmd | handler | log tag | what it does |
|---|---|---|---|
| `0x8000` | `0x1801b55a` | `CUSTOM_TASK_AuthRes` | register answer: **arm relay** — parse relay+stun, set state 3, `spook_rtsp_set_relay_info` (§5) |
| `0x8001` | `0x1801b66c` | `CUSTOM_TASK_CommRes` | parse `cSeq:%d, cmd:%d, ack:%d` — the server's ack of a camera request |
| `0x8002` | `0x1801b6ca` | — | **heartbeat**: stamps the liveness tick `dev->0x34`; every 5th one makes the camera send its own `0x0002` (§13.4) |
| `0x8004` | `0x1801b700` | `CUSTOM_TASK_playCtrl` | **play control**: u16 at body `0x14` — 1 PlayLiveview, 2 PlayPause, 3 stop (§13.3) |
| `0x8005` | `0x1801b8f0` | `CUSTOM_TASK_RelayPro` | **relay start**: `peerToPeerFlag` (`dev->0xc8`) = 2, mode `dev->0xd0` = 2 (§6) |
| `0x8021` | `0x1801b392` | `CUSTOM_TASK_UtcTimeResPro` | server clock — **then runs `ReportLocalAddr`** (STUN, then `0x0003`) (§12) |
| `0x8101` | `0x1801ba22` | `CUSTOM_TASK_P2PConnReq` | **direct-path request**: the viewer's public + private ip:port; STUN; start the P2P thread (§13.1) |
| `0x0091 0x0092 0x8201 0x8202 0x8203 0x8206 0x8207 0x8301 0x8601 0x9003` | — | — | not traced; nothing read so far ties them to media |

**Convention validation.** The `0x8001` → `0x1801b66c` mapping is the proof
that the `cmpnei/bf` reading in §1 is correct: `0x8001` is the server's ack,
its handler logs `cSeq:%d, cmd:%d, ack:%d`, and the wiretap shows the vendor's
`0x8001` bodies as `00 03 00 02 00` (cSeq=3, cmd=2, ack=0 — acking the
camera's `0x0002` request, seq 3) and `00 04 00 04 00` (acking `0x0004`,
seq 4). The fields line up exactly, so the dispatch decoding is sound.

---

## 5. `cmd 0x8000` — arm the relay (handler `0x1801b55a`)

The handler reads the frame body (`r4` = body pointer) at these fixed
offsets and stores them into the device struct:

| body offset | size | field | destination |
|---|---|---|---|
| 0 | 2 | seq (echoed) | — |
| **2** | **20** | **relay IP (ASCII)** | `memcpy(dev+76, body+2, 20)` |
| **0x18 (24)** | **2** | **relay port (u16 BE)** | `dev->0x60` |
| 0x1a (26) | 32 | stun host (ASCII) | `memcpy(dev+166, body+0x1a, 32)` |
| 0x3a (58) | 2 | stun port (u16 BE) | `dev->0xc6` |

Disassembly evidence:
```
1801b56a ld.b r5,(r4,0) ; 1801b56c ld.b r6,(r4,0x18) ; ... addu r6,r2  -> relay port = u16@0x18
1801b586 addi r1,r4,2   ; movi r2,20 ; ... bsr memcpy  -> relay IP = body[2..21] -> dev+76
1801b59e lrw r0,0x18060661  ; "seq:%d, relay port:%d relay ip:%s"
1801b5be addi r1,r4,26  ; r2=32 (mov r2,r7) ; bsr memcpy -> stun host = body[26..57] -> dev+166
1801b5d4 lrw r0,0x180606a2  ; "seq:%d, stun port:%d stun ip:%s"
1801b5e6 movi r3,3 ; st.w r3,(r2,0x2c)                 -> state = 3
1801b606 bsr 0x180132d4                                -> spook_rtsp_set_relay_info
```

This body layout is exactly the registration answer captured from the vendor
(`ziot-fw/REPORT.md §2.6`): a u16 lead/seq, a 20-byte relay IP slot, two u16
relay ports at 0x16 and 0x18, a 32-byte stun host slot at 0x1a, a u16 stun
port at 0x3a, then sig IP/port. **Note the camera reads the relay port from
offset 0x18 — the vendor's *second* port slot (10000 in the capture), not the
554 at 0x16.** The broker writes the relay port into *both* slots
(`vendor_registration_answer(..., relay_port, relay_port)`), so the armed URL
is correct regardless.

### 5a. `spook_rtsp_set_relay_info` (`0x180132d4`) only arms, never connects

It builds `rtsp://<relay_ip>:<relay_port>/live/<name>` with `snprintf`
(`0x1800268c`, format at `0x1805dd63`) into a stack buffer and hands it to
`rtsp_relay_info_set` (`0x18012538`). It performs **no socket connection**.
This is why `0x8000` alone never makes the camera publish — confirmed by the
wiretap: even against the real vendor, a `0x8000` carrying a real relay
(`156.246.16.114`) produced no publish, because no start command followed.

---

## 6. `cmd 0x8005` — start the push (handler `0x1801b8f0`)

Reached for `cmd == 0x8005` (dispatch: `0x1801b36c cmpnei r5,0x8004 / bf`;
`0x1801b372 cmphsi r5,0x8005 / bt 0x1801b384`; `0x1801b384 cmpnei r5,0x8005 /
bf 0x1801b8f0`). The handler:

```
1801b8f6 lrw r1,0x18063074 ; lrw r0,0x180608ea ; bsr printf  ; logs "CUSTOM_TASK_RelayPro"+"start relay" (log tags only)
1801b8fe ld.w r3,(r4,0) ; movi r2,2 ; st.w r2,(r3,0xc8)       -> dev->0xc8 = 2  (relay-start flag)
1801b906 movi r0,1 ; bsr 0x18038938                          -> a timing/delay helper (NOT a task start)
1801b90e ld.w r0,(r3,0xcc) ; blz ... ; bsr 0x1800e868 ; st -1 -> close any stale session fd, dev->0xcc = -1
1801b762 (fall-through)                                      -> store 2 to byte dev->0xd0
```

**Validated correction (2026-10-24).** `0x8005` does **not** start a task:
`CUSTOM_TASK_RelayPro` (`0x18063074`) is a **log tag** (referenced once), and
`bsr 0x18038938` is a timing/delay helper, not a task create/resume. The handler
only flips state (`dev->0xc8 = 2`, `dev->0xd0 = 2`) and drops a stale fd. It
**reads nothing from the frame body** (`r4` is reloaded with the device-struct
pointer), so `0x8005` is a bodyless trigger — a well-formed frame with the right
uid, XOR and `0x0D` is enough.

The media send is driven *separately* by a state machine (around `0x1801a4e6` /
`0x1801a53a`) that reads `dev->0x2c` (= 3, set by `0x8000`) and the `dev->0xc8`
flag (= 2, set by `0x8005`), calls `spook_rtsp_set_relay_info` (`0x180132d4`)
and then **`rtsp_p2p_start`** (`0x1801281c`). `rtsp_p2p_start` builds
`rtsp://%s:%d/webcam/jpeg_dvp/track0`/`track1`, looks up the `/webcam/jpeg_dvp`
media session, and calls **`connect_client_udp_endpoint`** (`0x1801193c`), which
opens a **UDP RTP socket** and `sendto`s the media to the relay IP:port. There
is **no RTSP ANNOUNCE/publish** — `ANNOUNCE`/`RECORD` are absent from the
firmware (grep: 0 hits); only the server-side `OPTIONS/DESCRIBE/SETUP/PLAY`
exist. So the camera delivers JPEG (RTP/AVP 26) + PCMU (RTP/AVP 0) as **UDP
RTP**, the same as the direct path, just aimed at the relay address.

> **Not fully traced:** the exact edge from the `0x8005` flag to that state
> machine actually firing (the thread that polls `dev->0xc8`). It is consistent
> and plausible but unproven statically; the live test (§9) is what proves
> `0x8005` → UDP RTP.

**(inferred)** The *behavioural* correspondence is solid: this handler is the
relay-start signal, and the app's relay request is
`GET /v1/ipc/notify-live-event?eventType=5` (`CameraEventType.relay`, eventType
5; `DECOMPILATION_REPORT.md §2.2`). So the broker should send `0x8005` where
the vendor cloud would react to that HTTP call. **Do not** read the matching
"5" as a rule, though: the server→camera cmd space is *not* `0x8000 + eventType`
— there is no `0x8003` (which "stop", eventType 3, would need) and `0x8000`
itself is the register answer, not an event. The link is semantic (relay task),
and the exact `eventType → cmd` translation the cloud performs was never
captured on the wire (no live-view ran during any capture). *Update (evening):*
the firmware's other handlers give that translation for the remaining event
types as well. start/pause/stop are **one** command, `0x8004`, with the event
number as a u16 in its body, which is why there is no `0x8003`. See §13.6.

---

## 7. The complete media-start sequence

```
camera  --0x0001 register-------------------->  server
camera  <-0x8000 (relay+stun addresses)-------  server   [arms: URL built, state=3]
camera  --0x0021/0x0002/0x0004/0x1601-------->  server   [normal handshake]
camera  <-0x8021/0x8001/0x8001/0x8601---------  server
...
                                (a viewer wants the stream)
camera  <-0x8005 (empty body)-----------------  server   [relay-start flag dev->0xc8=2]
   (state machine reads dev->0x2c=3 + dev->0xc8=2 -> rtsp_p2p_start -> connect_client_udp_endpoint)
camera  ==UDP RTP JPEG(26)+PCMU(0) via sendto=> relay_ip:relay_port  (receiver -> bridge)
```

That is the **relay** path. The **direct** path, the one that streamed
against the vendor on 2026-08-29, is different (§13). It needs the camera's
STUN to succeed first (§12):

```
camera  --0x0001 register-------------------->  server
camera  <-0x8000 (relay+stun addresses)-------  server   [arm; STUN host = body 0x1a]
camera  --0x0021 clock------------------------>  server
camera  <-0x8021------------------------------  server   -> ReportLocalAddr:
camera  ==STUN Binding Request (from RTP port)=> stun host   (up to 4 tries, new port each)
camera  <=Binding Success WITH attr 0x0020=====  stun host   (0x8020 alone fails, §12)
camera  --0x0003 local+public ip:port--------->  server   [what stun-addr/<uid> serves]
...
                          (viewer: send-stun-addr, inferred §13.6)
camera  <-0x8101 viewer public+private ip:port-  server   -> STUN + 0x0003 again,
                                                            choose pair, start P2P thread
   (P2P thread binds the RTP port, peerToPeerFlag=1, recvfrom: viewer's "App send hello")
                          (viewer: notify start, inferred)
camera  <-0x8004 playCtrl=1-------------------  server   [PlayLiveview: flag 1->2, mode 0xd0=1]
camera  ==UDP RTP JPEG(26)+PCMU(0)============> chosen viewer ip:port
camera  <-0x8002 heartbeat, repeated----------  server   (viewer: notify keepAlive, inferred)
```

*Corrected 2026-10-24 evening.* This paragraph used to say that, read
against the vendor's 2 h wiretap, the sig leg runs as a **short
transaction**: connect, register, the four answers, then the camera closes
the TCP itself ~5-6 s after connect and re-runs the cycle every ~9-18 s. The
observation stands; the reading does not. The close is the end of
`ReportLocalAddr`'s failure branch, which closes the sig socket (`dev->0x28`)
after its 4th failed STUN. The wiretap holds no `0x0003`, so the STUN failed
throughout it. **(inferred)** That is because the routed STUN host was
answered by the broker, which gives no `0x0020` (§12). How the camera behaves
after a
*successful* STUN has not been observed yet. **(inferred)** It sends
`0x0003` and keeps the connection, which is the "held channel" the vendor's
`notify-live-event` frames would need.

The camera learns its own public/media address from STUN on **its RTP
port**: the port the P2P thread later binds, and the one `0x0003` reports as
the local port. So the STUN source port is the media port (§12.1), which is
why punching the `stun-addr` private port is the right target. The STUN host
is the one armed by `0x8000` (body `0x1a`): `stun.gps555.net:13478` in the
vendor's answer and in the broker's.

---

## 8. Why the broker's 462-candidate sweep missed it

`ziot_fake_cloud.py`'s `_PUSH_CMD_IDS` are `0x0002, 0x0020, 0x0014,
0x0022…0x004E`, each also tried with the `0x8000` bit set (`0x8002, 0x8020, …`).
Those ids came from two wrong places, **neither** of which is the server→camera
sig space (`0x80xx`, §4):

* `0x02` and `0x14` — low ids from the app's command enums (`CmdAPType`
  `pullVideo 02`/`pushVideo 20=0x14`, or `CameraCMDType`). Those are the
  *camera's local AP/BLE / HTTP control* commands, a different protocol that
  only shares the `86 86` frame shape.
* `0x20–0x4F` — from `ziot-fw/REPORT.md §2.7`, which read the table at payload
  `0x80` as a 40-entry command table and derived ids `0x20-0x31, 0x36-0x49,
  0x4E-0x4F`. **That table is the interrupt vector table**, not a command table
  (each entry is an ISR: `nie; ipush; push r15; bsr <enter>; <load callback>;
  jsr; bsr <exit>; ipop; nir`). The "command ids" were an artefact of
  disassembling ISRs on the wrong ISA; §2.7 is superseded.

So the media-start command `0x8005` was never in the candidate set and cannot
be produced by OR-ing `0x8000` onto any base id in it. The elaborate bodies the
sweep attached were also irrelevant — the real trigger carries **no body**.
Right idea (an unsolicited server→camera push), wrong id space, built on a
misread table.

---

## 9. Broker implications (partially implemented — not yet working)

> **Superseded by §15 (2026-10-24 evening).** The list below assumes the
> relay path is the one to drive and that the camera is ready for it. §12
> shows the camera tears its session down before any of this can matter,
> because its STUN never succeeds. §13 shows the direct path (`0x8101` +
> `0x8004`) is the one the vendor's live view used. Kept for the record.

**Implementation status (2026-10-24, after the regression fix):** the `0x8005`
frame is built (`push_relay_trigger()`) but **held behind `--relay-start`
(default off)** — the per-tick `keepalive()` wiring described in older drafts
was removed after the live test showed it breaking the handshake (validation
box in §0), and the stale `PunchPushTests` expectations are fixed. Step 1
below is implemented in its corrected form: `arm_target()` arms a **UDP RTP
sink** — the bridge's own media socket, or a viewer's public pair — instead
of `ziot_relay.py`, the wrong receiver (§6). What is still **not** done is
the live proof: a capture showing a camera settle into one sig session and
push to the armed address.

To make a camera stream to us, the broker needs, per camera, once the sig
handshake has completed:

1. Answer the `0x0001` registration `0x8000` with the camera's `seq` echoed in
   the body's first two bytes (§5) and a real UDP RTP sink armed: the relay
   slots carry the bridge's own media address (`arm_target()`, both port
   slots; the camera reads offset `0x18`), falling back to a viewer's public
   pair and only then to the configured `--relay-port`.
2. Send an **unsolicited `0x8005`** frame on the camera's sig connection:

   ```
   86 86 | 80 05 | 00 0c | <seq> | 00 0c | <uid ASCII 12B> | <xor> | 0D
   ```

   `len = 0x000c` (uid only, empty body); `seq` any value; `xor` over all
   preceding bytes. The camera must be in session state 2/3/4 (it is, after
   registration) for the frame to pass the receive gate (§3).
3. **Receive UDP RTP at `relay_ip:relay_port`.** The camera does *not* RTSP-
   publish; after the start signal it `sendto`s JPEG (RTP/AVP 26) + PCMU
   (RTP/AVP 0) over UDP to that address (§6). So the listener there must be a
   **UDP RTP sink**, not an RTSP server waiting for `ANNOUNCE`. The bridge's
   existing direct-path receiver already is one — so the simplest validated
   design is to put the **bridge's UDP media address** into the `0x8000` relay
   slot and skip the RTSP relay for ingest. `ziot_relay.py`, which was built to
   accept an RTSP `ANNOUNCE`/`RECORD`, does **not** match how the camera
   delivers this leg.
4. Validate by watching for **UDP datagrams at `relay_ip:relay_port`**
   (`tcpdump -i wlan0 'udp and host <cam_ip> and port <relay_port>'`) and the
   bridge's `rx_datagrams` climbing — not an RTSP `ANNOUNCE`.

**(open / to verify live)**
* Whether `0x8005` must be re-sent periodically or sent once, and whether a
  specific `seq` or a preceding `0x8004` is needed. The `0x8005` handler ignores
  the body, but the state machine that turns its flag (`dev->0xc8 = 2`) into the
  actual `rtsp_p2p_start` + UDP `sendto` is not fully traced (§6).
* Which UDP port the camera sends *from*, and whether it expects the sink at the
  exact `relay_port` it was armed with (offset `0x18`) — confirm on the wire.
* The direct path (`eventType=1`) uses the same `rtsp_p2p_start` with a
  different mode flag and the STUN/viewer address instead of the relay slot; if
  the direct path is preferred, its own trigger cmd (not `0x8005`) still needs
  identifying. *Identified (evening): `0x8101` delivers the viewer address and
  `0x8004` with value 1 starts the play; mode byte `dev->0xd0` = 1 (§13).*

---

## 10. Evidence index (claim → firmware address)

| claim | address / artifact |
|---|---|
| CPU is C-SKY CK803 | Taixin product page + teardowns; clean CK803 reset at `0x18000a68` |
| frame parser, `86 86` scan, header offsets | `CUSTOM_PROTOCOL_ParseData` ~`0x18018094` (`cmpnei r3,134`) |
| receive gate on state `dev->0x2c` | enqueue `0x18018740` (`cmphsi r3,2` / `cmpnei r3,5`) |
| dispatcher | `CmdProHandle 0x1801b1fc`; registered at `0x1801ac0c` |
| `0x8001`→ack handler (convention check) | `0x1801b66c`, string `0x180606f9` |
| `0x8000`→arm handler, body offsets | `0x1801b55a`; relay `lrw 0x18060661`, stun `lrw 0x180606a2` |
| arm sets state 3 | `0x1801b5e6 st.w r3,(r2,0x2c)` |
| `spook_rtsp_set_relay_info` builds URL, no connect | `0x180132d4`; URL fmt `0x1805dd63`; `rtsp_relay_info_set 0x18012538` |
| `0x8005`→start handler, body ignored | `0x1801b8f0`; `CUSTOM_TASK_RelayPro 0x18063074`; `rtsp_p2p_start 0x1805d251` |
| start push log | `start push, session id:%s` `0x1805d6fc`/`0x1805d709` |
| sweep used wrong id table | `ziot_fake_cloud.py` `_PUSH_CMD_IDS` (ids `0x20–0x4F`) |
| `0x8000` body matches vendor capture | `ziot-fw/REPORT.md §2.6` |
| vendor `0x8001` ack bodies | `~/ziot-wiretap.log` (`00 03 00 02 00`, `00 04 00 04 00`) |
| camera STUN client accepts only attr `0x0020` | `CUSTOM_TASK_StunXorAddr 0x180190e8`; type test `0x180192c2 cmpnei r8,32`; XOR decode `0x180192dc movi r3,8466` (`0x2112`) |
| STUN reply source not checked | `0x1801926a` `recvfrom(fd, buf, 200, 0, NULL, NULL)` |
| 4 STUN tries, new port each, then close sig | `CUSTOM_TASK_ReportLocalAddr 0x1801b054`; `movi r6,4`; port `0x1801b13a` (`rand(9999)+50001`); give-up `0x1801b15e`, closes `dev->0x28` at `0x1801b17e` |
| STUN success sends `0x0003` | `0x1801b1d2 movi r0,3 ; bsr 0x18017ec8` (generic sender, `r0` = cmd) |
| `ReportLocalAddr` callers | `0x8021` handler `0x1801b428`; `0x8101` handler `0x1801bab4` |
| `0x8101` = P2PConnReq, body layout | `0x1801ba22`; `public ip:%s port:%d` `0x18060abb`; target to `dev+0xd1` (`0x1801bb84`, `0x1801bbac`) |
| `0x8004` = playCtrl, value at body `0x14` | `0x1801b700`; `0x1801b70c ld.b r3,(r4,0x14)`; branches `0x1801b722`/`26`/`2a` |
| PlayLiveview needs `peerToPeerFlag == 1` | `0x1801b732 ld.w r2,(r3,0xc8) ; cmpnei r2,1`; else `p2p is not finish` `0x18060773` |
| P2P thread binds RTP port, sets flag 1 | `custom_peer_to_peer_thread 0x18019638`; `0x18019712 st.w r6,(r3,0xc8)` (r6 = 1) |
| `0x8002` stamps liveness tick | `0x1801b6d0 bsr 0x18038998 ; st.w r0,(r5,0x34)` |
| zero `0x0003` frames on the wire | `~/ziot-wiretap.log` (760 `0x0001`, 576 `0x0002`, 576 `0x0004`, 675 `0x0021`, 535 `0x1601`); `ziot-broker.err` |
| phone capture of `eventType=5` | `PCAPdroid_24_out._13_47_51.pcap`, 13:48:15 |
| `seqNo` per registration; app waits for its own | `~/Downloads/Telegram Desktop/PCAPdroid_27_ago._21_00_10.har` (186/187/188); `PCAPdroid_29_ago._15_13_27.pcap` (114/115) |
| `IpcPublic*` is the camera's, not the viewer's | same two captures: camera public ports 65234/64709 and 65003/64695 vs phone 64642/65018/65449 and 64550/64991 |
| app punches the RTP port before `eventType=1` | 29 Aug pcap: hellos to `192.168.18.75:53693` at 15:13:44, `eventType=1` at 15:13:48.8 |
| `mediaState` = camera `0x0004`, argument 2 = wire id 3 (`dev->0x2f4`) | reporter `0x180193a0` (sends cmd 4 at `0x18019546`; id 3 stored at `0x1801946e`); writers `0x18019716` (3), `0x1801983c` (0), `0x180198aa` (4), `0x180195a0` (3), `0x1801a4dc` (1), `0x1801a530` (2) |
| first frames through the broker | `~/ziot-bridge.log` 16:14:48 (`7.4 fps, 136 frames total`); broker `fg-broker.log` 16:14:22–24 (`0x8101`, `0x8004`, hello) |
| P2P loop runs only while the flag is 1 | `0x18019730 ld.w r5,(r3,0xc8) ; cmpnei r5,1`; per-datagram stamp `0x18019782` |
| live heartbeat timeout = 36 s in mode 3 | `0x1801a628 cmpnei r7,3`; `0x1801a634 cmplti r2,36`; string `0x18061748`; `StopPlay` call `0x1801a650` |
| play start sets mode 3 and stamps the tick | `0x1801a498`/`0x1801a49a` and `0x1801a4fc`/`0x1801a4fe` (mode 3); `0x1801a4a8`, `0x1801a50c` (`dev->0x34`) |
| state machine gated on the sig link | `0x1801a0b4` (`0x18017860` flag), `0x1801a0c0` (`dev->0x2c ≥ 2`); live: 2 min of media with no broker, 16:14:32–16:16:27 |
| 61 s sig watchdog | `0x1801a54c`–`0x1801a556` (`cmplti r6,61`); strings `0x18061700`, `0x180617a8`; stamp `0x1801aa24` |
| 36 s stop on the wire | `/tmp/opencode/broker.err3`: `0x0004` value 1 at 16:16:41/16:17:31/16:18:21/16:19:12/16:20:04/16:20:55/16:21:44, value 0 at 16:17:17/16:18:07/16:18:57/16:19:48/16:20:40/16:21:30/16:22:20 |
| no `0x8002` from the vendor without a viewer | `~/ziot-wiretap.log`: 631 `0x8000`, 1134 `0x8001`, 606 `0x8021`, 500 `0x8601`, zero `0x8002` |

---

## 11. Cross-check against the Android app (2026-10-24)

The app (`com.flu.flutter_wifi_camera` 1.13.0) is an independent view of the
same protocol from the *client* side. Comparing the app data
(`DECOMPILATION_REPORT.md` + `libapp.so` strings) against the firmware findings
above:

### Corroborations (app agrees with firmware)

* **Frame shape** — the app's local frames (`CustomBleProtocolPacket`) are
  `86 86 | LEN | SEQ | DLEN | payload | XOR | 0x0D`, the same family the
  firmware parses (§2).
* **Camera→relay is UDP RTP; app→relay is an RTSP pull** — the camera does
  **not** RTSP-publish (§6): it `sendto`s UDP RTP to `relay_ip:relay_port`. The
  app, separately, *pulls* via RTSP `rtsp://<relay_ip>/live/<uid>`
  (`CameraInfoModel::relayPath`, `_supportRelayPath()==false` for our newer X5,
  `DECOMPILATION_REPORT.md §1.2`). So in the vendor topology the relay is a
  gateway: UDP RTP in from the camera, RTSP out to the app — it is not a path the
  camera and app share directly. The `/live/<name>` URL the firmware builds in
  `spook_rtsp_set_relay_info` is a session-name/metadata string (the `<name>` is
  a 20-byte field copied from the protocol context `*0x20001ee0 + 88`, likely
  the uid), not a connection the camera opens.
* **Media encoding** — firmware SDP `m=video RTP/AVP 26` (JPEG) + `m=audio
  RTP/AVP 0` (PCMU); the bridge already decodes exactly that.
* **Mobile PCAP (`PCAPdroid_29_ago…`, phone-side) — consistent, no conflict.**
  It is a **direct-path** live-view: HTTP `send-stun-addr` (publicIp
  `177.221.120.116`) → `notify-live-event?eventType=1` (start) + `eventType=0`
  keepalives → `stun-addr/<uid>`; **no `eventType=5`**, **zero RTSP lines**,
  **zero RTP** to the phone. So it never exercised the relay/`0x8005` path and
  carries no camera→relay traffic (phone-side only) — it neither confirms nor
  contradicts the `0x8005` finding, and its absence of any RTSP `ANNOUNCE`
  matches §6. It also shows the vendor flow delivering no media here (phone
  behind CGNAT), consistent with `DECOMPILATION_REPORT.md §2.10`. The only
  `eventType`s ever seen on the wire, across every capture, are 0 and 1 — never
  5 — which is why the `eventType→0x8005` link stays an inference (§6).
* **Relay is the deliberate mode** — the app has a `CameraConnectState` with
  both `directConnect` and `relay` modes (`DECOMPILATION_REPORT.md §1.4`), and
  its relay flow is driven by `CameraEventType.relay`
  (`requestSendRelay`/`setupRelayVideo`). The firmware's `0x8005` handler is the
  relay-mode start. (A separate direct-connect/P2P start command likely exists —
  the app has `directConnectVideoRtpPush` — but our architecture uses the relay,
  so `0x8005` is the one we need.)

### Inconsistencies found (and resolved here)

1. **`REPORT.md §2.7` is wrong: the table at payload `0x80` is the interrupt
   vector table, not a command table.** §2.7 (written with capstone on the
   wrong ISA) read it as 40 command handlers and derived ids `0x20–0x4F`. The
   correct C-SKY disassembly shows each entry is an ISR
   (`nie/ipush/…/ipop/nir`). The real command ids are the sig `0x80xx` set (§4)
   and the local `CmdAPType` set — never `0x20–0x4F`. §2.7 is superseded; a
   correction note is added there.
2. **Two command-id spaces were conflated.** The `86 86` frame is used by *two
   different protocols*: the **local** AP/BLE channel (`CmdAPType`, ids
   `0x01–0x14`, over TCP 16532 / BLE / the camera's own AP) and the **cloud
   signalling** channel (`0x80xx`, over TCP 8882). They share only the framing.
   The broker's push sweep drew from the local ids (and the misread `0x20–0x4F`)
   and applied them to the sig channel — see §8. On these station-mode units the
   local 16532 port is closed, so the local `CmdAPType` path is unreachable
   anyway; the cloud `0x80xx` path is the only one in play. The one `CmdAPType`
   command this repo does speak is `cmd 1` = `wifiConnect` over BLE:
   `00 01 | <len><ssid> | <len><pwd> | <len><urlAndPort> | <tz:2> | [regCode:4]`
   inside the `86 86` frame. The camera's firmware prints nine field names
   because its parser serves several ids at once — `p1`, `port` and the two
   `bind id`s belong to the setup/pull command, not to `wifiConnect`
   (`DECOMPILATION_REPORT.md` §14.0).
3. **The cloud's `eventType → cmd` translation is not numeric.** The app sends
   HTTP `notify-live-event?eventType=0..5`; the firmware's sig cmd space has no
   `0x8003` and uses `0x8000` for the register answer, so it is *not*
   `0x8000 + eventType`. `0x8005 == relay` is a semantic match (the handler is
   the relay-start signal), not a numbering rule (§6).
4. **Relay port-slot asymmetry + the relay-receiver mismatch.** The vendor's
   `0x8000` carries two relay ports: `554` at body offset `0x16` and `10000` at
   `0x18`; the camera reads the `0x18` port as its send target. In the vendor
   topology the camera `sendto`s **UDP RTP** to `relay_ip:<0x18 port>` and the
   app/bridge *pull* RTSP from the `relay_ip` service port — the relay bridges
   the two. **Our `ziot_relay.py` is an RTSP server (TCP) and is the wrong
   receiver for the camera's UDP RTP** (§6, §9): it was built to accept an RTSP
   `ANNOUNCE`/`RECORD` the camera never sends. The broker does write `8554` into
   both `0x8000` port slots and the device-list `relay_ip`, so the *address* the
   camera is armed with is right; what's missing is a **UDP RTP sink** there (the
   bridge's direct receiver), not an RTSP server.

### What the app cannot confirm

The push (`0x8005`) is sent by the *cloud*, not the app, so the app's Dart code
contains no `0x8005` literal and cannot corroborate it directly. The firmware is
authoritative for the server→camera direction; the app corroborates the frame
format, the media path, and the relay semantics around it.

---

## 12. The camera's STUN client — why every session ends at ~6 s (2026-10-24 evening)

Read from the firmware (listing reproduced as in §1), then checked against the
broker log and the vendor wiretap. Function names are the firmware's own log
tags.

### 12.1 `CUSTOM_TASK_ReportLocalAddr` (`0x1801b054`)

It has exactly two callers: the `0x8021` clock handler (`0x1801b428`), which
runs on **every** sig cycle, and the `0x8101` P2P request handler
(`0x1801bab4`, §13.1). What it does:

1. Logs `local port:%d ip:%s` and `rtp port : %d`. The port is the
   **RTP port** `dev->0xa4`. The same port is bound by the P2P thread later
   (§13.2) and reported to the server (step 4).
2. Calls the STUN client (§12.2) with the STUN host and port armed by
   `0x8000` (`dev+166`, `dev->0xc6`), from that RTP port.
3. **On failure** it logs `call stun server fail, retry!!!` and draws a new
   RTP port, `rand() % 9999 + 50001` (`0x1801b024(9999)` followed by the
   `movi 15534; nor; addu` idiom, i.e. `+ 50001 mod 2^16`). It retries, **4
   attempts in all** (`movi r6, 4`). After the 4th failure it logs
   `error: call stun server fail!!!` and
   `stun function, device need reboot!!!!!`. It then **closes the sig socket**
   (`dev->0x28`), calls `0x18019f50` and returns -1.
4. **On success** it logs `stun return ip_port:%s` and sends **`cmd 0x0003`**
   through the generic frame sender `0x18017ec8`. Its call sites show the
   first argument is the cmd: `0x0001`, `0x0002`, `0x0004`, `0x0021` and
   `0x1601` all go through it, and so do `0x0005`, `0x0013`, `0x0022`,
   `0x0023`, `0x1201`–`0x1206` and `0x1301`. The 47-byte body, offsets from
   the start of the body:

   | offset | size | field |
   |---|---|---|
   | 0 | 20 | public IP, ASCII, NUL-padded (STUN result, `dev+98`) |
   | 20 | 2 | public port, u16 BE (`dev->0x82`) |
   | 22 | 20 | local IP, ASCII |
   | 42 | 2 | local port = the RTP port, u16 BE (`dev->0xa4`) |
   | 44 | 2 | caller-supplied u16 |
   | 46 | 1 | caller-supplied u8 (0 from the `0x8101` path) |

   **(inferred)** This is what the vendor's `stun-addr/<uid>` serves:
   `IpcPrivateIP:Port` = the local pair, `IpcPublicIP:Port` = the STUN pair.

### 12.2 `CUSTOM_TASK_StunXorAddr` (`0x180190e8`)

```
socket(AF_INET, SOCK_DGRAM) -> dev->0x48
setsockopt(fd, 0xfff, 0x1006 /*lwIP SO_RCVTIMEO*/, {1 s}, 12)
server = gethostbyname(<stun host from 0x8000>), htons(<stun port>)
bind(fd, {INADDR_ANY, htons(<rtp port>)})          "socket opened to %s:%d at local port %d"
sendto(fd, <Binding Request>, ..., server)          "Send data ..."
recvfrom(fd, buf, 200, 0, NULL, NULL)              "Read recv ..."   <- source NOT checked
if (u16 buf[0] != 0x0101) fail                     "STUN binding resp: success" otherwise
for (off = 20; off < 200; off += 4 + len):         walks attributes by their length
    if (type == 0x0020):                           0x180192c2 cmpnei r8, 32
        port = ntohs(v[2..3]) ^ 0x2112             0x180192dc movi r3, 8466
        ip   = v[4..7] ^ 21 12 A4 42               xori 33/18/164/66
        dev+98 = "%d.%d.%d.%d"; dev->0x82 = port; return 0
fail: close(fd); return -1
```

So the camera wants a **standard RFC 5389 XOR-MAPPED-ADDRESS, type
`0x0020`**. It skips every other attribute and never looks at
MAPPED-ADDRESS. Its request is RFC 5389-shaped (magic cookie `2112a442`)
and carries a **fixed transaction id** `63c7117e0714278f5ded3221`, the same
on both units, so a repeated txid is not evidence of retransmission. It
does not check where the answer came from.

### 12.3 What the broker answers, and why it fails

`stun_binding_success()` answers every Binding Request with `0x0001`
MAPPED-ADDRESS, `0x0004` and `0x8020`. That is the vendor's answer to the
**phone's** request in the 2026-08-29 capture (`DECOMPILATION_REPORT.md`
§11). The phone's request is RFC 3489-shaped, with no magic cookie
(confirmed again in §14), and `0x8020` is the pre-RFC 5389 draft code for
XOR-MAPPED-ADDRESS that old servers send old clients. There is no `0x0020`,
so the camera's parse loop never matches: **every camera STUN through the
broker fails.** **(inferred)** For the camera's RFC 5389 request the vendor
answers with `0x0020`. The camera streamed against the vendor on
2026-08-29, which by §12.1/§13 requires its STUN to have succeeded.

### 12.4 The wire agrees

* **No `0x0003`, ever.** The vendor wiretap holds 760 `0x0001`, 576
  `0x0002`, 576 `0x0004`, 675 `0x0021` and 535 `0x1601` from the cameras,
  and not one `0x0003`. Neither does the broker log (12:18–15:00,
  2026-10-24).
* **STUN arrives in bursts of up to 4, each from a new port.** Broker log,
  12:18–15:00. `.107`: 233 requests in 62 bursts (50 of 4, 9 of 3, 3 of 2).
  `.108`: 274 requests in 72 bursts (60 of 4, 10 of 3, 2 of 2). The median
  burst spacing is 16 s, which is one sig cycle. Not once did two requests
  in a burst share a port. Every port seen is between 50035 and 59924. That
  is §12.1's retry loop, drawing `rand() % 9999 + 50001`, run to
  exhaustion. The shorter bursts are most likely cycles cut short.
* **The sig session closes ~5-6 s after connect:** one register round trip,
  then up to 4 STUN tries with ~1 s of timeout each, then
  `ReportLocalAddr` closes `dev->0x28`. This is the "short transaction" of
  §0/§7, in every lab and in the vendor wiretap.

### 12.5 The reply's source address — irrelevant to the camera, fatal to the phone

The broker's STUN socket is bound to `0.0.0.0:13478` and answers with a plain
`sendto`. The kernel then picks the reply's source address from the route,
not from the address the request was sent to. On this host
`ip route get 192.168.68.106` gives `src 192.168.68.111` (wlan0's DHCP
address). That is neither `.114` nor the claimed vendor address
(`47.238.125.111` or `8.130.23.234`). The camera does not care (`recvfrom`
with a NULL address, §12.2). The phone's STUN replies, though, never reached
it (§14). Note also that `wlan1` (`192.168.68.119`) and `wlan0`
(`192.168.68.111`) are both on `192.168.68.0/24`, so the outgoing interface
for LAN replies is the routing table's choice too.

---

## 13. The direct (P2P) live-view path — `0x8101` + `0x8004` (2026-10-24 evening)

All offsets are from the start of the frame **body**, the first byte after the
uid, as in §5.

### 13.1 `cmd 0x8101` — `CUSTOM_TASK_P2PConnReq` (handler `0x1801ba22`)

1. Waits for any file upload to finish (`dev->0x24c`). If a play is
   running (`peerToPeerFlag` ≠ 0, or `dev->0x40` set), stops it with
   `StopPlay(0)` (`0x18019804`).
2. Runs **`ReportLocalAddr`** (§12.1): STUN, then `0x0003`.
3. Parses two address pairs and logs each as `public ip:%s port:%d`:

   | offset | size | field |
   |---|---|---|
   | 0 | 20 | **not read** by this handler |
   | 20 | 20 | IP A, ASCII, NUL-padded |
   | 40 | 2 | port A, u16 BE |
   | 42 | 20 | IP B, ASCII, NUL-padded |
   | 62 | 2 | port B, u16 BE |

4. Chooses the target. If IP A equals the camera's own STUN-learned public IP
   (`dev+98`), **and** the camera's local IP and IP B agree up to the last
   `.` (the same /24), it uses **pair B**. Otherwise it uses **pair A**. That
   is the app's `DeviceStunItem.ipAddress` subnet rule
   (`DECOMPILATION_REPORT.md` §4.6), applied from the camera's side. So A is
   the viewer's public pair and B its private pair, the same four values
   `send-stun-addr` carries.
5. Stores the target IP at `dev+0xd1` (20 B) and the port at
   `dev+0xe5`/`0xe6`. Calls `0x180127a8(ip, …, port)` (**inferred**: sets
   the RTP destination) and logs `ip:%s port:%d`.
6. Starts the **P2P thread** (§13.2).

### 13.2 The P2P thread — `custom_peer_to_peer_thread` (`0x18019638`)

`socket(AF_INET, SOCK_DGRAM)` into `dev->0xcc`, then `bind` to the **RTP port**
`dev->0xa4` (`rtp port = %d`; `bind fail` → exit). On success it sets
**`peerToPeerFlag` = `dev->0xc8` = 1**, stamps `dev->0x34`, and loops on
`start receive`. Each loop is a `recvfrom` of up to 128 bytes, and every
datagram re-stamps `dev->0x34` and logs the sender's port and the payload.
The loop exits when the flag leaves 1 (`peerToPeerFlag = 2` /
`peerToPeerFlag = 0`), closing the socket (`0x18019730`–`0x18019782`).
So viewer datagrams keep `dev->0x34` fresh only while the camera waits
for PlayLiveview. Once PlayLiveview sets the flag to 2, nothing the
viewer sends over UDP refreshes it (§13.4). **This socket is where the
viewer's `App send hello` lands:** at the camera's RTP port, which is also
the STUN port and the `0x0003` local port. That is why punching the
`stun-addr` private port is right.

### 13.3 `cmd 0x8004` — `CUSTOM_TASK_playCtrl` (handler `0x1801b700`)

The handler hex-dumps 22 body bytes (tag `test`), reads a **u16 BE at body
`0x14`** (bytes 20–21), and logs `app play control cmd:%d`. Then:

| value | name | effect |
|---|---|---|
| **1** | PlayLiveview | if `peerToPeerFlag == 1`: flag = 2, ~50-tick wait, close the P2P socket, **mode `dev->0xd0` = 1**. Else log `p2p is not finish` and do nothing |
| **2** | PlayPause | close the P2P socket, flag = 0, stop the session (`0x18012638`), wait `rtsp session free`, log the stored target `ip:%s port:%d`, restart the P2P thread. If it never comes back: `waitting p2p thread finished fail, device will reboot` |
| **3** | stop | `0x180182a0`, then `StopPlay(1)` (`stop play`) |
| other | — | `unknow play control cmd:%d` |

So the minimal frame is 20 bytes the handler does not interpret, followed by
the u16 value.

### 13.4 `cmd 0x8002` — heartbeat (handler `0x1801b6ca`)

Stamps `dev->0x34` with the current tick (`0x18038998`) and bumps a counter
at `dev+0x2e0`. On every 5th one the camera sends its own `cmd 0x0002`,
carrying 32 bytes from `dev+8`. The body is not read. The camera's
`0x0002` is therefore a heartbeat of its own, not "pullVideo"; that name
came from the local `CmdAPType` table (§8).

**The reader is the media state machine, at `0x1801a628`** (traced
2026-10-24, 16:20).
* **The check.** In mode 3 (playing, §13.5) it takes `|now − dev->0x34|`.
  At **36** or more, unless `dev->0x2f8` is set (not traced), it:
  * logs `live heartbeat timeout, stop live!!!!!` (`0x18061748`);
  * calls `StopPlay(0)`, which reports `mediaState` 0;
  * sets the mode to 0.
* **Who stamps the timer during play.** Play start stamps `dev->0x34`
  once (`0x1801a4a8` direct, `0x1801a50c` relay). By then the P2P
  thread's loop has exited (§13.2). From then on **only `0x8002` keeps a
  live view alive.**
* **Seen live, 16:16–16:23.** Every start ended 35–36 s later, and no
  `0x8002` was sent in that time.
* **What the vendor sends.** The 2 h vendor wiretap, with no viewer
  attached, holds no `0x8002` at all. **(inferred)** The cloud sends one
  for each `notify-live-event?eventType=0`.

**The check only runs while the sig link is up.** The state machine sits
in a loop with a gate at its head:
* the flag byte at `[0x20001ee0]+0x10`, read by `0x18017860`, checked at
  `0x1801a0b4`;
* session state `dev->0x2c ≥ 2`, checked at `0x1801a0c0`.

When the gate is closed, the loop only sleeps 1 s. So without a sig link
the timer is not checked. Seen live:
* From 16:14:32 to 16:16:25 no broker logged anything, in either
  `fg-broker.log` or `~/ziot-broker.log`. In that time the camera's
  frame counter moved by one. The camera streamed for the whole 2 minutes
  without a single `0x8002`.
* It stopped the moment it reconnected (`0x0004` value 0 at 16:16:27).
  **(inferred)** The stale stamp tripped the check at once.

The 36 s is the camera's own limit. The "session dies in ~12 s without
keepAlive" rule in `AGENTS.md` was measured against the vendor cloud.
**(inferred)** That limit is the cloud's, which stops the camera itself
once the app's keepAlive lapses.

**A second watchdog sits in the same loop** (`0x1801a54c`):
* If `|now − dev->0x260| ≥ 61`, it logs `cloud tcp connection is
  abnormal, need reboot!!!!!`, closes the sig socket and calls
  `0x18019f50`.
* The stamp is written at `0x1801aa24`, by a task not traced.
* It has not fired so far: one sig socket lasted from 16:16:27 to at
  least 16:23.

### 13.5 The state bytes

| field | values |
|---|---|
| `dev->0xc8` `peerToPeerFlag` | 0 idle · 1 P2P socket bound, waiting for the hello · 2 playing |
| `dev->0xd0` mode | 1 direct (set by PlayLiveview) · 2 relay (set by `0x8005`) · **3 playing**: the state machine starts the stream and sets 3 (`0x1801a49a`, `0x1801a4fe`) · 0 after the heartbeat timeout |
| `dev->0xd1` / `dev+0xe5` | the direct target chosen by `0x8101` |
| `dev->0xa4` | the RTP port (50001–59999), reused for STUN and the P2P socket |
| `dev->0x34` | liveness tick, refreshed by: P2P datagrams (only while the flag is 1), play start, `0x8002`. Stale for 36 s in mode 3 → stop (§13.4) |

`0x8005` sets the flag to 2 **unconditionally**; PlayLiveview sets it only
from 1. The media state machine reads `dev->0xd0`.
* **Mode 1:** it hands the `0x8101` target (`dev+0xd1`, port from
  `dev->0xe4`) to `0x180127a8` (`0x1801a4b6`–`0x1801a4d4`). Confirmed live:
  the media arrived at the `0x8101` pair.
* **Mode 2:** it hands the `0x8000` relay slot (`dev+0x4c`, port
  `dev->0x60`) to `spook_rtsp_set_relay_info` (`0x1801a518`–`0x1801a528`).
* Either way it then sets mode 3.

### 13.6 `notify-live-event` → frame, as the firmware implies it

**(inferred)**: the server side was never captured, but each firmware arm
matches one app call field for field.

| app HTTP call | frame the cloud would send | firmware evidence |
|---|---|---|
| `send-stun-addr` (publicIp/Port, privateIp/Port) | `0x8101` carrying the four fields | body = pair A + pair B, chosen by the app's own subnet rule |
| `eventType=1` start | `0x8004`, value 1 | PlayLiveview |
| `eventType=2` pause | `0x8004`, value 2 | PlayPause |
| `eventType=3` stop | `0x8004`, value 3 | stop |
| `eventType=0` keepAlive | `0x8002` | stamps the liveness tick |
| `eventType=5` relay | `0x8005` | RelayPro, mode 2 |
| `eventType=4` connected | (no camera frame identified) | the app's "P2P established" notify; **sent instead of `eventType=1` when `supportCacheStun()` is false — which is every X5 newer than `V1.0.11.46`, i.e. both our units** (§17.2). What the cloud sends the camera for it was never captured; **(inferred)** the same `0x8004`/1 play, since it is the unit's only live-start arm |
| `stun-addr/<uid>` (read) | served from the camera's `0x0003` | the `0x0003` body is the four `Ipc*` fields |

There is no `0x8003` because start/pause/stop are one command with the event
number in its body.

### 13.7 `mediaState` is the camera's own report (`cmd 0x0004`)

`0x180193a0(item)` builds the camera's status report and sends it as
**`cmd 0x0004`** (`0x18019546`, through the generic sender).
* The argument picks one item; 8 means all.
* The body is a count byte, then 4-byte entries `00 id 00 value`. Its
  length is `4 × count + 1`.
* **Argument 2 goes on the wire as id 3 and carries the byte
  `dev->0x2f4`.** A single report reads `01 00 03 00 VV`.
* A full report (argument 8) carries ids 1–7 and 9, for example
  `08 | 00 01 00 00 | 00 02 00 01 | 00 03 00 01 | 00 04 00 01 | 00 05 00 00 | 00 06 00 00 | 00 07 00 00 | 00 09 00 01`
  (16:16:35).

Every writer of `dev->0x2f4`, with the task it sits in:

| value | where | when |
|---|---|---|
| 3 | `CUSTOM_TASK_P2PThread` `0x18019716` | P2P socket bound, waiting for the viewer (§13.2); immediately reported with `0x180193a0(2)` |
| 3 | `CUSTOM_TASK_StopCloudPush` `0x180195a0` | end of a cloud push |
| 0 | `CUSTOM_TASK_StopPlay` `0x1801983c` | play stopped; reported with `0x180193a0(2)` |
| 4 | `CUSTOM_TASK_StopPlay` `0x180198aa` | other StopPlay branch |
| **1** | media state machine `0x1801a4dc` | the mode byte at a mode-1 start: **direct live** |
| **2** | media state machine `0x1801a530` | the mode byte at a mode-2 start: **relay live** |
| ? | `0x1801a6f2` | not traced |

**Live, 16:16–16:23**, after every `0x8101`, the camera reported:
1. **3**: the P2P socket is bound.
2. **0**: the start's own `StopPlay(1)` (`0x1801a492`).
3. **1**: direct live.
4. **0** again, 35–36 s later: the heartbeat timeout (§13.4).

So the device list's `mediaState` is the camera's own report.
`DECOMPILATION_REPORT.md` §5 called its server-side meaning unknowable from
the client; it is in the firmware. "3" means "P2P socket up after a
`0x8101`". That is exactly when the vendor captures show it flip 0 → 3
(§14.1): after `stun-addr` matched and the hellos went out, *before*
`eventType=1`. It goes back to 0 after the stop. The 1 and 2 seen in the
field are the state machine's writes: media flowing, direct or relay. The
camera sends a `0x0004` on every sig
cycle as well; it is the same status report, which the vendor acks with
`0x8001` (`cmd=4`).

---

## 14. The phone capture `PCAPdroid_24_out._13_47_51.pcap` (2026-10-24 13:47:51)

The app, captured by PCAPdroid in VPN mode (tun address `10.215.173.1`;
phone `192.168.68.106` on the LAN), talking to **the broker** through the
router routes. 706 packets.

**What the app did.** For `140979857781`, with times from the broker log:

| time | call | answer |
|---|---|---|
| 13:48:06 | `ipc/<uid>`, `run-info/<uid>`, `send-stun-addr?publicIp=186.233.129.211&publicPort=27719&privateIp=192.168.68.106&privatePort=59979` | viewer recorded as `192.168.68.106:59979` |
| 13:48:09–:14 | `stun-addr/<uid>` ×4, ~1.5 s apart | `IpcPrivate 192.168.68.107:54012`, `IpcPublic 186.233.129.211:27719`, `seqNo` 175/176/178/180 |
| 13:48:16 | **`notify-live-event?eventType=5`** (relay) | `"ok"` |
| 13:48:16 | `send-cmd {"cmdType":"20"}` (speakOff), then `eventType=3` (stop) | `"ok"` |

* **This is the first `eventType=5` ever seen on the wire** (§11 had only
  0 and 1). The app went to relay ~10 s after `send-stun-addr`, **without
  ever sending `eventType=1`** and without sending a single UDP hello to the
  camera. The device record's `relay_ip` is `""`, so the relay step had
  nothing to open, and no RTSP connection appears. No websocket was opened
  either.
* **The app's STUN.** RFC 3489-shaped Binding Requests (20 bytes, no magic
  cookie), each sent twice ~0.46 s apart, to `stunaddr.gps555.net:13478`,
  `stun.gps555.net:13478`, `stun.ekiga.net:3478` and `stun.12voip.com:3478`.
  Only `stun.12voip.com` answered as seen from the phone (MAPPED-ADDRESS
  `186.233.129.211:27719` plus SOURCE-ADDRESS and CHANGED-ADDRESS), and that
  is the public pair it registered. The broker logged **every** request to
  13478 and an answer to each (13:48:00–:04, :20–:25), but none came back
  through PCAPdroid. The answers left from `192.168.68.111`, not from the
  vendor address the phone had sent to (§12.5). With no internet the app
  would have had no public pair at all.
* **Why no `eventType=1`: the `seqNo` never matched** (settled by the
  vendor-era captures, §14.1). On the vendor, every `send-stun-addr` answer
  carries a new `seqNo`. The app keeps polling `stun-addr` until that same
  number comes back, and only then punches and sends `eventType=1`. Here
  `send-stun-addr` answered `seqNo` 172, and `stun-addr` answered 175, 176,
  178, 180. Those come from `HttpApi._stun()`, which bumps one shared counter
  on every HTTP answer to anyone (`self.seq += 1`), including the bridge's
  own polling. 172 never came back, so after ~10 s the app gave up and asked
  for the relay. 175 > 172 did not satisfy it, so **(inferred)** the check
  is equality, not "newer than".
* **The vendor's `IpcPublic*` is the camera's own pair, not the viewer's**
  (§14.1). The broker's `_stun()` docstring ("the public pair is the
  VIEWER's") misread the 2026-08-29 capture. Phone and camera shared one
  NAT there, so the IPs matched, but the ports never did.

### 14.1 The vendor-era captures, re-read (2026-08-27 and 2026-08-29)

Two phone-side captures of a **working** live view against the real vendor:
`PCAPdroid_27_ago._21_00_10.har` (+ the raw `…_21_00_04.txt`) and
`PCAPdroid_29_ago._15_13_27.pcap`, both in `~/Downloads/Telegram Desktop`.
Camera `141030191094` at `192.168.18.75`, phone at `192.168.18.30`.

**The app's sequence (27 Aug, HAR).** Times are the phone's (UTC−3).

| time | call | answer |
|---|---|---|
| 20:52:53.4 | `send-stun-addr` (viewer `…:56896`, public port 64642) | `seqNo` **186**; camera `192.168.18.75:52508`, public port 65234 |
| 20:52:54.5 | `send-stun-addr` (viewer `…:53223`, public port 65018) | `seqNo` **187** |
| 20:52:54.8 | `send-stun-addr` (viewer `…:52094`, public port 65449) | `seqNo` **188** |
| 20:52:57.8 | `stun-addr` | `seqNo` 186, `updateTime 07:52:56`, public port 64709 |
| 20:52:59.3 / 20:53:00.8 | `stun-addr` ×2 | `seqNo` 187, `updateTime 07:52:59` |
| 20:53:02.3 | `stun-addr` | `seqNo` **188**, `updateTime 07:53:02`: matches the last registration |
| 20:53:04.0 | device list | **`mediaState` 0 → 3** |
| 20:53:07.1 | `notify-live-event?eventType=1` | `"ok"` |
| 20:53:12 … 20:53:47 | `eventType=0` every **5 s** | `"ok"` (live view running) |
| 20:54:36 | device list | `mediaState` back to 0 |

The 29 Aug pcap shows the same shape, with UDP visible:

* `send-stun-addr` ×2 (`seqNo` 114, 115), then `stun-addr` (114 at
  `updateTime 02:13:39`, then **115** at `02:13:42`).
* Then **`App send hello` ×2 + `App send heart for stun`** from the viewer
  socket (`:51877`) to the camera at **`192.168.18.75:53693`**
  (15:13:44–:46).
* Then `mediaState` 3 in the device list (15:13:48.4), then `eventType=1`
  (15:13:48.8), then keepAlive every 5 s.

What this settles:

* **`seqNo` is per registration.** Each `send-stun-addr` answer carries the
  next number, together with the camera's *current* record (`receiveTs` =
  the call's time). The `stun-addr` record moves to `seqNo` N about 3 s
  later, one registration at a time (186 → 187 → 188, ~3 s apart). The app
  waits for its own N. **(inferred)** The cloud sends `0x8101` per
  registration. The camera STUNs and answers with `0x0003`, which echoes N
  in its caller-supplied u16 (body offset 44, §12.1), and that report
  updates the record. The ~3 s per registration is one `ReportLocalAddr`
  round.
* **`IpcPublicIP/Port` is the camera's own STUN mapping.** Its ports
  (65234, 64709 on 27 Aug; 65003, 64695 on 29 Aug) match none of the
  phone's (64642, 65018, 65449; 64550, 64991). The IP is the same only
  because both sit behind one NAT. The public port changes between the
  `send-stun-addr` and `stun-addr` answers while the private port does not,
  which fits a NAT that maps each STUN destination separately.
* **`IpcPrivatePort` is the camera's RTP port, and it did not move.**
  52508 stayed fixed across three reports on 27 Aug, and 53693 on 29 Aug:
  no STUN retry happened. The punch went to exactly that port (29 Aug).
  **53693 is also the first port both cameras drew today**: `.108` at
  12:18:22, `.107` at 12:57:41, each at the start of a burst. So the RTP
  port draw (§12.1) is reproducible after a restart; `0x1801b024` reseeds
  before each draw, but the seed source was not traced. The August camera
  was on that first draw with a STUN that worked first time.
* **The app punches *before* it sends `eventType=1`.** Order: matching
  `seqNo` → hellos → `mediaState` 3 → `eventType=1`. The camera must
  therefore already have its P2P socket up (§13.2) when the hellos arrive.
  That is `0x8101`'s job, not `eventType=1`'s. **(inferred)** `0x8004`/1
  follows `eventType=1`.
* **The app's keepAlive cadence is 5 s** (both captures). The bridge's 2 s
  is stricter than needed, and either is inside the ~12 s limit.
* **The vendor's STUN answer to the phone** (29 Aug, RFC 3489 request) is
  `0x0001` + `0x0004` (`172.16.110.64:13478`, the server's private
  address) + `0x8020`. Its `0x8020` is XORed with **request bytes 4–7**,
  i.e. whatever sits in the cookie position. **(inferred)** A server that
  XORs by position like this would answer the camera's RFC 5389 request
  (cookie `2112a442`) with a proper `0x0020`. A dual-mode server sends the
  `0x8020` code only to legacy clients.
* Minor conformance difference: the vendor answered `send-cmd` with an
  object (`{"uid", "timeId", "msg": "ok"}`, 29 Aug), where the broker
  answers `"ok"`.

The camera's media-state report, read from the firmware to explain the
`mediaState` 0 → 3 → 0 above, is in §13.7.

**What it cannot show.** Nothing about the camera:

* From 13:41 to 13:53, `192.168.68.114:8882` was held by the opencode lab
  "v8". That was `labv7.py` (its log says `v7 owns`), whose parser checks a
  **sum** instead of the XOR, so it read no camera frame at all. The broker
  logged `push uid=… not connected` for every `notify`.
* The bridge was running against the same broker at the same time (its
  requests show up from `120.26.121.165:*`, this host's claimed address).
  It re-registered its own viewer (`192.168.68.114:55658` / `:33223`) about
  every 60 s, overwriting the phone's (13:48:06 phone, 13:48:32 bridge). The
  broker keeps one viewer per uid.

---

## 15. What §12–§14 imply

**Status at 16:30:** these are in `ziot_fake_cloud.py`:
* item 1 (the `0x0020` attribute);
* the parse half of item 3 (`stun-report`);
* item 5 (`0x8101`);
* the `eventType=1` half of item 6 (`0x8004` value 1 plus a hello punch).

Items 2, 4 and 7, and the rest of item 6, are not. With what is in,
camera .107 streams in 36 s bursts (§0, §13.4).

In order of expected payoff:

1. **Give the camera a `0x0020` XOR-MAPPED-ADDRESS.** Port XOR `0x2112`,
   address XOR the cookie `21 12 A4 42`, both taken from the camera's own
   request. Keeping `0x0001`/`0x0004`/`0x8020` alongside is harmless: the
   camera skips unknown types by their length. The phone's RFC 3489
   requests (no cookie) can keep the current shape. A test should parse the
   reply exactly as `0x180190e8` does (§12.2). **Cheapest live check:** the
   camera sends `0x0003` within a second of `0x8021`, and its sig session
   stops closing at ~6 s. **DONE AND PROVEN (16:16-16:18):** after the fix
   the `0x0003` reports arrive every cycle (`stun-report` lines:
   `public=192.168.68.107:<port> local=...:<port>`), and with the direct
   live chain (§13: `0x8101` viewer pairs → `0x8004` value 1 → hello punch
   at the camera's RTP port) the bridge's `/health` showed **`frames_total`
   1466+, `audio_pkts` 4670+, `rx_datagrams` 14412+, `fps` 7.8,
   `streaming: true`** — the user confirmed frames on screen. The relay
   path (`0x8005`, mode 2) was never needed for a same-LAN viewer.
2. **Answer from the address the request was sent to.** That means one UDP
   socket per claimed address (`47.238.125.111`, `8.130.23.234`,
   `192.168.68.114`), or `IP_PKTINFO`. The camera does not need it; the
   phone (and anything with a connected UDP socket) does. The same applies
   to the DNS listener on 53/udp.
3. **Parse `0x0003`** (layout §12.1) and serve `stun-addr/<uid>` from it:
   private = local pair, public = the camera's STUN pair (**not** the
   viewer's, §14.1). The vendor's answer to `0x0003` was never captured;
   **(inferred)** an `0x8001` ack (`cSeq`, `cmd=3`, `ack=0`) like those for
   `0x0002`/`0x0004`.
4. **Give `seqNo` the vendor's meaning** (§14.1). Each `send-stun-addr`
   answer takes the next N and carries the camera's current record.
   `stun-addr` shows N only once the camera has reported after that
   registration. The app waits for its own N and does nothing else until
   it sees it (§14). This is why the 13:47 app never punched. A per-uid
   counter would do, not one shared by every HTTP answer.
5. **Send `0x8101` when a viewer registers** (`send-stun-addr`): bytes 0–19
   zero, pair A at 20/40, pair B at 42/62 (§13.1). On a LAN-only setup, put
   the viewer's LAN `ip:port` in **both** pairs. The camera picks B only if
   its STUN-learned public IP equals A's IP and the /24 matches, so identical
   pairs make the choice moot. The vendor handles several registrations
   one after another, ~3 s each (§14.1).
6. **Translate the events** (§13.6): `eventType=1` → `0x8004` value 1,
   sent only after the camera's P2P thread is up (after the `0x0003` that
   follows `0x8101`; too early and the camera logs `p2p is not finish`);
   `eventType=0` → `0x8002`; `eventType=2`/`3` → `0x8004` value 2/3;
   `eventType=5` → `0x8005` (relay only). The app sends keepAlive every
   5 s.
   **`eventType=0` → `0x8002` is now the blocker (seen 16:16–16:23).**
   * Without it the camera ends every live view 35–36 s after the start
     (§13.4).
   * Any cadence under 36 s will do. One `0x8002` per `eventType=0` (the
     bridge sends one every 2 s) is the vendor-like choice.
   * Every 5th `0x8002` makes the camera send its own `0x0002`. The broker
     already answers that with the wiretap's `0x8001`.
   * Expect `0x8002` to keep the sig link's traffic up as well, against
     the 61 s watchdog (§13.4). **(inferred)**
7. **Serve `mediaState` from the camera's `0x0004`** (wire id 3, §13.7)
   instead of a fixed `"0"`. The values are: 0 stopped, 1 direct live,
   2 relay live, 3 P2P socket waiting for the hello, 4 the other StopPlay
   branch. "3" after `0x8101` is what the vendor's device list showed just
   before the app's `eventType=1`.
8. **Lab hygiene before the next live test.**
   * One owner of `:8882` at a time.
   * Do not run the bridge and the phone app against the same uid at once
     (single viewer slot).
   * Log the *destination* address of each STUN request, to know which
     claimed IP the camera used.
   * Route `47.97.71.82` as well as `47.110.77.193`: the wiretap reached the
     vendor's sig server at `47.97.71.82:8882`, and the vendor's own
     `0x8000` names `47.97.71.82` in its sig slot.
9. **Validation to look for**, in order:
   * `0x0003` on sig;
   * the camera's STUN bursts shrinking to one request;
   * after `0x8101`, the viewer's hello reaching the camera's RTP port
     (`tcpdump -i wlan0 'udp and host <cam> and port <rtp port>'`);
   * after `0x8004`/1, UDP RTP from the camera's RTP port to the chosen
     target, and the bridge's `rx_datagrams` climbing (**seen 16:14**);
   * after the `0x8002` fix: no `0x0004` value 0 at 36 s after each
     value 1, and the bridge's `re_rendezvous_count` holding still.
10. **Follow the latest `0x0003`.** After a repeat `0x8101`, the camera's
    first STUN sometimes fails and it draws a new RTP port (16:19:08
    `:58328` → 16:19:09 `:52167`). The media then comes from the new
    port. So the punch and the bridge must use the newest report, as they
    do now. **(inferred)** The previous session's socket on the same port
    takes the reply.

---

## 16. Live runs, 2026-10-24 22:28–23:41: what they added

This section follows the 22:28 box in §0. It covers:
- the broker from the main checkout, running since 21:33 on the 21:32 code;
- the bridge, running since 20:11.

Times are from the broker log unless marked otherwise.

### 16.1 The heartbeat holds: 16 minutes unbroken on `.108`

The run went from 22:28 to 22:44:
- 7.4–8.4 fps, 7,815 frames;
- the bridge's `re_rendezvous` stayed flat at 84 the whole time.

There was no 36 s stop. The `0x8002` forward (§13.4, §15 item 6) is
validated live.

The run ended because the *host* went to sleep, not the camera (§16.6).

### 16.2 The camera can be live before the bridge sees a frame

This is `.108` after a power cycle:

| time | event |
|---|---|
| 23:32:24 | `0x0001` register; the broker sends `0x8101` with the bridge's pair `192.168.68.114:46320` |
| 23:32:27 | `id3 = 3` (P2P socket bound) |
| 23:32:30 | broker hello ×3 → `:52241`, then `0x8004` |
| 23:32:31 | `id3 = 0 → 1`: direct live |
| 23:32:50 | the bridge leaves its 60 s backoff ("stream dead 445s"), re-rendezvouses and starts punching from `:46320`; the broker sends `0x8101` again, with the same pair |
| 23:32:57 | hello, `0x8004`, `id3 = 0 → 1` |
| ~23:33:00 | the bridge's first new frames (7,815 → 7,840) |

The camera was live from 23:32:31, but the bridge received nothing for the
next 19 s. That only changed once the bridge was punching from the socket
named in the `0x8101`.

The broker's own hello comes from a throwaway socket (`punch_camera`). So
the camera seems to send its RTP to the source of the hellos it hears (the
P2P thread logs each sender, §13.2), not just to the `0x8101` pair.
**(inferred; not checked in the firmware)**

If that holds, the broker's hello only satisfies the P2P thread. The
bridge's punch is what lands the stream.

### 16.3 The bridge's retry kills the broker's chain

This is `.107`:

| time | event |
|---|---|
| 23:35:15 | `0x0001` register → `0x8101` |
| 23:35:17–:20 | `id3 = 3`, three times |
| 23:35:22 | bridge log: cloud state `1/0 → 1/3`, so the bridge *knows* the socket is up |
| 23:35:23 | hello ×3 → `:54739`, `0x8004` (p2p bound) |
| 23:35:30 | bridge log: "109 direct attempts failed — trying the relay" → `eventType=5` (`0x8005` held) |
| 23:35:31 | bridge `send-stun-addr` + `eventType=1` → the broker sends `0x8101` again |
| 23:35:39 | no `id3 = 3` since; the broker sends `0x8004` on its 6 s fallback |
| 23:35:40 | `id3 = 0` |

The bridge retries on its own ~60 s cycle, whatever the camera is doing.
Each retry's `notify(1)` becomes a `0x8101`, and §13.1 says a `0x8101`
stops a view that is playing. Here the retry landed 8 s after the play, so
the view never came up. `.108` at 23:32 survived because its retry landed
19 s *after* it went live.

Two consequences:
* **The fallback play still fires.** `P2P_PLAY_FALLBACK_S` sends `0x8004`
  6 s after a `0x8101` that the camera never answered with `id3 = 3`. The
  22:28 box (§0, item 2) says that must not happen. It fired at 23:32:36
  and again at 23:35:39, after the bridge's retries.
  **Fixed 2026-10-25:** the 6 s is now only a deadline. With no
  `id3 = 3` the pending play is dropped, not sent.
* **Either side can break the cycle.** The bridge could skip the retry while
  its cloud state reads `/3` or `/1`. Or the broker could hold a repeat
  `0x8101` within ~30 s of the last chain. Neither is implemented.

### 16.4 `.107` fails without the collision too

| time | event |
|---|---|
| 23:31:55 | register → `0x8101` |
| 23:31:58 | `id3 = 3` |
| 23:32:01 | hello → `:59890`, then `0x8004` (p2p bound) |
| 23:32:18 | `id3 = 0`, without passing through 1 |

The next bridge retry came only at 23:32:28.

Everything visible was the same for both cameras:
- the same sequence of frames;
- a `0x0005` with the same `vendor-ack`;
- one redraw of the RTP port after the first `0x8101` (`.107` `53298 →
  59890`, `.108` `52508 → 52241`).

The 22:28 box says a power cycle revives `.107`. This one, at ~23:31, did
not. **Open.**

### 16.5 The app no longer tries P2P (23:37 and 23:40)

Two app sessions on `.107` went through the broker (phone
`192.168.68.106`). Both took the same path within 10–16 s:
1. `/ipc/<uid>`
2. `current-package`
3. `notify-live-event?eventType=5`
4. one `eventType=0`, at 23:40:15
5. `send-cmd` ×2
6. `eventType=3`

There was no `run-info`, no `send-stun-addr` and no `stun-addr` polling.
No STUN from the phone reached the broker between 23:20 and 23:39. At
13:47 (§14) the same app tried P2P first.

What differs. None of these is confirmed **(inferred)**:
1. **`relay_ip` in the device row.** The broker runs with `--relay-port
   58918`, so the row carries `relay_ip = 192.168.68.114:58918` and
   `server_ip`. But the vendor's rows carried a `relay_ip` too
   (`156.246.16.114:554`, README).
2. **`.107`'s sig link.** It was "not connected" by 23:38:35, yet the row's
   `onlineState` is always `"1"`.
3. **The app may remember** the 13:47 P2P failure.

The test that separates them:
- start the broker without `--relay-port`;
- stop the bridge (its retries re-registered its own viewer mid-session, at
  23:38:35);
- register `.107` fresh;
- open the app once.

> **Partly resolved statically, 2026-10-25 (§17.3).** What is proven:
> the app has a punch-timeout → relay fallback — the punch completion
> callback maps `1` (established) → `start` and `0` (timeout) → **relay**,
> logging `公网 IP 获取异常, 走转发`; `natType` is never read by the app
> (only `fromJson`/`toJson`), killing hypothesis 2's neighbour field; and
> `relay_ip` is read only *inside* the relay branch, so hypothesis 1 cannot
> *select* relay — an empty `relay_ip` only makes the relay attempt itself
> fail. Still open: at 23:37 the app sent no `send-stun-addr`, and the
> punch runs only *after* `send-stun-addr` inside `_prepareForStun`, so the
> app apparently reached relay without punching at all. The entry point
> that sets `connectType` to relay on page open is not traced
> (`HomePageAction.debugActionForForwardConnect` is a debug-only action;
> the normal path is not). A capture of the app's own log at that moment
> (`P2P Server 连接超时` vs `获取转发地址异常`) would settle it.

### 16.6 Two lab conditions that looked like protocol failures

* **The host went to sleep.** GNOME's `gsd-power` idle-suspended the laptop
  at 22:44:45 for 41 min. The sig links died, and `.108` did not come back
  until it was power-cycled. The telltale is a gap in the bridge log where
  even the 30 s stats line stops.
* **The cameras transmitted but did not receive (21:17).** `.108` broadcast
  `who-has 192.168.68.1` 24 times in 25 s and got no answer. Neither
  camera answered the host's ARP, and sig never completed. A power cycle
  cleared it.

Read `ip neigh` and an ARP capture before reading a protocol fault into a
silent camera. That is the same lesson as the firewalld episode.

### 16.7 Compliance against the 21:32 broker

This audits the main checkout against this document (camera plane) and the
13:47 capture (app plane).

* **Matches:**
  - the frame layout, the XOR and the `0x0D` end byte;
  - STUN `0x0020`;
  - the `0x8000`, `0x8101` (offsets 20/40/42/62), `0x8004` and `0x8002`
    bodies;
  - `mediaState` taken from `0x0004`;
  - `0x8005` off by default;
  - the AGENTS.md rules.
* **Fixed by 21:32** (open in §15):
  - `IpcPublic*` is the camera's own pair, from `0x0003`;
  - `seqNo` is per camera and shown only after the camera reports;
  - STUN replies go out via `IP_PKTINFO`;
  - unknown commands get a vendor-shaped `0x8001`; the sweep sits behind
    `ack_sweep`, off.
* **Still open:**
  - **The unknown-command `vendor-ack` body is 6 bytes:** `00 | cSeq | cmd
    | 00`. For example `00 00 0b 00 05 00` answers `.108`'s `0x0005` seq
    11 (23:32:30); this is proven from the broker's log and code. Read with
    the 5-byte layout, it says cSeq `0x0000`, cmd `0x0b00`.

    What the vendor sends for `0x0005` is **not known**. Every one of the
    vendor's 1,138 `0x8001`s in the 2 h wiretap (`~/ziot-wiretap.log`,
    2026-10-23) is 5 bytes, `cSeq | cmd | ack`: 570 answer `0x0002` and 568
    answer `0x0004`, all with ack = 0. The broker's own `0x0002`/`0x0004`
    acks match that. But in that session the camera sent only `0x0001`,
    `0x0002`, `0x0004`, `0x0021` and `0x1601`, never `0x0005`. So a 5-byte
    answer to `0x0005` is an extrapolation **(inferred)**.

    The 13:47 phone capture cannot settle it. It holds no sig traffic
    at all: no port 8882, and nothing to or from 47.110.77.193 or
    47.97.71.82.

    It is not shown to do harm: `.108` gets this ack every session and
    streams.
  - `eventType=5` → `0x8005` is held by design (`--relay-start`).
  - `GET /api/v1/lang/pt` answers 404. The app asks for
    `/v1/lang/applang` (§17.5); the vendor's answer shape is unknown and
    the app carries on without it.
  - The bridge's ~60 s retry can still send a fresh `0x8101` into a view
    that is coming up (§16.3). That retry lives in the bridge, not the
    broker.

---

## 17. What the app's own code settles (2026-10-25, Blutter)

Read from the app's AOT snapshot (`/tmp/opencode/out`, same build as
`DECOMPILATION_REPORT.md`; Dart addresses are that snapshot's). This maps
the *app-side* of the connect flow — the part the wire captures could only
hint at. Where a claim is an inference it is marked.

### 17.1 The connect pipeline

Opening a camera runs `CameraPageController.connectType` →
`handleActionForPrepare` (`0x86dd74`) → `handleCameraConnect`
(`0x71e0f8`), which switches on the `CameraConnectState` index (§1.4):

| state | what the app does |
|---|---|
| `prepare` (1) | the cached-stun fast path if `isCacheStun()` (§17.3), else `_prepareForStun` (§17.4) |
| `start` (2) | immediately recurses into `p2p` |
| `p2p` (3) | `requestSendConnectState` (§17.2) + `requestSendHeart` (eventType=0) + `_startFCMediaRTSP` (the local ZLMediaKit player) |
| `relay` (4) | reads `infoModel.relay_ip` (logs `当前转发地址是: `), `setupRelayVideo(relay_ip, cb)`, then `requestSendRelay` = `eventType=5`; empty `relay_ip` does **not** prevent the notify — the player callback reports `获取转发地址异常` and exits |
| `directConnect` (5) | `directConnectVideoRtpPush` if the `rtpPush` arg is `"1"`, else `directConnectVideo(directUrl)` |
| `stop` (6) | `requestSendStop`: teardown notify — `eventType=2` (pause) only for CacheStun devices, otherwise **`eventType=3` (stop)** — then `_stopP2PSocket` + `_stopFCMedia` |

`handleActionForPrepare` first gates on `isFree()` (§2.1): if the device
row's `mediaState` is not `"0"` it logs `设备繁忙, 请稍后再试` and
**exits without connecting**. The broker must serve `mediaState "0"` when
idle or the app refuses to open the camera at all.

### 17.2 `eventType=4` is these units' live-start notify

`requestSendConnectState` (`0x725a4c`) picks its event by
`infoModel.supportCacheStun()` (`0x65a54c`):

```dart
requestSendEvent(supportCacheStun() ? CameraEventType.start   // 1
                                    : CameraEventType.connected); // 4
```

`supportCacheStun()` is `deviceNeedUpdate(version, "TXW817_A_V1.0.11.46")`
for an X5 — **true only when the firmware is older than V1.0.11.46** — and
unconditionally true for every other camera type. Both units run
`TXW817_A_V1.0.12.32`, so on our cameras the app's "P2P established"
notify is **`eventType=4` (connected), never `eventType=1`**. The vendor-era
captures' `eventType=1` (§14.1) must predate this app/firmware pair
**(inferred)**.

There is no firmware handler identified for "connected" (§4 has no such
arm); **(inferred)** the cloud triggers the same `0x8004`/1 play it would
for start.

### 17.3 The cached-stun fast path — dead code for our units

`isCacheStun()` (`0x7275b0`) = `supportCacheStun()` **and**
`StunManager.cacheUID() == uid`. On an X5 ≥ V1.0.11.46 it is always false,
so `handleCameraConnect` always logs `旧的逻辑` ("old logic") and runs
`_prepareForStun`. The cached path (worth knowing, dead here): it reads the
per-uid `StunState` (`rtpPortWithUid`, `0x72754c`) and picks **`p2p`** if
the state is `success`/`heartLive`/`pause`, else **`relay`** — this, not
the punch, is the app's "remembered P2P works/failed" switch.
`StunState` (`pp.txt`): working(0) success(1) failed(2) init(3) none(4)
pause(5) heartStart(6) heartLive(7) relay(8).

### 17.4 The P2P punch, from the app's side

`_prepareForStun` (`0x725b84`):

1. Draws the viewer punch port: `Random().nextInt(10001) + 50000`
   → **50000–60000** (stored in a static; this is the port that goes into
   `send-stun-addr` as `privatePort`).
2. `checkStunPublicAddress` ×2 (the phone's own STUN); both null → toast
   `网络异常` and give up.
3. `requestStunAddress` = `GET send-stun-addr` with the four fields.
4. `_startConnectDevice` → `_startP2PConnect` (`0x725fe0`).

`_startP2PConnect` binds a UDP socket on `anyIPv4` (log `UDP Server
listening on `), starts the **1 s** punch `Timer.periodic`
(Duration 1,000,000 µs — the §4.3 constant), and listens:

* every datagram **from the camera** decrements a counter seeded at **20**
  and logs `P2P Serve 接收到消息, 需要重新发几次` ("received, must resend
  a few more times") while the counter is > 9;
* when the counter drops to ≤ 9 — **~10 datagrams in** — it logs
  `P2P Serve 连接设备: 成功 success`, closes the socket, and calls the
  completion with **1**;
* a timeout path logs `P2P Server 连接超时` and calls the completion with
  **0**.

The completion (`0x726af4`) maps `1` → callback(**2**), `0` →
callback(**4**); the `handleActionForPrepare` closure (`0x86df24`) maps
`2` → `CameraConnectState.start`, `4` → `CameraConnectState.relay`
(anything else → log `公网 IP 获取异常, 走转发` → relay).

Two consequences:

* **The punch succeeds only if the camera answers.** The success criterion
  is *receiving* ~10 datagrams, so the camera must send datagrams back to
  the viewer while it waits **(inferred from the app; the firmware's P2P
  thread, §13.2, is only documented to recvfrom and stamp — its reply, if
  any, is not traced)**. That turns §16.2's inference ("the camera sends
  its RTP to the source of the hellos it hears") into a functional
  requirement on both sides: no camera replies → no live view, straight to
  relay.
* **A silent camera produces relay, not an error.** This is the fallback
  behind §16.5.

### 17.5 `get-sig-ip` and `lang` — the two former 404s, from the app's side

* **`GET /v1/ipc-function/get-sig-ip`** (`CameraAPI.requestSigIPAddress`,
  `0x782dbc`): sent with param `supportLoadBalance=1`. The app reads
  `answer["code"]` and **when it equals 400** takes `answer["data"]` (a
  string) and passes it to `Application.saveSigAddress` →
  `SpUtil.putString("icpSigAddress", …)`. That `icpSigAddress` is exactly
  the `urlAndPort` the BLE `wifiConnect` frame carries
  (`DECOMPILATION_REPORT.md` §14.0). So **serving
  `{"code": 400, "data": "<bridge-LAN-addr>:8882"}` makes the app
  provision cameras with the bridge's sig server** — the cleanest adoption
  lever there is. The broker serves that answer (`HttpApi.sig_address`,
  2026-10-25). (The vendor using `code 400` as the success marker here
  is what the code says; not confirmed against the vendor's answer.)
* **`lang`**: a field of the `POST /v1/login` body; the app also fetches
  dictionaries from `/v1/lang/applang` (path built in `request.dart`,
  `/v1/lang/` + `applang`) and caches them under `lang_data_map_<…>`. The
  answer shape is unrecovered; the app carries on without it (§16.7).

### 17.6 `natType` is never read

`natType` appears only in `CameraInfoModel.fromJson`/`toJson` (`0x60c444`,
`0x60d45c`). No Dart code branches on it. Any theory that makes the app's
connect path depend on the device row's `natType` is dead.

### 17.7 Broker implications (in the broker as of 2026-10-25)

1. **`eventType=4` is mapped like `eventType=1`** (`schedule_p2p_live` →
   `0x8101`, then `0x8004` value 1 once the camera reports `id3 = 3`).
   On these units the app sends `connected`, not `start`.
2. **`GET /v1/ipc-function/get-sig-ip`** answers
   `{"code": 400, "data": "<this broker>:8882"}` (`HttpApi.sig_address`).
   The app saves that as `icpSigAddress` and puts it in the BLE
   `wifiConnect` frame (§17.5). The sig plane off (`--sig 0`) answers
   `data: ""`.
3. **`mediaState` is `"0"` whenever nothing is live.** A sig link
   closing resets it (the camera can no longer report the stop), and
   loading the camera file at startup does the same. A non-zero value
   makes the app exit with `设备繁忙` (§17.1).
4. **The punch needs camera replies** (§17.4): the app's punch only
   completes after ~10 datagrams back from the camera. If a firmware stops
   answering hellos on its RTP port while `peerToPeerFlag == 1` (§13.2),
   app-side live view dies to the relay fallback even when the bridge's own
   punch still works. That reply is the camera's, not something the
   broker can send.

---

## 18. Live run, 2026-10-25 07:39–08:55: the overnight stop, and a restart on the 08:27 broker

Sources: the broker log (`~/ziot-broker.nohup`), the bridge log
(`~/ziot-bridge.nohup`), and a 4-minute capture of both cameras taken
across the restart (08:45:28–08:49:28, `tcpdump -i any`).

### 18.1 What stopped the stream overnight

It was the host, not the protocol. This is the §16.6 condition again.
* At 00:00:27 GNOME idle-suspended the laptop (`sleep-inactive-ac-type =
  'suspend'`, 900 s, on AC power). It resumed at 07:39:30. The bridge
  log has a gap from 00:00:34 to 07:39:31, then
  `send-stun-addr: Network is unreachable`.
* At 07:39:38 `.108` re-registered **without having rebooted** (its sig
  counter carried on at 210). The chain ran: `0x8101`, `id3 = 3`, hello,
  then `0x8004 (p2p bound)` at 07:39:47. The camera went `3 → 0` and never
  reached 1.
* Its sig TCP then stalled for 31 s. At 07:40:33 the stall ended with 15
  coalesced `0x0002`s in one 360-byte read, and the link dropped. `.108`
  stayed off sig until it was power-cycled at 08:36:15 (counter back to 1).
  `.107` behaved the same way.
* After the 08:36 power cycle, `.108` streamed on the *21:32* broker,
  without a code change.

### 18.2 `.107`: transmits, does not receive

From 08:37:18 to at least 08:49, `.107` broadcast `who-has
192.168.68.114` about once a second: 225 requests in the 4-minute
capture. The host answered all 225, and the camera kept asking. The
host's own ARP probes to `.107` went unanswered (`ip neigh` stayed in
`PROBE`). The bridge's punches (280 datagrams) went out, and nothing came
back.

So the camera's unicast receive path was dead. It could not reach sig
and could not send media. `.107` had gone `id3 = 1` at 08:37:17 (after
the old broker's fallback play) and dropped sig one second later. A
power cycle cleared it at 08:56.

This was first read as a radio fault. §18.6 shows the chain failing on
our side first, so that reading is withdrawn: `.107` streamed through
this broker on 2026-10-24 16:14.

### 18.3 The camera streams to the `0x8101` viewer pair, not the hello source

The 08:45:30 restart of both processes settles §16.2's inference, and
settles it the other way.
* The restored viewer file made the register-time `0x8101` (08:45:31)
  name the *old* bridge port `:46320`. The new bridge process was on
  `:59394`.
* From 08:45:33 the new bridge punched the camera's RTP port `:54095`
  from `:59394` (24 datagrams by 08:45:52).
* After the play (08:45:40) the camera streamed **from `:54095` to
  `:46320`**: 1,004 packets, 08:45:42–08:45:53, ~85/s. That is a port
  nobody was listening on.
* Media moved to `:59394` only at 08:45:52. That was after the bridge's
  retry had sent a `0x8101` naming `:59394` (08:45:48), the camera had
  re-bound on `:58310`, and a new play had gone out (08:45:53).
* Before each play, the camera's P2P socket sent single datagrams to the
  `0x8101` pair: 08:45:37, :40 and :41, from `:54095` to `:46320`.
  These look like its own punches. They are the datagrams §17.4 says the
  app's punch counts **(inferred)**.

So the hello only satisfies the P2P thread. **The media goes to the pair
in the `0x8101`.** A `0x8101` naming a stale port wastes the whole view.

### 18.4 The registration number the camera echoes is the `0x8101` *header* seq

In every chain since 22:27, the `0x0003`'s field at body offset 44
equals the header seq of the `0x8101` just sent. It never equals the
body's bytes 0–1, where the broker puts `pending_seq`. The redraw report
that follows carries 0.

| `0x8101` sent | header seq | camera's `0x0003` n |
|---|---|---|
| 22:27:47 `.107` | 1 | 1 |
| 22:28:24 `.108` | 13 | 13 |
| 23:32:50 `.108` | 15 | 15 |
| 07:39:38 `.108` | 210 | 210 |
| 08:36:39 `.108` | 15 | 15 |
| 08:45:48 `.108` | 87 | 87 |

So `stun-addr`'s `seqNo` is the camera's own sig counter. It never equals
the number `send-stun-addr` answered with. §14.1's round trip, where the
app waits for its own number, cannot close as the broker is built. It
also falls back to 1 when the camera reboots, and the bridge then logs
`stale stun rejected: seqNo 1 < 210` until its next re-rendezvous.
§12.1's "registration number" field is real; it is fed from the header,
not from the body.

### 18.5 The restart, on the 08:27 broker

Both processes were restarted at 08:45:30, with unchanged flags.
* `.108` re-dialled sig 1 s after the old broker died: it got RST, then
  SYN-ACK at 08:45:30.5. It re-registered at 08:45:31.
* The chain was `(p2p bound)` both times; the dropped fallback never
  fired.
* The camera went live at 08:45:42 to the stale port (§18.3), and live
  to the bridge at 08:45:54.
* From then on: 7.4–8.0 fps, `re_rendezvous` flat at 2, 3,579 frames by
  08:53:41. A grabbed MJPEG burst decodes as 640×480 frames. One frame
  in 25 (the first one served) was truncated; main's bridge does not
  carry the frigate branch's drop-incomplete-frames fix.

The ~20 s of dead air at boot comes from `schedule_p2p_live`. The
bridge's first `send-stun-addr` with its new port (08:45:33) was a no-op,
because a chain was already pending with the restored, stale pair. The
right pair went out only on the bridge's next retry.

### 18.6 `.107` fails because the hello goes to the wrong port, and the host firewall hides the right one

Every `.107` chain since 22:27 sent one or two `0x0003` reports after the
`0x8101`. One was the report that answers it (`n` = the `0x8101`'s
header seq), and one was a redraw with `n = 0`. The broker sends its
hello to the **newest** port (§0, the 22:28 box). The outcome follows
whether the two ports agree:

| `.107` chain | answering report | redraw | hello to | result |
|---|---|---|---|---|
| 22:27:47 | 53693 | 55684 | 55684 | live, then died ~30 s |
| 23:31:55 | 53298 | 59890 | 59890 | 3 → 0 |
| 23:35:15 | 53298 | 54739 | 54739 | never live |
| 23:48:22 | 52506 | 55727 | 55727 | never live |
| 07:40:06 | 53298 | 58851 | 58851 | 3 → 0 |
| 08:36:23 | 53298 | 53830 | 53830 | 3 → 0 |
| **08:56:41** | **52901** | **52901** | **52901** | **live** |
| 08:58:14 | 53298 | 54611 | 54611 | never live |
| 09:02:05 | 53691 | 50156 | 50156 | never live |

The 08:58 capture shows where the P2P socket really was.
* From 08:58:21 to 08:58:37 the camera sent its own 18-byte punch every
  2 s from **:53298**, the answering report's port, to the `0x8101` pair
  (`.114:51608`). The broker's hello and the bridge's punches all went
  to :54611.
* At 09:02 it was the same: punches came from :53691, and the hello went
  to :50156.
* So `.107`'s waiting socket is the *answering* port, and it never heard
  a hello. `.108` has gone live with the hello on the redraw port
  (22:28, 08:36), so the broker has to punch **both**.
* `.107` boots with 53298 as its first port almost every time, which is
  why it fails so consistently.

**The host firewall hides the camera's own punches.** firewalld's
`public` zone (where `wlan1` sits) opens the broker's ports (80/8882/
7080/13478) but not the bridge's UDP ports, which are ephemeral.
* A camera datagram reaches the bridge only if it matches a conntrack
  entry made by the bridge's own punch, i.e. only from the exact port
  the bridge punched.
* Every `.107` punch from :53298 and :53691 was answered with `ICMP host
  unreachable - admin prohibited` (17 in the `.107` capture).
* So was all of `.108`'s media to the stale `:46320` after the restart
  (§18.3).
* With the firewall in the way, the bridge never sees the camera's real
  port, and media from a port it did not punch is dropped.

**What the camera does after a failed chain.**
* At 08:58:22.372 our `0x8004` was never acked. The TCP retransmits ran
  to 08:58:35, and the camera re-sent its own ping because it never saw
  our acks.
* It closed sig at 08:59:08 and re-registered **with seq 1** at 09:02:05,
  i.e. it rebooted.
* In the 08:56:50 cycle it streamed for 24 s, then everything stopped
  at 08:57:15.1. It re-registered with seq 1 at 08:58:14, 59 s later,
  close to the 61 s sig watchdog (§13.4).
* Nothing unusual was sent to it before that stop: `.108` gets the same
  heartbeats, acks and 60 bridge punches a minute, and holds. That death
  stays **open**.

Fixes:
* **Broker:** punch every port the camera reported since the last
  `0x8101`.
* **Broker:** re-send a pending `0x8101` when the viewer pair changes
  (§18.5).
* **Host:** accept UDP from the cameras in firewalld. On the homelab
  server, which runs ufw, the same rule is needed.

### 18.7 A burst of `0x8101`s takes a live camera off the network

09:14, broker with the §18.6 port fix, before the firewall rule was in:
* `.108` reported two ports (51873, then 51781). The hello went to both,
  and it went live at 09:14:43. **It streamed from :51783, a port it
  never reported.** The bridge had punched only :51781, so firewalld
  rejected the whole stream (`admin prohibited`). The camera's media
  port cannot be predicted from its reports, so the firewall rule is
  required. Conntrack luck is not enough.
* The bridge saw nothing and retried: a `0x8101` with the same pair at
  09:14:53, 09:15:03 and 09:15:23. The camera never reported `id3 = 3`
  again, and each play was dropped.
* At 09:15:32 its sig TCP flushed a 365-byte coalesced burst, and the
  link dropped. `.108` was still off sig at 09:38: neighbour `DELAY`,
  no reconnect.

This is the same shape as `.108` at 07:40 and `.107` at 08:37: repeated
`0x8101`/plays into a camera that is live or coming up, then a stall,
then a deaf camera. The vendor app sends one `0x8101` per session. The
broker now holds a same-pair `0x8101` sent within `P2P_RESEND_MIN_S`
(30 s) of the last one. A changed pair, or a camera that has just
registered, always gets one. Why the camera wedges is not traced in the
firmware **(open)**.

### 18.8 `stun-addr` must serve the report that answers the `0x8101`, not the latest one

10:23, after a power cycle, with the firewall rule in and the §18.6/§18.7
broker. **`.108` streamed, `.107` did not:**

| | answering report (`n` = `0x8101` header seq) | redraw (`n = 0`) | the bridge punched | camera punched us from | result |
|---|---|---|---|---|---|
| `.108` | 51323 | 58952 | **51323** | — | live; media from :58954 |
| `.107` | **52508** | 56481 | 56481 | **52508** | play never acked |

* The broker's hello reached both ports, including `.107`'s P2P socket
  `:52508` at 10:23:11.466.
* What differed is the **bridge's** punch, which comes from the viewer
  address named in the `0x8101`. The bridge punches `stun-addr`'s
  `IpcPrivatePort`, and the broker serves the *latest* report.
  * For `.108`, the bridge's 5 s poll landed between the two reports,
    so it kept punching the answering port.
  * For `.107`, the poll landed after the redraw, so the P2P socket only
    ever heard the broker's hello, from `.119`.
* `.107`'s 08:56 success fits too: both reports named :52901, so the
  bridge punched the P2P socket.
* Once more, `.107` stopped acking sig at the play: our `0x8004`
  (10:23:11.917) was retransmitted, and the camera re-sent its own status
  because it never saw our acks.

The vendor's record behaves the other way (§14.1). `stun-addr` moved to
seqNo N when the camera's `0x0003` echoed N, and `IpcPrivatePort` stayed
fixed across reports. So the broker now remembers the report whose `n`
equals the last `0x8101`'s header seq (§18.4) and serves that pair from
`stun-addr`. A redraw no longer moves the bridge's punch target.

**(inferred)** PlayLiveview sets the flag to 2 and closes the P2P socket
(§13.3). It seems to need the viewer's datagrams arriving on that socket
to finish cleanly. Without them, the camera's receive path wedges.

**Correction, 10:29: the bridge's punch is not required for `.108`.** At
10:27:53, on the broker with this fix, `.108` reported :58952 (answering
`0x8101` seq 42) and then :55702. It went live at 10:27:59, and media
reached the bridge from :55704. All that time the bridge was still
punching **:51323**, the previous chain's port; it does not re-poll
`stun-addr` while media flows. The camera answered each of those
punches with `ICMP port unreachable` (410 between 10:23 and 10:29). So
the only datagrams on `.108`'s P2P socket were the broker's hello from
`.119`, and that was enough.

What actually decides `.107` is therefore still **open**. Serving the
answering report matches the vendor (§14.1) and is kept. It recreates
`.107`'s one success (08:56, the bridge punching the P2P socket), but it
is not shown to be the cause. `.108`'s frame rate swinging between
2 and 8 fps (45–85 packets/s on a ~90 s cycle, no gaps, no rejects) comes
from the camera's own output, not the transport.

### 18.9 Both cameras stream: 10:31, broker with §18.6–§18.8 plus the firewall rule

`.107` was power-cycled and registered at 10:31:50.
* The answering report was :52508 (`n = 1`). The redraw was :56463.
* The broker logged `p2p socket :52508 (answers 0x8101 seq 1)` and kept
  serving that port. The bridge punched :52508, and the hello went to
  both ports.
* It went live at 10:31:57, `id3 0 → 1`, and media reached the bridge
  that same second from :56465, another unreported port.
* This is `.107`'s first success on a chain whose two reports differ;
  the earlier eight all failed (§18.6).
* At 10:33:20: 767 frames, 6.2 fps, no re-rendezvous since the chain.
  `.108` was alongside it: 16,041 frames, 5.8 fps. The user confirmed
  both visually.

What it took, in order of evidence:
1. **The host firewall must accept UDP from the cameras.** Each camera
   streams from a port it never reports (§18.7, §18.9), so conntrack
   from the bridge's punch cannot cover it. On the laptop this is a
   firewalld rich rule in `public`, per camera IP; the homelab server's
   ufw needs the same.
2. **The hello goes to every reported port** (§18.6).
3. **`stun-addr` serves the report that answers the `0x8101`** (§18.8,
   §14.1).
4. **There is a floor on same-pair `0x8101` re-sends** (§18.7). A changed
   pair or a fresh registration always gets one.

Still open:
* why `.107` died 24 s into its 08:56 stream (§18.6);
* whether item 3, rather than chance, is what made the 10:31 chain work.
  `.108` went live without the bridge's punch (§18.8 correction).

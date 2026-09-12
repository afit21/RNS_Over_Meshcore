# Field Test Report — alpha-0.1-snapshot1

**Source logs:** `fieldtests/raw/alpha-0.1-snapshot1/`
(`alpha0.1snap1rnslaptoplogs.txt`, `alpha0.1snap1transfernodelogs.txt`, `alpha0.1snap1testingnotes.txt`)

**Build under test:** matches `changelog.md` `[alpha-0.1.1] - 2026-09-12` (the log timestamps and this changelog entry share the same date, and the log's feature set — dual outgoing queues, adaptive path-discovery backoff, `direct_path_reset_threshold`, per-fragment stats — all correspond to that release).

## 1. Test setup

Two Heltec V3 nodes running the MeshCore Dynamic Interface under `rnsd`:

| Node | Role | Identity | Capability | Session window |
|---|---|---|---|---|
| `a` ("laptop") | Reticulum endpoint | `343377c464a79a48…` | edge (`can_route=False`) | 14:00:17 – 15:55:16 (restarted repeatedly, see §4) |
| `b` ("transfer node") | Reticulum router | `7bd024b5d082747e…` | router (`can_route=True`) | 12:45:22 – 16:04:28 (single continuous session, no restarts) |

Both were configured on MeshCore channel `RNSTunnel` (idx 35). The transfer node stayed stationary; the laptop node started at rest at the tester's house, then moved by car to other locations in town starting around 14:29, per the testing notes.

## 2. Timeline of testing activity (from testing notes)

- **~14:17** — Both nodes stationary in different rooms. Meshchat text messages exchanged; times-to-delivered reported by the tester: 10s, 22s, 15s, 34s (multiple send attempts noted for all of them).
- **14:20 – 14:24:50** — A 320×320 image sent over meshchat.
- **14:29** — Tester starts an LXMF ping to the laptop and begins driving around town, antenna position in the car not fully controlled.
- **14:55** — Tester stops and pushes an announce.
- **14:58** — Tester notes MeshCore detected a new repeater, but path resolution to the transfer node was slow.
- **15:01** — Ping stopped (110 sequences sent, see §3.3). `rnsd` restarted on the laptop to check for reconnection; official MeshCore app also checked and showed only a "shakey" connection to one repeater.
- **15:17 – 15:52** — Tester continues moving and repeatedly restarts `rnsd`, trying to reconnect at several locations. Notes explicitly ask: *"Why would this work in the official meshcore client but not the rns interface?"* — the official app reported a working 3-hop path to the transfer node during this window while the RNS interface could not complete a send.
- **15:52 – 15:53** — Connection recovers ("Looks like I have a connection again. weird cause I didn't move or anything"). LXMF pings confirmed working again.

## 3. Quantitative findings

### 3.1 Stationary phase (14:00 – ~14:40)

- DIRECT sends from `a`→`b` climbed steadily from 0 to ~250 with a 0.0% failure rate for the first ~40 minutes.
- MeshCore-level DIRECT send+ACK RTT and RSSI were stable and good: `last_rssi≈-47dBm`, `last_snr≈12dB`, `noise_floor≈-118dBm`.
- RNS-level queue-to-delivered latency for the very first exchanges (before a DIRECT peer bind completed) was high — `avg=13707ms` over the first two minutes — consistent with the tester's manually-timed 10–34s meshchat delivery times, which happened before/around the peer bind completing.
- The 320×320 image transfer (14:20–14:24:50) moved roughly 8.5 KB of fragment payload in that window; the interface's own byte counters show cumulative TX growing from 8.7 KB to 14.9 KB over the same period, i.e. an effective sustained throughput on the order of 40–50 B/s net payload — expected for a LoRa-class channel with `payload_size≈88B` fragments and `fragment_delay` pacing, not a surprise regression.

### 3.2 Degradation phase (~14:40 – 15:01, while driving)

Signal quality and delivery reliability degraded together as the laptop moved away from the transfer node:

| Time | last_rssi | last_snr | recv_errors (cum.) | direct-send fail rate (cum.) |
|---|---|---|---|---|
| 14:41 | -68dBm | 12.25dB | 8 | 0.4% |
| 14:46 | -112dBm | 1.75dB | 25 | 1.2% |
| 14:51 | -106dBm | 5.0dB | 46 | 3.3% (peer hops unresolved, `b=-1`) |
| 14:56 | -109dBm | 0.0dB | 54 | 3.3% (frozen — no further direct sends attempted) |
| 15:01 | -80dBm | 11.75dB | 57 | 3.3% (frozen) |

At **14:47:30** the interface's stale-path reset logic fired: *"Peer key 7bd024b5d082… has failed 2 consecutive DIRECT send(s) on its cached path (out_path_len=1) -- resetting to flood mode."* This is the `direct_path_reset_threshold` mechanism added in alpha-0.1.1 (see §5). After the reset, path discovery for peer `b` kept re-attempting on an exponentially growing backoff (29s → 66s → 116s → 202s → 485s → 839s over six consecutive failed rounds, 14:39:57–15:00:51) and never resolved again during this drive.

### 3.3 LXMF ping test (14:29 – 15:01, 110 sequences, from testing notes)

- 60 succeeded, 50 timed out (25s timeout each).
- All of sequences 1–72 either succeeded or timed out individually; from **seq 73 through seq 110 (38 consecutive pings), every single one failed** — a hard cutoff rather than a gradual decline, and it lines up with the RSSI/SNR collapse and the `b=-1` unresolved-peer state shown in §3.2 above (last successful ping, seq 72, completed at `duration=5115ms`, immediately before the wall of failures).
- Successful ping durations ranged from ~4.8s to ~23.8s, all reporting `hops_there=3 hops_back=3` — i.e. even the "good" pings during this drive were traveling a 3-hop path, not the 1-hop DIRECT path seen in the stationary phase.

### 3.4 Disconnected phase (~15:03 – 15:52)

- The laptop's interface was restarted 9 times in this ~49-minute window (`rnsd` restarts at 15:03:36, 15:08:41, 15:18:07, 15:18:34, 15:27:37, 15:32:24, 15:43:55, 15:44:06, 15:49:09, 15:49:24, 15:52:21 — several pairs are a failed init immediately followed by a successful reconnect on the same restart cycle).
- 3 of those startups hit `Driver init returned no MeshCore instance` before the immediately-following restart succeeded (15:18:07, 15:43:55, 15:49:09).
- No DIRECT sends were attempted in this window at all (`direct sends: 0 total` for every STATS tick from 15:04 to 15:48) — the interface never had a bound peer with a resolved path to send on.
- 15 occurrences of `Mesh utilization poll returned no usable data`, spread through this window.
- On the transfer node side, the reverse path to peer `a` stayed unresolved (`out_path_len=-1`) for essentially this entire period — see §4.

### 3.4a Broadcast/announce traffic during the blackout

Since `CHANNEL` (flood) sends — including announces — don't require a resolved MeshCore path, it's worth checking separately whether they got through during the §3.4 disconnected window. They did not:

- The laptop kept transmitting broadcasts (`Routing -> CHANNEL. Reason: Mandatory broadcast packet (e.g., Announce)`) roughly every 30–90 seconds nearly continuously from 15:09:16 through 15:52:46.
- The transfer node's last *received* `CHANNEL` packet from `a` before that stretch was at **14:58:34**; its next one was at **15:52:47** — a **54-minute gap with zero broadcast packets arriving**, despite the laptop transmitting the whole time.
- In the other direction, the transfer node itself logged only two outgoing broadcasts in that same window (15:38:00, 15:52:47), and the laptop received nothing at all from `b` between 14:15 and 15:52.

This indicates the ~15:03–15:52 outage was a genuine RF/link blackout affecting all MeshCore traffic between the two nodes, not something specific to DIRECT path resolution — flood traffic that doesn't depend on a resolved path was equally unable to get through.

### 3.4b RF environment at the transfer node during the blackout

One possible explanation considered for the 54-minute blackout was receiver desensitization/overload at the transfer node from a MeshCore repeater physically too close to its radio. The transfer node's own local RF telemetry (sampled roughly once a minute throughout 14:58–15:52) doesn't support that:

- `noise_floor` stayed flat at -116 to -117dBm the entire time — no elevation.
- `rx_channel_util` was 0% for most samples, occasionally 1.7–5.0% — a quiet channel, not one saturated by a nearby transmitter's own traffic.
- `recv_errors` stayed flat at 10 for essentially the whole window (only +1 right at the very end) — no burst of decode/CRC failures, which is the signature a strong nearby interferer would usually leave.
- `last_rssi` sat steadily at -52/-53dBm — a moderate signal (presumably last heard from the 0-hop "Bh South 1" repeater), not the very strong reading (e.g. -20 to -40dBm) that a repeater sitting right next to the transfer node's antenna would typically produce.

None of this rules out a very short interference burst between samples, and there's no data here on the physical distance/placement of "Bh South 1" or the other repeaters relative to the transfer node's antenna. But on the telemetry available, the blackout looks more consistent with the laptop's own transmitted signal simply not reaching the transfer node's receiver (attenuated below the noise floor by distance/obstruction while driving) than with a receiver-overload problem local to the transfer node.

### 3.5 Recovery (15:52 – 15:55)

- 15:52:34 — peer `b` bound again on the laptop.
- 15:53:30 — one path-discovery round fails.
- 15:53:39 — path resolves (`out_path_len=0`), and by 15:54:21 the interface reports 17 direct sends with 2 failed (11.8%), RTT `avg=1718ms`.
- `last_rssi` at recovery was `-89dBm` — noticeably weaker than the original -47dBm stationary signal, yet the link recovered anyway, so the earlier long outage doesn't look purely RSSI-driven (see §6 on missing context).

## 4. Path-resolution asymmetry between the two nodes

The transfer node (`b`) log shows peer `a`'s outbound path (`out_path_len`) sitting at **-1 (unresolved) for almost the entire ~3 hour session**, including throughout the stationary phase (14:00–14:40) when the laptop's own view of the path to `b` was fully resolved (`out_path_len=1`) and DIRECT sends were succeeding at 0% failure. The transfer node's path to `a` only resolves at 15:53:41, at the very end, coinciding with the laptop-side recovery in §3.5.

This means for most of the test, DIRECT delivery only worked in the `a → b` direction from the MeshCore path-resolution point of view, even though ACKs were clearly getting back to `a` (358 ACKs received on the laptop over the session). The transfer node itself made 0 direct sends the entire session, so it's unclear from these logs alone whether its unresolved path to `a` ever actually blocked a send it needed to make, or whether it's simply an artifact of the transfer node never having a reason to originate a DIRECT send back. Flagging this as something to watch rather than a confirmed problem.

Separately, every one of the 4 times peer `a` was (re)bound on the transfer node, the same pattern appears in that log: `a` is bound as `[edge — no upstream routing]` in the same second the `RNSBIND_REQ` is received, then a few minutes later the transfer node logs a follow-up bind for `a` labeled `[router]` (14:00:23→14:25:46, 15:03:47→15:06:50, 15:18:43→15:25:46, 15:27:47→no follow-up logged before the session's next restart). Node `a` is configured `can_route=False` (edge) for this whole session, so a later bind line recording it as `[router]` looks like it may be a mislabeling in that log line rather than a real capability change on `a`. Reported here as an observation for a developer to look at directly in the code, since the report format asks for description rather than root-causing.

## 5. Correlation with the alpha-0.1.1 changelog

- **`direct_path_reset_threshold` (stale-path → flood-mode reset):** fired twice in this session (14:47:30 and 15:53:36), exactly as designed. In the first case it happened during active signal degradation (see §3.2), so unlike the changelog's cited 67%-vs-0% A/B result, this log alone can't show the reset "fixing" delivery — conditions kept degrading regardless, and the interface didn't get a bound peer again for another ~65 minutes. It's a real trigger of the new mechanism, but not, on its own, a clean before/after demonstration of it helping in the field.
- **Adaptive path-discovery backoff:** clearly visible and working as documented — six consecutive failed rounds during the drive produced the expected doubling sequence (29s→66s→839s) on both the laptop and the transfer node sides independently, each backing off against its own peer.
- **`_auto_payload_size()`:** the laptop log shows `Auto-adjusting payload_size from 80 to 88 due to node name length` firing early in most of the 9 startup cycles — consistent with the changelog's description, no anomalies observed.
- **Auto-reconnect (`auto_reconnect`, `max_reconnect_attempts`):** this session predates or doesn't clearly exercise the auto-reconnect path — the reconnects in §3.4 look like the tester manually restarting `rnsd`, not the interface's own serial/BLE reconnect logic kicking in (no `CONNECTED`/`DISCONNECTED` event log lines were observed in this log). Worth a dedicated test that unplugs/replugs the radio without a manual `rnsd` restart, to actually exercise that code path.
- **Z85 fragment encoding, split DIRECT/CHANNEL queues, priority tiers:** nothing in these logs contradicts the changelog's description; queue depth/wait `[PERF]` lines behave as expected (e.g. announce backlog draining in order during the busy CHANNEL-only startup period at 14:00–14:01).

## 6. Missing context / notes for future test sessions

- No GPS or location markers are in the raw logs — the testing notes describe the drive qualitatively ("stopped somewhere," "traveling to a new location") but there's no way to correlate a specific RSSI/SNR reading with a specific place or distance from the transfer node.
- No antenna type, orientation, or vehicle-mounting detail is recorded beyond the tester's note that positioning "may have been in less than ideal locations" while driving.
- The two log files' timestamps are not verified to be from clocks synced to each other or to the phone used for manual ping timing in the notes; cross-referencing (as done in §§3–4 above) assumes clock agreement.
- It's not possible to tell from the interface's own recv_errors counter whether errors were CRC failures, decode failures, or something else — the counter is just a firmware-reported aggregate.
- The testing notes end mid-session ("LXMF Pings now working") without a formal stop time or closing summary; the raw logs continue somewhat longer (laptop to 15:55:16, transfer node to 16:04:28) with no further narrated context for that tail.
- The tester's own question in the notes — why the official MeshCore app showed a working 3-hop path to the transfer node ("desktop radio") around 15:32–15:43 when the RNS interface could not — has a well-supported (though not fully proven) explanation once the radio is confirmed to be the same physical device in the same position both times (the tester confirmed the radio was never moved or reconnected; `rnsd` on the laptop was simply closed so the MeshCore app could run against the same USB-attached Heltec V3, then closed again to restart `rnsd`). During the `rnsd` session immediately before that check (15:32:24–15:43:36, ended by a manual `^C`), the laptop logged **zero** peer binds and **zero** path-discovery attempts for peer `b` — the interface never got far enough to even know `b` was reachable, because peer discovery in this interface (`RNSBIND_REQ`/`RNSBIND`, documented in [`Interface/MeshCore_Dynamic_Interface.py:88-102`](../../Interface/MeshCore_Dynamic_Interface.py#L88-L102)) works by broadcasting a request on the shared MeshCore channel and waiting for another node to *directly hear it* and broadcast a reply back — a single-shot flood exchange with no hop-by-hop relay of its own. That's consistent with the 54-minute CHANNEL blackout already established in §3.4a. The official app's "3 hops message sent through path to desktop radio" is most likely MeshCore's own native contact-to-contact routed messaging instead — a message addressed to an already-known contact, relayed hop-by-hop through repeaters that have that contact's route cached, where each individual hop only needs adequate signal at the moment it relays, rather than needing to be heard in one flood transmission end-to-end. That difference in delivery mechanism (single-shot flood broadcast vs. hop-by-hop routed relay) would explain the app succeeding while this interface's own discovery/bind traffic could not, on the identical radio, in the identical conditions. This is a plausible mechanism grounded in the interface's own documented design, not a confirmed root cause — it would take a look at MeshCore's firmware-level channel-flood-vs-routed-relay behavior, or app-side logs from the same moment, to fully confirm.
- Several `[Warning]`/`[Error]` lines in the laptop log (`AutoInterface`/`TCPInterface` carrier-loss and "Network is unreachable" entries around 14:09 and repeatedly from 15:03–15:52) are from Reticulum's own local network interfaces (Wi-Fi/home TCP), not the MeshCore interface, and line up with the tester driving away from home Wi-Fi — included here only so a future reader doesn't mistake them for MeshCore-layer errors.

## 7. Suggestions for future test sessions

- Log GPS coordinates or at least distance-from-transfer-node estimates alongside the manual testing notes, so RSSI/SNR/failure-rate data can be tied to physical distance rather than just narrated locations.
- Record whether `rnsd` was restarted manually vs. left running, with a timestamp for each, directly in the notes file (this session's restarts had to be inferred from the raw log rather than the notes).
- Add a deliberate radio unplug/replug test to exercise the `auto_reconnect` code path specifically, since this session's reconnects all appear to be manual `rnsd` restarts.
- Run the official MeshCore app and the RNS interface side-by-side with their own timestamped logs/screenshots during a known-marginal-signal window, to directly compare path resolution behavior between the two (the tester's own open question in §2).
- Consider logging which node initiated the DIRECT send whenever `out_path_len` is reported, to make the kind of asymmetry noted in §4 easier to spot without cross-referencing two files by hand.

# Field Test Report — alpha-0.1-snapshot2

**Source logs:** `fieldtests/raw/alpha-0.1-snapshot2/`
(`alpha1.0snap2testingnoteslaptop.txt`, `transfernodelogs.txt`, `Notes.txt`)

**Build under test:** matches `changelog.md` `[alpha-0.1-snapshot2] - 2026-09-12` (log timestamps are 2026-09-13, one day after that changelog entry; the log's feature set — per-attempt stale-path reset inside `_send_direct_with_retry`, `direct_ack_timeout_routed_max`, telemetry-permission auto-grant, `get_stats_core()`'s `tx_queue_len`/`battery_mv`, `routing_health`/`outgoing_packet_type_counts` in `[STATS]` — all correspond to that release). This is the first field test of this snapshot; the changelog itself notes it was "not yet field-tested" at release time.

## 1. Test setup

Two nodes running the MeshCore Dynamic Interface under `rnsd`, on MeshCore channel `RNSTunnel` (idx 35):

| Node | Role (per `Notes.txt`) | RNS peer name(s) seen | MeshCore identity | Session window |
|---|---|---|---|---|
| `a` ("laptop") | Reticulum endpoint | `a` | `343377c464a79a48…`, `[router]`, `can_route=True` | 10:57:14–11:07:37, then 11:10:29–11:20:17 (two sessions; restarted once) |
| `afipc`/`b` ("desktop"/transfer node) | Reticulum router | `afipc` (fresh this session) and `b` (a stale cached name resolving to the same MeshCore pubkey) | `7bd024b5d082747e…`, `[router]`, `can_route=True` | 10:36:08 onward, single continuous session, log excerpt runs through 11:27:10 |

`Notes.txt` for this snapshot is a single line: *"Essentially just made sure I was in a spot with good connectivity on my laptop (a) and tested connecting to my desktop."* There is no journaled timeline of tester actions/locations this round (contrast with snapshot1's `testingnotes.txt`) — the laptop's "testing notes" file for this snapshot is in fact its raw `rnsd` log, not a separate narrative. Both nodes appear to have been stationary for this test based on the note.

## 2. The `a`↔`afipc` fragment-reassembly asymmetry (headline finding)

This directly reproduces the question asked about this snapshot: the transfer node's own MeshCore radio counters and its own log both show substantial received/sent activity, while the laptop's log is dominated by `Dropping incomplete reassembly` lines. Counting actual RNS-packet-level outcomes (not just raw radio byte/frame counters) on each side gives a clear, large, and consistent asymmetry:

| Direction | Full reassemblies logged | Dropped (incomplete after 300s) | Success rate (of attempts that got ≥1 fragment) |
|---|---|---|---|
| `a` → `afipc` (laptop's traffic, as seen by the transfer node) | 55 | 5 | **~92%** |
| `afipc` → `a` (transfer node's traffic, as seen by the laptop) | 1 | 31 | **~3%** |

(The 1 laptop-side success was a 2-fragment, 83-byte `LINK_REQUEST` at 11:00:17. Every dropped packet on the laptop side is logged with a partial fragment count, e.g. `only 1/4 fragment(s) arrived`, `only 2/3 fragment(s) arrived` — never 0, meaning the laptop's radio genuinely heard *something* of nearly every one of these packets, just never the complete set. Packets for which the laptop never heard even one fragment leave no reassembly-buffer log line at all, so the true failure count in this direction could be higher than 31.)

### A likely contributing factor: traffic composition differed sharply between directions

The two nodes were not sending the same *kind* of traffic, and that difference alone would produce an asymmetry like this even if the underlying per-fragment RF loss rate were identical in both directions:

- **`a`'s outgoing traffic was overwhelmingly single-fragment.** The laptop log shows a near-continuous stream of `OUT size=51 ptype=0 broadcast=True` sends, roughly one every 4–20 seconds for most of both sessions, each logged as `Routing -> CHANNEL. Reason: Mandatory broadcast packet (e.g., Announce)`. Per the interface's own header-parsing logic (`ptype=0` is `DATA`; the "mandatory broadcast" reason covers both `ANNOUNCE` and `DATA`+`PLAIN` path requests), these 51-byte sends are almost certainly `PATH_REQUEST` broadcasts, not `a`'s own periodic self-announce (`a`'s actual `ANNOUNCE` packets are the larger 192–239-byte, multi-fragment sends, of which there were only ~5–8 across the session). A 1-fragment send has no way to arrive "incomplete."
- **`afipc`'s outgoing traffic was overwhelmingly multi-fragment.** Its own `[STATS] outgoing packet-type mix` climbed to `LINK_REQ=6, DATA=3, ANNOUNCE=52` by the end of the session — 52 `ANNOUNCE` sends (192–239 bytes → 3–4 fragments each) plus 6 `LINK_REQUEST`s (83 bytes → 2 fragments), against only 3 single-fragment `DATA` sends. Since one lost fragment drops the whole packet, a 4-fragment send fails far more often than a 1-fragment send even at an identical per-fragment loss probability — this composition difference alone would bias the observed success rate heavily against `afipc`'s traffic.
- The 4 distinct `Linked RNS token … -> 'a'` events on the transfer node (10:57:58, 10:58:21, 11:01:16, 11:06:16) indicate the tester's "tested connecting to my desktop" was several separate Link-establishment attempts (consistent with a client retrying after a prior attempt stalled), each one contributing another `LINK_REQUEST`/`ANNOUNCE`-adjacent burst of multi-fragment traffic that then had to survive the same asymmetric link to complete.

### A second, independent contributing factor: RF conditions on the laptop's receive path looked more volatile

Comparing the two sides' own local RF telemetry during the ~10:57–11:15 window (before the total blackout in §3 below):

- `afipc`'s `last_rssi` while it was hearing `a` was consistently weak but narrow-band: `-118, -118, -112, -113, -113`dBm across five consecutive DIRECT-send-failure log lines (10:58:42–11:00:41).
- `a`'s `last_rssi` while hearing `afipc` swung far more widely over the same general period: `-86, -119, -84, -87, -84, -85, -80, -83, -122`dBm across consecutive one-minute `[STATS]` samples (10:59:16–11:07:16) — a 30–40dB range from one minute to the next, with `last_snr` correspondingly swinging from ~+12dB to -5.25dB.

A consistently-weak-but-stable link (transfer node hearing the laptop) surviving mostly intact, versus a wildly-fluctuating one (laptop hearing the transfer node) failing almost completely, is consistent with genuine link-quality asymmetry compounding the fragment-count asymmetry above, though the two nodes' hardware/antenna setup isn't described in the notes, so this can't be fully separated from a possible physical-siting difference (see §5).

## 3. Total channel blackout from ~11:15 onward

Independent of the directional asymmetry in §2, both nodes' local MeshCore radio counters go flat at almost the same wall-clock moment, indicating a period where *neither* side received anything on the channel at all — not specific to `a`↔`afipc` traffic:

- **Laptop:** `recv` frozen at 181 and `recv_errors` frozen at 32 from the 11:15:31 `[STATS]` tick through the last one before disconnect (11:19:32); `last_rssi` reads exactly `-96dBm` for four consecutive one-minute samples (11:15:31–11:19:32) — the kind of value you'd expect from a firmware counter that simply isn't being updated because nothing new is arriving to update it, not four independent measurements happening to agree. The interface itself declared the MeshCore link lost at 11:20:17 (`reason=reconnect_failed, reconnect attempts exhausted`), after which sends were skipped entirely.
- **Transfer node:** `recv` frozen at 27562, `sent` at 26086, and `last_rssi` at `-119dBm` from the 11:15:10 `[STATS]` tick all the way through 11:27:10 (the last STATS line in the available log excerpt, ~12 minutes later) — `rx_channel_util` reads `0.0%` for this entire stretch, i.e., not just "no traffic from `a`" but "no traffic at all observed on the channel."
- The last successful reassembly in either direction (§2's `RX -> CHANNEL from 'a'` list) is at 11:15:01 on the transfer node side — nothing after that point in either log.

This reads as a genuine, roughly-simultaneous RF blackout affecting the whole channel for both nodes, separate in character from the §2 asymmetry (which was present and measurable during the ~18 minutes *before* this point, while both sides were still receiving something). No cause for the blackout itself is visible in either log (no `[Warning]`/`[Error]` MeshCore-layer lines, no radio-setting change logged).

## 4. DIRECT-path reliability and path discovery

- **`afipc` → `a` (the direction with more DIRECT attempts logged):** ended the session at `8 total, 4 failed (50.0%)`, `MeshCore-level DIRECT send+ACK RTT (n=4): avg=3765ms`. This wasn't a steady 50% — it alternated in a tight burst between 10:58:42 and 11:00:54: reset-to-flood at 10:58:42, ACK received 10:58:47, reset-to-flood 10:59:04, ACK received 11:00:04, reset-to-flood 10:59:54 (sic, interleaved), ACK received 11:00:19, reset-to-flood 11:00:16, ACK received 11:00:54, reset-to-flood 11:00:41 — five `_maybe_reset_stale_path` triggers and four successful ACKs inside about two minutes, i.e. the path to `a` was being torn down and rediscovered repeatedly in quick succession rather than settling.
- **`a` → `afipc`:** only `2 total, 1 failed (50.0%)` logged on the laptop side, with one `_maybe_reset_stale_path` trigger at 11:01:03 (RSSI -119dBm at the time, `out_path_len=3`) and one ACK received at 11:01:29 (RTT 5546ms) after falling back to flood mode.
- **Dedicated path discovery to `a` consistently failed to get a response** on the transfer node's side — five separate `PATH DISCOVERY AFTER (attempt N/3): no response within timeout` lines (10:59:12, 10:59:17, 10:59:48, 10:59:53, 10:59:58), and `consecutive path-discovery failures: 343377c464a7...=1` stayed pinned at 1 in every `[STATS]` line from 11:06:09 to the end of the log — yet DIRECT sends to the same peer *did* intermittently succeed via paths learned through ordinary contact-table updates (`Path for 343377c464a79a48... changed via EventType.CONTACTS: ... -> out_path_len=N`, e.g. at 10:58:47, 10:59:15, 11:00:04, 11:00:54). This is the same pattern noted from the other direction in the snapshot1 report (§6 there): the interface's own dedicated path-discovery command getting no answer while a route is still resolvable some other way. Telemetry-permission auto-grant (new in this snapshot, meant to fix exactly this class of problem — see `changelog.md`) was apparently in effect on both sides (`telemetry_mode_base set to ALLOW_FLAGS` appears in both startup logs, and both sides completed a confirmed RNSBIND exchange), so this doesn't look like the same root cause snapshot1/this changelog entry targeted; it's flagged here as a fresh observation rather than a diagnosed regression.
- A minor logging oddity: the laptop's peer table shows **both** `b` and `afipc` bound to the same MeshCore pubkey `7bd024b5d082747e...` — `b` was restored from the persisted peer-binding cache (a name from an earlier session), `afipc` was learned fresh via this session's RNSBIND exchange. Both names resolve to the same physical node, so this isn't a functional problem, but two live entries for one peer is worth being aware of if a future session sees them diverge (e.g. different `out_path_len`) instead of tracking in lockstep.

## 5. Missing context / notes for future test sessions

- No journaled timeline for this snapshot — `Notes.txt` is a single sentence, so unlike snapshot1 there's no way to correlate a specific log event with what the tester was doing/observing at that moment (e.g., which of the 4 `Linked RNS token` events, if any, the tester saw succeed or fail in their client UI, or whether they noticed the ~11:15 blackout happening live).
- Neither log's "Interface ready" line prints the configured `mode` (`access_point`/`boundary`/`gateway`) for either node, and the transfer node's log excerpt doesn't show any other interfaces it might have (a backbone/TCP link to the wider Reticulum network, as the term "transfer node" implies from snapshot1). This matters for interpreting the 52 `ANNOUNCE` sends from `afipc`: that could be its own ordinary self-announce cadence, or proactive re-announcing of routes learned from a connected backbone (which the README's own "Interface mode" section warns can flood a shared LoRa channel if `gateway` mode is used on a backbone-connected node). The logs alone can't distinguish these.
- No antenna/hardware/placement details are recorded for either node in this snapshot (contrast with snapshot1's driving narrative) — the RSSI-volatility difference noted in §2 is described only as an observation, not attributed to a specific physical cause.
- `recv`/`sent`/`recv_errors` on the MeshCore radio are cumulative firmware counters, not reset per `rnsd` session — the transfer node's `recv=27298` already at its first `[STATS]` tick (one minute after its own connect) reflects the device's uptime since its last reboot, not activity from this test. Any comparison of raw `recv` magnitude between the two nodes (e.g. "the transfer node received far more") should be read as reflecting each device's own uptime/general channel exposure, not necessarily traffic volume from the other node specifically — the packet-reassembly counts in §2 are the more reliable signal for that.
- The cause of the ~11:15 whole-channel blackout (§3) has no supporting telemetry beyond "both sides' counters stopped moving at once" — no external interference source, weather, or physical event is recorded.

## 6. Suggestions for future test sessions

- Keep a timestamped narrative log for every session, even a stationary one — this snapshot's near-total absence of one made it impossible to connect specific fragment losses to anything the tester was doing or seeing at the time.
- Record each node's `mode` setting and full interface list (not just the MeshCore one) in the notes, since that's currently unrecoverable from the logs and matters for interpreting announce volume.
- Since the core finding this round is a *directional* asymmetry, a future test could deliberately send comparable-sized/comparable-fragment-count payloads in both directions (rather than relying on whatever mix of announces/path-requests/link-attempts happens organically) to isolate whether the asymmetry is really about fragment count or about the link itself.
- Log or note antenna type/placement for both nodes, to help separate "this node's RF path is worse" from "multi-fragment sends are just structurally more fragile" as explanations for an asymmetry like this one.
- If possible, capture a few seconds of finer-grained (sub-second) timing around a multi-fragment send/receive to check whether a receiving node's own transmissions (half-duplex) are colliding with incoming fragments of the other side's packet — the per-minute `[STATS]` granularity here can't confirm or rule this out as a contributor to the fragment loss in §2.

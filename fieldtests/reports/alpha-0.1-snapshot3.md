# Field Test Report — alpha-0.1-snapshot3

**Source logs:** `fieldtests/raw/alpha-0.1-snapshot3/` (`laptop_a.txt`, `afipc.txt`, `Notes.txt`). `afipc` was driven by a separate Claude session on a different machine, coordinating with the laptop's session live via cross-session messaging; `afipc.txt` is a filtered excerpt (STATS/QUEUE/DEQUEUE lines and most per-minute RNSBIND heartbeat/response lines stripped, per that session's own description) pasted via chat, not a raw file this report's author read directly off `afipc`'s filesystem. See the note at the top of `afipc.txt` and §5.

**Build under test:** commit `8229153` ("Enhance proof packet routing and apply forced path for known contacts"), the tip of branch `Experimental` at the time of testing, still under `[Unreleased]` in `changelog.md` (no snapshot3 version tag exists there as of this report). The deployed interface copy on the laptop (`~/.reticulum/interfaces/MeshCore_Dynamic_Interface.py`) was confirmed byte-identical to the repo copy at session start.

## 1. Test setup

| Node | Role | RNS/MeshCore identity | Channel | Session driver |
|---|---|---|---|---|
| `a` ("laptop") | Reticulum endpoint, `mode=gateway`, `can_route=false` | MeshCore key `343377c464a79a48…` | idx 35 "RNSTunnel" | This Claude session, via Remote Control |
| `afipc` | Reticulum endpoint, `mode=access_point`, `can_route=yes` (per earlier-session config, not re-confirmed this snapshot) | MeshCore key `7bd024b5d082747e…` | idx 35 "RNSTunnel" | A separate Claude session on a different machine, coordinating live via cross-session messaging |

Unlike snapshot1/snapshot2 (both stationary, close-range), this snapshot's laptop was physically relocated mid-session: it started in direct/close range of `afipc`, then the user drove it into a car roughly 2-3 MeshCore repeater hops from `afipc` (at the user's home), with terrain blocking any direct RF path between the two. This is the first field test in this project's history to exercise real multi-hop relay over genuine, uncontrolled public repeater infrastructure rather than a direct or single-controlled-repeater link. Repeaters observed in the laptop's MeshCore contact list at the relocated position: `WRR Broken Hill` (confirmed by the user to be a regular MeshCore repeater, not another instance of this project's interface), `RH Broken Hill`, `Broken Hill 2`, `Bh South 1` (the last reported by `afipc`'s session as 0 hops from `afipc` directly).

## 2. Close-range sanity checks (before relocation)

Before the laptop moved, basic interface functionality was reconfirmed at close range:

- RNSBIND peer-discovery handshake completed cleanly in both directions (laptop → `afipc` request at startup, response ~15-20s later per the randomized backoff).
- Persistent peer-binding restore across a laptop `rnsd` restart worked as designed: on restart, both `b` (a cached name from a prior session, same MeshCore pubkey) and `afipc` were restored from the peer-binding cache and confirmed still present in the MeshCore device's own contact table, without needing a fresh RNSBIND exchange.
- `rnstatus` reported the interface `Up` throughout, with no unexpected `MeshCore connection lost` events at any point in the session (checked explicitly via log grep after a later hypothesis raised the possibility).

## 3. An intermittent "no send-side log for a dispatched packet" symptom

At several points in this snapshot, `RNS.Transport`'s own `[Pathing]` log lines showed it deciding to send or rebroadcast a packet (e.g. `Forwarding path request from local client for <hash> ... to all other interfaces`, or `Rebroadcasting announce for <hash> with hop count 0`), but the interface's own `[PERF N] OUT ...` log line — which fires unconditionally at the very start of `processOutgoing()`, before any rate-limiting or routing decision — never appeared for that specific packet. Observed instances: 16:46:36, 16:48:57 (laptop time, both before relocation), and 17:25:49 and 17:37:17 (after relocation).

This was investigated twice with temporary diagnostic logging added to the *deployed* copy of the interface only (never committed to the repo; reverted both times before the session's later restarts). In both instrumented windows — one covering three close-spaced attempts (16:53:52, 16:54:16, 16:54:48), the other covering a single retried exchange with `afipc` at 17:39:57-58 — the interface's `process_outgoing`/`processOutgoing` entry points fired every single time, logged `online=True OUT=True`, and completed their send (fragmentation, queueing, dequeueing) without error. So this symptom did not reproduce under direct instrumentation in either window tested, despite reproducing outside those windows. **This is an open, unexplained, genuinely intermittent gap as of this snapshot** — not resolved, not root-caused, and the two instrumented windows may simply not have been unlucky enough to catch it. See §5 for what would help narrow it down further.

**This symptom was only ever observed on the laptop side.** `afipc.txt` shows six outgoing sends across the session (`[PERF 0]` through `[PERF 5]`, at 16:23:02, 16:43:42, 16:44:06, 16:53:55, 17:12:11, and 17:37:13) — every single one has a corresponding `[PERF N] OUT ...` / `Routing -> ...` log line, with no gaps. Whether this reflects a real asymmetry (something laptop-specific — its `rnsd` version, 1.4.2 vs `afipc`'s 1.5.2, its `mode=gateway`/`can_route=false` config vs `afipc`'s `mode=access_point`/`can_route=true`, or something else) or is just a smaller sample not happening to catch it on `afipc`'s side (6 sends vs many more on the laptop) can't be determined from this snapshot alone.

## 4. Multi-hop relay result (the headline finding)

After relocation, the laptop ran `testscripts/relay_delivery_test.py` (sender) against `afipc`'s responder repeatedly. Pattern across the attempts checked against `afipc`'s own log:

| Attempt (laptop time) | Laptop's request reached `afipc`? | `afipc` generated a response? | Response reached the laptop? |
|---|---|---|---|
| ~17:12 (first post-relocation attempt) | Yes (per `afipc`'s log) | Yes, full 3-fragment ANNOUNCE sent | No — sender script timed out |
| 17:21:37 | Yes | No response logged on `afipc`'s side at all | N/A |
| 17:25:49 | `afipc` reported nothing arrived (RX counters flat) | N/A | N/A |
| 17:37:13 (reverse-direction test, `afipc`→laptop's own fresh responder) | Laptop reported nothing arrived from `afipc` | N/A (this leg tests `afipc`'s *request*, not response) | N/A |
| 17:39:57-58 → 17:40:06 (reverse-direction retry, instrumented) | — | Laptop's interface sent a clean 3-fragment ANNOUNCE (confirmed via diagnostic: `online=True OUT=True`, all 3 fragments queued/dequeued 17:39:58-17:40:00) | **Yes** — `afipc`'s log showed `RX -> CHANNEL from 'a'. Reassembled 183b ANNOUNCE packet.` at 17:40:06, ~6s after the laptop's first fragment, with a couple of duplicate-fragment lines noted beforehand (consistent with repeater retransmission) |

The 17:40:06 result is the first confirmed successful delivery of a multi-fragment RNS packet across this multi-hop link in this session. Despite that, `afipc`'s own `relay_delivery_test.py sender` (run concurrently, targeting the laptop's responder) still reported "Path request timed out" — `afipc`'s session initially hypothesized its sender's path-resolution timeout (reported as roughly 12-20s) was simply shorter than the actual delivery latency. **This hypothesis was tested and did not hold**: `afipc` patched a `--timeout` override into its local copy of `relay_delivery_test.py` (not merged into this repo as of this report — see §5) and retried with a 90-second window against the laptop's responder; still no response arrived within that window. So the failure pattern in the table above is not simply "the test gives up too early" — genuine intermittency (packets sometimes arriving in ~6s, sometimes not arriving at all within 90s) is the more accurate description as of this snapshot.

Independent of this project's interface, two other checks at this same relocated position both succeeded reliably:

- The user tested with the official MeshCore companion app directly (not this interface) and reported hearing 3 repeater relays and most messages getting through.
- `testscripts/path_discovery_diag.py` (a standalone script that talks to the local MeshCore device directly, bypassing RNS/this interface entirely) was run against `afipc`'s MeshCore contact: **3 attempts, 3 successes**, each resolving in ~5 seconds to a 1-hop MeshCore-level path (`out_path: '19'`). Local RF snapshot at the time: `noise_floor: -115, last_rssi: -113, last_snr: 2.25`.

Taken together, these two results indicate the repeater/RF path itself was reliable and low-latency at this position; the inconsistent results in the table above are specific to this project's RNS-level channel-broadcast traffic (RNSBIND, path requests, and especially the 3-fragment ANNOUNCE), not the underlying mesh.

## 5. Missing context / notes for future test sessions

- `afipc.txt` is a filtered/trimmed excerpt pasted via chat (see the note at its top), not a raw file this report's author read directly off `afipc`'s filesystem — the full, unfiltered log was offered but not obtained for this snapshot.
- The exact physical distance/repeater count between the laptop and `afipc` after relocation is not precisely known — the user described it as "a couple km" and "multiple repeater hops," and MeshCore's own path-discovery resolved a 1-hop path, but the true repeater count in the chain wasn't independently verified (MeshCore only reports the immediate next hop in `out_path`, not the full chain length).
- The §3 intermittent no-send symptom remains unexplained, and now has a further open question: it was only observed on the laptop side (never on `afipc`'s, across a smaller 6-send sample there — see §3). A future session could leave verbose+diagnostic-level logging on continuously (rather than added/removed around specific tests) to catch a live instance with full context on both sides symmetrically, or add logging one level up (in `RNS.Transport.transmit`, or wherever calls `interface.process_outgoing`) to see whether the call is even being made when the symptom occurs, versus made but silently absorbed inside the interface.
- `afipc` is running `rnsd` 1.5.2; the laptop is running 1.4.2, which per `testscripts/relay_delivery_test.py`'s own docstring lacks `RNS.Reticulum.get_medium_path_timeout()` and falls back to a fixed 60s wait. Whether this version difference (or the `mode=gateway`/`can_route=false` vs `mode=access_point`/`can_route=true` config difference noted in §1/§3) contributes to anything observed in this report is not established, just noted as a difference between the two nodes.
- No fragment-level RSSI/SNR-per-repeater-hop data is available for the 17:40:06 successful delivery — only that it took ~6 seconds end-to-end and involved at least one observed duplicate fragment (consistent with, but not proof of, more than one repeater relaying it).
- Per `changelog.md`, this snapshot's build includes the `[Unreleased]` "Architectural hardening" entry (single validation chokepoint `_check_event`, self-verifying command serializer install, design invariants documented in the module docstring) on top of the already-released `8229153` proof-routing/forced-path fixes. Whether any of that hardening interacts with the §3 symptom is speculation — nothing in this session's logs points at it directly, it's simply the most recent code change preceding this test.
- `afipc`'s session locally patched a `--timeout` override into its copy of `relay_delivery_test.py` sender (to test the 90s-wait hypothesis in §4) and committed it locally, but had not pushed it as of this report — so it isn't reflected in this repo's copy of the script yet. This is a tooling change, not a decision about the interface itself.

## 6. Suggestions for future test sessions

- If another two-machine, two-Claude-session field test happens, arrange for both sides to export/share their actual full (unfiltered) raw log files before the session ends, so both device logs can go in `fieldtests/raw/` as complete files per this directory's stated convention.
- The 90s-timeout retry in §4 ruled out "timeout too short" as a full explanation, but a longer window still (e.g. several minutes) hasn't been tried — worth doing before concluding intermittency is unrelated to timing entirely.
- Since `testscripts/path_discovery_diag.py` was a clean, decisive, RNS-independent way to separate "mesh problem" from "interface problem" this session, consider running it routinely alongside `relay_delivery_test.py` in future multi-hop tests as a standard baseline.
- If the §3 symptom recurs, capturing the exact wall-clock gap between consecutive attempts might help — the instrumented windows in this session were attempts spaced closely together in time (30s and ~4min apart) and both happened to work; whether spacing, mesh load, or something else correlates with the symptom is currently unknown. Also worth running the same instrumentation on `afipc`'s side simultaneously, given §3's laptop-only observation.
- A dedicated fragment-count experiment (as suggested in the snapshot2 report already) would help separate "multi-hop is unreliable generally" from "multi-fragment specifically is the weak point" — send comparable single-fragment and multi-fragment payloads back-to-back at the same multi-hop position and compare outcomes directly, rather than relying on whatever mix of announces/requests happens organically.

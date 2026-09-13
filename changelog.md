# Changelog

All notable changes in this repository compared to the original upstream
project it was forked from (preserved at
[`referenceprojects/MeshCore_Dynamic_Interface_original_repo.py`](referenceprojects/MeshCore_Dynamic_Interface_original_repo.py))
are documented here.

## [Unreleased]

Architectural hardening following the two bugs fixed just above (the
concurrent-command race and the CHANNEL-send silent failure): rather than
leaving those as one-off patches, this closes the general patterns behind
them so a similar bug is harder to reintroduce by accident.

### Added

- **`_check_event`**: a single chokepoint for validating a radio command's
  reply, used by both the DIRECT and CHANNEL send paths in place of their
  previous separate, ad hoc checks. Raises on `EventType.ERROR`, no
  response, or (when given an `expected_type`) the wrong reply type.
  Documented in the module docstring as design invariant #1: any new
  command call site that cares about success should route through this
  rather than trusting a bare `await` to mean the send worked.
- **`_install_command_serializer` now verifies its own install** and
  fails closed: it returns `False` (instead of assuming success) if
  `_mc.commands.send` doesn't have the expected shape to wrap, or if the
  wrapper assignment doesn't actually stick, and `_async_setup` refuses
  to bring the interface online at all in that case rather than starting
  up silently unprotected against the reply-crosstalk bug this closes.
- **Design invariants section** in the module docstring: the two rules
  above, plus a third -- every `self.X = cfg.get("X", ...)` must be read
  somewhere in the file, with two explicitly-labeled, narrow exceptions
  (an attribute read externally via the `RNS.Interfaces.Interface`
  base-class contract, like `bitrate`; or a genuine temporary gap, like
  `firmware_text_limit` while `_auto_payload_size` is hardcoded for an
  in-progress test) -- written down so a future change doesn't have to
  re-derive these from field-test forensics again.
- **`tests/`**: an offline regression suite (`python3 -m unittest
  discover -s tests`, no radio hardware) built against fakes of the
  `meshcore` library's command/event shape. Covers: the DIRECT ACK-timing
  and path-reset bugs fixed above (late ACKs, truncated-wait attempts not
  counting toward a reset, wrong reply type); the CHANNEL silent-failure
  bug (`_check_event` actually raising, and the failure reaching
  `_handle_send_failure`'s log); the command serializer (no two sends'
  critical sections ever overlap, install fails closed on a malformed
  `_mc`); and a static AST scan (`test_config_usage.py`) that would have
  caught the `firmware_text_limit` dead-config bug the day it was
  introduced -- it already caught one previously-unnoticed instance
  (`bitrate`, confirmed to be a false positive: consumed externally by
  RNS core, not actually dead) while being written.

**Testing overrides**, motivated by two confounds identified in
[`fieldtests/reports/alpha-0.1-snapshot2.md`](fieldtests/reports/alpha-0.1-snapshot2.md):
path-discovery flakiness and automatic reset-to-flood made it hard to
tell, from field logs alone, whether a specific repeater hop itself was
delivering traffic reliably, and there was no way to confirm a received
CHANNEL message had actually been relayed rather than heard directly.
Neither knob is meant for a real deployment; both log loudly
(`RNS.LOG_WARNING`) at startup when active, and reject invalid config
with a clear reason rather than silently doing nothing.

- **`force_direct_path_peer` / `force_direct_path`**: pins one peer's
  MeshCore `out_path` to a manually-specified repeater route (validated
  hex path + hash mode, matching the on-wire format `change_contact_path`
  already uses) as soon as that peer is bound, via the same persistence
  mechanism `discover_path()` uses for a normally-discovered route.
  `discover_path()` and `_maybe_reset_stale_path()` both refuse to touch
  a pinned peer for the rest of the session (`_is_forced_peer`), so a
  test against one specific hop can't have its route silently
  rediscovered or reset out from under it.
- **`channel_relay_only`**: drops any received CHANNEL message whose
  firmware-reported `path_len` shows no repeater has touched it yet (0
  hops, or the library's 255 "not a flood packet" sentinel) -- including
  `RNSBIND`/`RNSBIND_REQ` peer-discovery traffic -- so a test session
  only "sees" genuinely multi-hop deliveries. `path_len` was already
  arriving in every `CHANNEL_MSG_RECV` payload (verified in the
  `meshcore` library's `reader.py`) but nothing in this interface read it
  before now.
- `tests/test_testing_overrides.py`: config validation (valid/invalid
  hex, mode, hop-count and byte-length limits), `_is_forced_peer` prefix
  matching, `discover_path`/`_maybe_reset_stale_path` both skipping a
  pinned peer, and `_on_channel_msg` dropping/passing CHANNEL traffic
  correctly based on `path_len` and the `channel_relay_only` flag.

**`processOutgoing()` call-outcome accounting**, motivated by a "missing
send" symptom hit during the same live field test session as the
`force_direct_path`/PROOF-routing fixes above: RNS core appeared, from
its own logging, to have decided to send or rebroadcast a packet, but
nothing in this interface's logs showed any trace of that packet ever
arriving at `processOutgoing()`. A temporary diagnostic added live during
the session narrowed it down enough to keep testing that day, but wasn't
enough to root-cause it on the spot, and got reverted rather than kept
as one-off debug noise. This is the permanent replacement: every
`processOutgoing()` call is now tallied into exactly one outcome bucket
(`dropped_offline`, `dropped_rate_limited`, `queued`, or `exception`)
against a `handed_to_interface` count incremented before any check can
short-circuit the call, all surfaced in the existing `[STATS]` line
(`_stats_summary_loop`) behind `debug_logs` like the rest of that
summary.

- **`_SessionStats.record_outgoing_call_outcome` / `outgoing_call_outcome_counts`**:
  same dict-of-counters pattern as the existing
  `record_outgoing_ptype`/`outgoing_packet_type_counts`, keyed by outcome
  name instead of RNS packet type.
- **`processOutgoing`/`process_outgoing`**: records `handed_to_interface`
  as its first statement, then exactly one of `dropped_offline` (the
  `online` check), `dropped_rate_limited` (either
  `_rate_limit_announce` or `_rate_limit_path_request`), `queued` (a
  packet reached `_enqueue_fragments`), or `exception` (the whole body is
  now wrapped in a `try`/`except` that logs the exception and traceback
  via `RNS.log(..., RNS.LOG_ERROR)` -- matching the existing
  `_async_setup` exception-logging convention -- then re-raises, so
  behavior toward RNS core is unchanged and only the accounting/logging
  is new).
- **`_stats_summary_loop`**: a new `[STATS] processOutgoing() call
  outcomes:` line reporting all four buckets plus a computed
  `unaccounted` residual (`handed_to_interface` minus the sum of the
  other four). `unaccounted` is the actionable signal this was built
  for: nonzero means a call fell through this accounting itself (a bug
  in this new code, not the original symptom); `handed_to_interface`
  staying flat across an interval where RNS core is otherwise known to
  be emitting packets instead points to a gap upstream of this
  interface (RNS core or the interface alias), which this counter can
  now rule in or out for the first time.
- `tests/test_outgoing_call_outcomes.py`: each outcome bucket firing
  exactly once for its triggering condition (offline, each of the two
  rate limiters, a normal send, an exception raised partway through),
  counts accumulating correctly across repeated calls with different
  outcomes, an exception still propagating to the caller after being
  counted, and the underlying `_SessionStats` methods in isolation.

**Multi-hop CHANNEL fragment reliability**, motivated by the same live
field test session: over a genuine multi-hop repeater chain (confirmed
via `channel_relay_only` and a TX-power sweep to rule out direct-range
confounds), CHANNEL delivery was bimodal -- a full, fast success or
nothing at all within 90s, with no clean partial-progress pattern in
between.

- **`fragment_delay` default raised `1.0s` → `2.5s`** (back towards the
  pre-fork value it was lowered from -- see the
  `alpha-0.1-snapshot1`-era "Configuration defaults" entry below, which
  already flagged this as "intended to be revisited towards a more
  mesh-considerate value once the build is stable"). A repeater has one
  radio: it cannot receive fragment N+1 while still relaying fragment N,
  and the firmware's own per-hop scheduling adds further delay on top of
  that. At `1.0s`, a multi-fragment packet's later fragments were likely
  being re-flooded into the mesh (and potentially colliding with an
  in-progress relay at a repeater) before an earlier fragment had
  finished propagating across every hop -- consistent with the bimodal
  pattern observed. This can't be scaled automatically per hop count the
  way `direct_ack_timeout_routed_max` is for `DIRECT` sends: `CHANNEL` is
  a flood broadcast with no fixed route, so there's no single hop count
  to scale against. It stays a flat, manually-tuned value -- lower it for
  a known single-hop/no-repeater deployment where the extra margin only
  costs latency.
- **`path_response_retransmit_extra`** (new, default `1`): a path-response
  ANNOUNCE (sent because a path request for this destination just came
  in -- see `_path_response_pending`) is a one-shot, unacknowledged
  `CHANNEL` broadcast with no retry unlike a `DIRECT` send
  (`_send_direct_with_retry`). It previously shared
  `announce_retransmit_extra` (`0` by default, deliberately -- see the
  `alpha-0.1-snapshot1`-era default change below -- since a spontaneous
  self-announce nobody's waiting on shouldn't be retried), so in practice
  it had no retry margin at all: losing it over a lossy multi-hop
  `CHANNEL` relay just silently expired the requester's path-request
  timeout, indistinguishable on their end from "no path exists". It now
  gets its own budget, non-zero by default because (unlike a spontaneous
  announce) it's narrowly scoped -- `_schedule_extra_retransmits` only
  applies it when `_rate_limit_announce` identifies the send as
  demand-driven -- and specifically the case field testing found
  unreliable. `_rate_limit_announce` now returns `(suppress,
  is_path_response)` instead of a bare bool so `processOutgoing` can pass
  that flag through to `_schedule_extra_retransmits`.
- `tests/test_path_response_retry.py`: `_rate_limit_announce`'s new
  return shape across every branch (non-announce packet, first announce
  for a destination, rate-limited, demand-driven bypass, expired pending
  entry, single-consumption of a pending entry), and
  `_schedule_extra_retransmits` picking `path_response_retransmit_extra`
  only when the flag is set (never for an ordinary announce, never for a
  non-announce packet type, nothing scheduled when the budget is `0`).

### Fixed (found live-testing `force_direct_path` against real repeater hardware)

- **`force_direct_path` silently never applied to an already-known
  peer**: the override was only ever applied from `_bind_meshcore_contact`,
  which fires solely on a LIVE MeshCore contact-table event (NEW_CONTACT/
  CONTACTS/CONTACTS_FULL/PATH_UPDATE/ADVERTISEMENT). A peer restored by
  `_load_peer_cache` (or otherwise already known before those event
  subscriptions are even wired up in `_async_setup`) goes through
  `_register_peer_binding` directly and never touches
  `_bind_meshcore_contact` -- so if that peer's path never happens to
  change again during the session, the override sat inert indefinitely,
  with no error and no log trace, while `out_path` stayed whatever a
  previous real session had left it at. Confirmed live: 6+ minutes of a
  field-test session with the pinned peer already known and unchanging.
  Fix: `_setup_apply_forced_direct_path`, run once during startup right
  after `_load_peer_cache`, applies the override directly against
  whatever contact `get_contact_by_key_prefix` already has for
  `force_direct_path_peer` -- `_bind_meshcore_contact`'s reactive hook
  stays as a second chance for a peer that isn't known yet at that point.
- **PROOF-of-delivery replies never used a peer's pinned (or otherwise
  known-good) DIRECT path**: found immediately after the fix above, once
  DIRECT sends over a correctly-pinned path were confirmed working
  (37 sends, 97.3% success, ~3s avg RTT) but an end-to-end delivery-proof
  probe still only got 1 of 7 proofs back in time, despite the responder
  actually receiving and auto-proving 6 of the 7. Root cause: DIRECT
  routing (`_resolve_outgoing_route`) requires the packet's destination-
  hash bytes to already be a key in `_rns_to_mc_map`, populated by
  `_learn_rns_token_binding` from observed traffic. That works for a
  Link (its destination becomes a stable ephemeral ID reused for the
  Link's whole lifetime, so learning it once helps every later packet),
  but a PROOF packet's destination-hash field is never a stable identity
  at all -- per `RNS.Packet.ProofDestination`, it's the truncated hash of
  the ORIGINAL packet being proved, different for literally every
  packet, so it can never land in `_rns_to_mc_map` ahead of time. Every
  `PROVE_ALL`-style delivery receipt for a one-off packet (not just this
  session's test probes -- any NomadNet/MeshChat message using delivery
  confirmation) was therefore falling back to CHANNEL regardless of how
  good the sender's known DIRECT path was, hitting exactly the flood-
  reliability ceiling prior field tests already documented, for traffic
  that didn't need to. Fix: `_deliver_reassembled_packet` now also
  records `truncated_packet_hash -> sender's MeshCore key` in a new
  short-TTL `_pending_proof_targets` map (120s, swept by the existing
  30s `_cleanup_loop` alongside the other bounded dicts) the moment it
  delivers a packet from a known peer; `_resolve_outgoing_route` checks
  that map, as a fallback behind `_rns_to_mc_map`, when a PROOF's token
  misses -- an exact match (the token IS that packet's hash), never a
  heuristic. `_link_id_from_lr_packet` (already computing this same
  RNS-defined hash for a different reason -- a LINK_REQUEST's own
  truncated hash becomes its Link's ephemeral ID) is renamed
  `_compute_truncated_packet_hash` and shared between both uses rather
  than duplicating the formula. Verified against real `RNS.Packet`
  objects (not just re-derived independently) that the computed hash
  matches `packet.truncated_packet_hash` exactly.
- `tests/test_proof_routing.py`: the hash computation against a real
  `RNS.Packet`-independent reference implementation (hop-count exclusion,
  HEADER_2 transport-ID exclusion, too-short-packet handling),
  `_deliver_reassembled_packet` recording a target only for a known
  sender, `_resolve_outgoing_route`'s PROOF fallback (matches, expires,
  falls through when unset, never triggers for a non-PROOF packet, and
  `_rns_to_mc_map` still wins if both would match), and the cleanup
  sweep.

## [alpha-0.1-snapshot2] - 2026-09-12

This release is a direct response to the
[`alpha-0.1-snapshot1`](fieldtests/reports/alpha-0.1-snapshot1.md) field test
report, plus a batch of RNS-core-integration and observability improvements
that came out of a broader design review. Not yet field-tested itself --
this is the build intended for the next round of field testing
(`fieldtests/raw/alpha-0.1-snapshot2`).

### Fixed

- **Concurrent radio commands received each other's replies**: the `meshcore`
  library's `CommandHandler.send()` has no locking and matches a reply by
  event *type* only -- it subscribes to the expected types, writes the frame,
  and returns the first such event the dispatcher fans out to every
  subscriber. This interface ran several commands at once (the DIRECT and
  CHANNEL send workers, path discovery + flood advert on failure,
  `reset_path` inside the retry loop, the 30 s contact refresh, the
  utilization and echo-check stats polls), and `send_msg` and
  `send_path_discovery` both wait on `MSG_SENT`. The snapshot1 laptop log
  shows the result four times: a DIRECT attempt logging
  `firmware suggested 4036ms` on a 3-hop route (every genuine suggestion was
  14-21 s) in the same second a discovery launched -- the send had adopted
  the discovery request's tag as its `expected_ack` and could never be
  ACKed, while the discovery adopted the message's reply and "timed out".
  Because discovery/advert/reset only fire on failures, and failures only
  happened over repeaters (see below), the collisions cascaded exactly
  there and never at 0-1 hops. Fix: every command to the radio now goes
  through one `asyncio.Lock` (`_install_command_serializer` wraps the
  library's `send()` itself, so its internal auto-fetch and
  `ensure_contacts` are covered too). Only the command round trip is held;
  delivery-ACK waits stay outside the lock.
- **Delivery ACK ceiling was shorter than the firmware's own estimate on
  every multi-hop route** (`direct_ack_timeout_max`, 8 s then 10 s): the
  same log shows the firmware suggesting 10.9 s at 1 hop and 13.7-21.3 s at
  3 hops, so the interface declared fragments failed before a legitimately
  in-flight ACK could arrive -- 44 of its 107 ACK timeouts coincide with
  the transfer node completing a DIRECT packet from the laptop inside that
  very wait window. The late ACK was then discarded (`_on_msg_ack` was a
  no-op), the fragment re-sent up to 3x, and after two such truncated waits
  the working route was wiped by `_maybe_reset_stale_path`. Fix, in three
  parts: (1) `MSG_SENT`'s type byte says whether the firmware routed or
  flooded the send, so routed sends now get their own ceiling
  `direct_ack_timeout_routed_max` (45 s) while only flood-mode sends keep
  `direct_ack_timeout_max` (10 s); (2) all attempts of a fragment share one
  delivery record (`_pending_acks`, resolved from `_on_msg_ack`, with a
  short `_recent_acks` memory for an ACK that beats its registration), so
  an ACK for an earlier attempt still counts as delivered; (3) only attempts
  that waited the firmware's full suggested time count toward
  `direct_path_reset_threshold`.
- **DIRECT send reliability over repeater-relayed (multi-hop) links**: found
  by comparing against the official meshcore client's own
  `send_msg_with_retry`, after the official MeshCore app was reported to
  send/receive reliably on the same hardware where this interface's DIRECT
  sends were failing 100% of the time. Two concrete divergences, both of
  which specifically worsen with hop count:
  - `direct_ack_timeout_max` (the hard ceiling on how long to wait for a
    delivery ACK) defaulted to `8.0s`, while the official client applies no
    ceiling at all. Field logs showed the firmware suggesting entirely
    ordinary multi-hop timeouts of 9.9-12.4s for a single-repeater
    (`out_path_len=1`) route -- our cap was guaranteed to declare failure
    2-4+ seconds before a legitimately in-flight ACK could ever arrive.
    Raised to `20.0s`, still well short of the genuinely pathological
    flood/no-path case (which can run into minutes) that the cap exists to
    guard against.
  - Reset-to-flood-mode-on-repeated-failure was previously decided from a
    cross-*packet* counter requiring an entire packet's `direct_send_attempts`
    to fail, then a second packet's too, before ever resetting a stale
    cached path -- about 3x more raw unicast attempts than the official
    client's own `send_msg_with_retry`, which resets after `flood_after`
    (default 2) attempts *within one message's own retry loop*. Moved into
    `_send_direct_with_retry` itself (new `_maybe_reset_stale_path`) so it
    now triggers on the same per-attempt basis official does, keeping the
    existing RSSI-gated patience logic (`direct_path_reset_rssi_floor` /
    `direct_path_reset_patience_multiplier`) but applying it to raw
    attempts within a single send rather than whole packets.
- **Peer capability mislabeling** (edge nodes repeatedly relabeled "router"):
  root-caused directly from the snapshot1 field test logs, which showed a
  peer configured `can_route=no` for its entire session getting bound as
  `[edge]` immediately on RNSBIND, then relabeled `[router]` by the next
  periodic contact refresh (every 30s), and repeating that flip indefinitely.
  Cause: `_bind_meshcore_contact` read `contact.get("can_route", True)` --
  but a MeshCore contact record has no such key at all (verified against the
  library's contact parser: only `public_key`/`out_path_len`/`adv_name`/etc),
  so this silently defaulted to `True` on every single contact-table event.
  Capability can now only ever be set from an actual RNSBIND/RNSBIND_REQ
  message; contact-table events (`_bind_meshcore_contact`) no longer touch a
  peer's recorded capability at all. Low runtime impact today (capability
  isn't read by any routing decision yet), but it was corrupting state a
  future capability-aware feature would depend on, and misleading every log
  line in the meantime.
- **`firmware_text_limit`** (was hardcoded to `128` in `_auto_payload_size`):
  verified directly against the MeshCore firmware source
  (`MAX_TEXT_LEN = 10*CIPHER_BLOCK_SIZE = 160`, `src/helpers/BaseChatMesh.h` /
  `src/MeshCore.h`) that the real per-message character ceiling is `160`, not
  `128` -- fragments were sized ~25-30% smaller than necessary. Now a
  configurable setting (`firmware_text_limit`, default `160`) rather than a
  hardcoded guess, in case a specific firmware build or BLE stack genuinely
  needs a lower value. The safety margin was also bumped from 2 to 4
  characters, to cover the firmware's own additional 2-character shrink on a
  DIRECT message's 4th+ send attempt (`attempt > 3` in `composeMsgPacket`).
- Outgoing announce/path-request rate limiters keyed on only the first 10 of
  the 16-byte RNS destination hash (`data[2:12]`), inconsistent with
  `_RNS_DST_LEN`/`_extract_rns_token` used everywhere else in the file.
  Corrected to the full 16 bytes (with matching length guards) in
  `_rate_limit_announce`, `_rate_limit_path_request`, and the
  `_path_response_pending` producer in `_deliver_reassembled_packet` (all
  three must agree, since the rate limiter looks up a key that the deliver
  path writes).

### Added

**Reliability, motivated by the snapshot1 field test**
- Peer-binding persistence (`_save_peer_cache` / `_load_peer_cache`):
  confirmed RNS-peer name<->MeshCore-pubkey bindings are now cached to a
  small local JSON file (under RNS's own storage directory) and restored on
  the next startup, but only for entries the MeshCore device's own live
  contact table still corroborates. Previously every `rnsd` restart
  discarded this mapping from memory and had to re-broadcast a fresh
  RNSBIND_REQ and sit through its backoff window before any traffic could
  flow again, even when the MeshCore device already had a working cached
  path -- directly observed in the field test's 9 `rnsd` restarts inside one
  49-minute window, each re-flooding the shared channel for no benefit.
  **Caught during this same round of testing**: the first implementation of
  this feature had a real regression -- a restored peer made
  `_bind_discovery_loop`'s `have_peers` check true immediately, which
  skipped the active `RNSBIND_REQ` phase entirely and went straight to a
  silent, unsolicited heartbeat with nothing sent again for
  `BIND_HEARTBEAT_S` (1 hour). Every prior restart (with no cache) always
  ran that active REQ phase, so this silenced the node's own startup
  advertisement and denied any node that didn't already have it cached the
  chance to learn about it. Fixed: the REQ phase now always runs at least
  once on a fresh start regardless of what the cache restored; only after
  it completes does cached/live peer state resume gating the steady-state
  heartbeat-vs-retry behavior as before. The RNSBIND heartbeat send (the
  quiet, no-response-expected branch) also previously had no logging at
  all on success or failure -- added, so a session that never sends an
  active REQ (because peers were already known) still leaves a visible
  trace that the heartbeat went out.
- Telemetry-permission auto-grant (`auto_grant_telemetry_permission`,
  default on): field-testing against real hardware found that MeshCore's
  path-discovery command is, per the firmware source itself ("'Path
  Discovery' is just a special case of flood + Telemetry req"), secretly a
  base-telemetry request -- and the firmware silently declines to answer
  it AT ALL unless the responding node's `telemetry_mode_base` preference
  allows it, which defaults to `TELEM_MODE_DENY`. This explained a
  100%-reproducible path-discovery failure under otherwise-ideal RF
  conditions (confirmed via live diagnostics against real hardware: the
  raw request always sent successfully and was confirmed physically
  received by the peer via the official MeshCore app, but no response was
  ever generated). Rather than opening telemetry to every MeshCore user in
  range (`TELEM_MODE_ALLOW_ALL`), this switches the node to
  `TELEM_MODE_ALLOW_FLAGS` and grants the per-contact permission bit only
  to peers who've proven they know this channel's secret via a real
  RNSBIND/RNSBIND_REQ (`_grant_telemetry_permission`, called from
  `_handle_bind`) -- confirmed RNS-tunnel peers, not every device sharing
  the LoRa channel.
- `direct_path_reset_rssi_floor` / `direct_path_reset_patience_multiplier`:
  before resetting a DIRECT peer's cached path to flood mode,
  `_handle_send_failure` now checks the last-polled RSSI. Resetting is
  irreversible -- it discards the path both locally and on the MeshCore
  device's own persistent contact record -- and forces recovery through
  flood-mode discovery specifically, which the firmware's own relay
  scheduler confirms is structurally less reliable over multiple hops than
  routed/DIRECT forwarding (randomized, increasingly-deprioritized
  rebroadcast at every hop, vs. a single top-priority designated relay per
  hop). The field test's outage began with a reset fired after only 2
  failures at a still-moderate RSSI/SNR reading, after which recovery
  depended entirely on flood discovery for ~65 minutes. Now, if RSSI still
  looks reasonable, the interface is `direct_path_reset_patience_multiplier`x
  (default 3) more patient before giving up on a path than failure count
  alone would suggest; it falls back to the original (fast) threshold when
  RSSI is poor or unavailable, preserving the behavior already validated for
  a genuinely stale path (repeater physically moved).

**RNS-core integration**
- SNR/RSSI reporting (`reports_phy_stats` / `r_stat_rssi` / `r_stat_snr`):
  the SNR (and, when available, RSSI) already arriving in MeshCore's own
  channel/direct-message events was being read off the payload and
  discarded -- it's now reported to RNS core for `rnstatus`/logging.
  Confirmed against RNS core (`Transport.py`) that this is display-only
  telemetry with no routing/retry/timeout logic behind it, and that it
  reflects last-hop link quality only, not end-to-end/whole-path quality.
- `shared_medium = True`: matches every other broadcast/half-duplex RNS
  interface (`SerialInterface`, `RNodeInterface`, `KISSInterface`, etc.) in
  correctly describing this interface's LoRa channel as shared airtime.
  Confirmed no RNS-core consumer currently reads this flag, so this is a
  correctness/self-description fix, not a behavior change.

**Observability**
- `get_stats_core()` added to the existing local (no mesh airtime) radio/
  packet stats poll, surfacing the MeshCore device's own outgoing TX queue
  depth (`tx_queue_len`) and battery voltage (`battery_mv`) -- a direct
  "is the local mesh side backed up" signal that wasn't being collected
  before.
- `_SessionStats.snapshot()` now also reports `routing_health` (per-target
  consecutive path-discovery and DIRECT-path failure counts -- already
  tracked internally for the adaptive-backoff/reset-to-flood logic, but
  never previously surfaced outside it -- plus current DIRECT/CHANNEL
  outgoing queue depths) and `outgoing_packet_type_counts` (DATA/ANNOUNCE/
  LINK_REQ/PROOF mix). Together these are the concrete signals a future
  self-tuning controller (see the module docstring's TODO list) would need
  to distinguish RNS-side demand from local mesh congestion.

### Internal / code quality

- Centralized the RNS packet-type-name mapping (`_PTYPE_NAMES`) instead of
  duplicating an inline dict in two places.

## [alpha-0.1.1] - 2026-09-12

This release is a substantial rework of the forked interface, focused on
delivery reliability, routing correctness, benchmarking/observability, and
hardening against unbounded resource growth on a shared radio medium. The
sections below are grouped by concern rather than by commit.

### Added

**Path discovery & DIRECT routing correctness**
- New `discover_path()` coroutine that runs a one-shot MeshCore path-discovery
  query for a contact and logs the result. The original had no equivalent —
  a peer with an unresolved path (`out_path_len == -1`) simply stayed on
  CHANNEL forever unless the official MeshCore app happened to resolve it.
- Adaptive per-peer path-discovery backoff (`path_discovery_base_cooldown`,
  `path_discovery_max_cooldown`, `path_discovery_backoff_factor`): cooldown
  doubles per consecutive discovery failure (with jitter) instead of retrying
  a broken hop at a fixed interval forever, and resets to fast retry the
  moment a discovery succeeds.
- `path_discovery_quick_attempts`: extra quick-retry attempts for path
  discovery to recover from a single lost broadcast without waiting a full
  backoff cycle.
- Outgoing routing (`processOutgoing` / `_resolve_outgoing_route`) now checks
  MeshCore's own cached `out_path_len` for a bound peer before committing to
  DIRECT. If unresolved, it falls back to CHANNEL for that send *and* kicks
  off a rate-limited `discover_path()` + flood advert in the background,
  instead of just attempting (and failing) a DIRECT send against an unknown
  route as the original did.
- `direct_path_reset_threshold` + consecutive-failure tracking
  (`_direct_path_failures`): after N consecutive DIRECT send failures against
  a peer's *currently cached* path, that path is reset to flood mode
  (`reset_path`) instead of being retried forever. Verified in field testing
  that a stale cached path (e.g. after a repeater was repositioned) fails far
  more often than letting the firmware re-flood for a working route.
- Peer path-change logging (`_log_path_if_changed`): logs whenever MeshCore's
  reported `out_path_len`/`out_path` for a peer actually changes, sourced
  from both contact-update events and the new periodic contact refresh below,
  so path resolution can be observed from the logs alone.
- `_contact_refresh_loop` / `contact_refresh_interval`: periodically
  re-fetches MeshCore's contact list so cached path info doesn't go stale
  between contact-update events. The original only ever fetched contacts once
  at startup.
- `discover_path` explicitly persists a successfully resolved path to the
  MeshCore device's own contact table via `change_contact_path()`
  (`CMD_ADD_UPDATE_CONTACT`). Verified against the MeshCore firmware source
  (`examples/companion_radio/MyMesh.cpp`) that the one-shot discovery command
  (`send_path_discovery_sync` / `CMD_SEND_PATH_DISCOVERY_REQ`) resolves the
  path for our own use but does *not* write it to the device's persistent
  contact record — only an ordinary message exchange's ACK does that
  normally. Without this explicit persist step, a path our interface
  considered resolved could still show as unresolved to the official
  MeshCore app.

**Delivery reliability**
- `direct_send_attempts` + `_send_direct_with_retry`: a DIRECT send is now
  retried up to N times (fresh send, fresh `expected_ack`, each attempt) on
  ACK timeout before falling back to CHANNEL. The original gave DIRECT
  exactly one attempt before escalating to a broadcast resend.
- `ordinary_data_retransmit_extra`: non-broadcast packets that fell back to
  CHANNEL (no bound peer / no resolved route) can now also get extra
  best-effort retransmit passes, matching the retransmit treatment ANNOUNCE
  and path-request packets already had in the original.
- Auto-reconnect for the underlying serial/BLE/TCP link
  (`auto_reconnect`, `max_reconnect_attempts`) with CONNECTED/DISCONNECTED
  event subscriptions (`_on_mc_connected`, `_on_mc_disconnected`). The
  original had no connection-lifecycle handling at all — a dropped USB/BLE
  link left the interface silently "online" with a dead connection
  underneath.

**Throughput & queue behavior**
- Outgoing traffic is now split into two independent
  `queue.PriorityQueue`s (`_direct_outqueue`, `_channel_outqueue`), each
  drained by its own worker task, instead of one shared FIFO `queue.Queue`.
  A DIRECT send stuck waiting on a delivery ACK can no longer stall CHANNEL
  broadcasts (or other DIRECT sends) queued behind it, and vice versa.
- Priority tiers within each queue: LINK_REQUEST and PROOF (handshake)
  packets are dequeued ahead of ordinary DATA/ANNOUNCE traffic, since RNS's
  own Link-establishment timeout is only a handful of seconds per hop and
  can expire while stuck behind a bulk data burst in a plain FIFO queue.
- Stale-fragment dropping (`stale_fragment_max_age`,
  `stale_fragment_min_queue_depth`): a non-broadcast fragment is only
  dropped once it has *both* waited past a max age *and* has a genuine
  backlog of other fragments behind it — age alone is not sufficient, since a
  fragment that simply had bad luck on an otherwise quiet queue is still
  worth sending. The original had no mechanism to shed queued work at all.
- `_auto_payload_size()`: automatically shrinks/grows the configured
  `payload_size` once the node's own name is known, so a fully encoded
  fragment (header + payload + prefix + the node name MeshCore prepends on
  relay) never exceeds the firmware's channel-message character limit. The
  original used a fixed `payload_size` with no adjustment for node name
  length, risking silent firmware truncation for longer node names.

**Observability / benchmarking**
- New `_SessionStats` / `_SlidingWindowCounter` classes tracking, for the
  life of the interface: peak TX/RX fragments-per-second, session total
  TX/RX bytes, current TX/RX bitrate (10s sliding window), flood
  (CHANNEL) messages sent in the last minute, DIRECT send failure rate
  overall and per-link, MeshCore hop count per known peer
  (`get_peer_hop_counts`), RNS-level queue-to-delivered latency, and
  MeshCore-level DIRECT send-to-ACK round-trip latency (overall and
  per-peer). None of this existed in the original.
- `_poll_mesh_utilization`: periodically polls MeshCore's own
  `get_stats_radio`/`get_stats_packets` commands (local, no extra mesh
  airtime) to derive an RX duty-cycle percentage for the local channel,
  independent of this interface's own traffic.
- `_stats_summary_loop`: when debug logging is enabled, prints a full
  statistics summary once a minute.
- Debug-only logging tier (`debug_level` config key, `_debug()` helper):
  verbose diagnostic detail (dropped/duplicate fragment reasons, poll
  failures, etc.) can be enabled independently of RNS's own global log
  level, instead of either being always-on noise or unavailable.

**Zero-configuration interoperability**
- A real, fixed default MeshCore channel secret is now used when
  `channel_secret` is left unset, so two freshly configured nodes can find
  each other with no coordination. The original's default
  (`"00000000000000000000000000000000"`) was a non-functional placeholder
  that effectively required every deployment to set its own secret before
  nodes could talk to each other at all. The security tradeoff (this default
  channel is shared/public at the MeshCore layer, though RNS traffic on top
  of it is already end-to-end encrypted) is logged explicitly at startup and
  documented in the module docstring and README.

### Changed

**Protocol / wire format**
- Fragment encoding switched from base64url to Z85. Z85 expands data by only
  25% (5 output chars per 4 input bytes) versus base64's 33%, giving more
  usable payload per MeshCore channel-message character-limit budget for the
  same overhead.

**Configuration defaults** (tuned since forking; see README for the full
tuning reference table)
- `announce_retransmit_extra`: `2` → `0` (superseded by the more targeted
  path-response-bypass and retry mechanisms added since forking).
- `fragment_delay` (CHANNEL fragment pacing): `2.5s` → `1.0s`, leaning
  towards throughput during active development; intended to be revisited
  towards a more mesh-considerate value once the build is stable.
- `contact_refresh_interval` (new setting, see Added above) defaults to
  `30s` — a local device query with no mesh airtime cost, so it can be
  polled fairly frequently.

### Fixed

- DIRECT sends against a peer with a stale-but-still-cached MeshCore path
  (e.g. a repeater that was physically repositioned) would fail repeatedly
  with no recovery path in the original. Field testing during this fork's
  development confirmed resetting to flood mode after repeated failures
  restores delivery (67% vs 0% success observed in a live A/B test), which
  is now handled automatically (`direct_path_reset_threshold`).
- A path resolved via the one-shot MeshCore path-discovery command was not
  actually written to the device's own persistent contact table (see
  `discover_path` under Added above), so it could appear resolved to this
  interface while still showing as unresolved everywhere else (including
  after a later contact refresh, or in the official MeshCore app).
- A malformed f-string (unbalanced quotes) in the startup log line reporting
  which contact-update events the meshcore library exposed, which could
  raise instead of logging when none were found.

### Security

- Several internal state dictionaries populated from data received over the
  open MeshCore RF medium (not just from already-bound peers) had no upper
  bound between their periodic cleanup passes, and could in principle grow
  without limit if a nearby transmitter — malicious or simply very noisy —
  sent enough distinct fabricated identities/packets:
  - `_assembly` / `_assembly_meta` (in-progress multi-fragment reassembly
    buffers, keyed by `(sender, pkt_id)`) — now capped at
    `_ASSEMBLY_MAX_KEYS`, oldest-first eviction.
  - `_peer_table` and its associated `_reverse_peers` / `_peer_caps` /
    `_peer_last_seen` dicts — previously only cleaned up on the (24-hour
    default) `peer_ttl` window, now additionally capped at
    `_PEER_TABLE_MAX_PEERS`, least-recently-seen eviction.
  - `_seen_pkts` (sliding-window packet-dedup cache) — now capped at
    `_SEEN_PKTS_MAX_KEYS`.
  - `_last_unbound_req` (opportunistic-bind-request throttle per unbound
    sender name) — now capped at `_UNBOUND_REQ_MAX_SENDERS`.
  All four follow the same oldest-eviction pattern the original already used
  for `_rns_to_mc_map` (`_RNS_MAP_MAX`), and none of these caps change
  behavior for legitimate traffic — normal usage never approaches them.

### Internal / code quality

- The 397-line monolithic `__init__` and 354-line `_async_setup` were split
  into focused, independently testable helper methods (`_configure_*` /
  `_setup_*`), each responsible for one cohesive slice of configuration or
  startup sequencing.
- `processOutgoing`, `_async_outgoing_worker`, and `_process_tunnel_text`
  (the three largest runtime hot paths) were similarly decomposed into
  smaller named helpers (rate limiting, route resolution, fragment
  enqueueing/sending, failure handling, dedup, reassembly, delivery).
- Every method now carries a short docstring describing its behavior; the
  original left most methods undocumented.
- Various stale/inaccurate comments (leftover references to base64 after
  the Z85 switch, a "single shared queue" comment after the queue split,
  etc.) were corrected.

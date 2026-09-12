# Changelog

All notable changes in this repository compared to the original upstream
project it was forked from (preserved at
[`referenceprojects/MeshCore_Dynamic_Interface_original_repo.py`](referenceprojects/MeshCore_Dynamic_Interface_original_repo.py))
are documented here.

## [alpha-0.1-snapshot2] - 2026-09-12

This release is a direct response to the
[`alpha-0.1-snapshot1`](fieldtests/reports/alpha-0.1-snapshot1.md) field test
report, plus a batch of RNS-core-integration and observability improvements
that came out of a broader design review. Not yet field-tested itself --
this is the build intended for the next round of field testing
(`fieldtests/raw/alpha-0.1-snapshot2`).

### Fixed

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

# Changelog

All notable changes in this repository compared to the original upstream
project it was forked from (preserved at
[`referenceprojects/MeshCore_Dynamic_Interface_original_repo.py`](referenceprojects/MeshCore_Dynamic_Interface_original_repo.py))
are documented here.

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

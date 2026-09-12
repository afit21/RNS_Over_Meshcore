"""
MeshCore_Dynamic_Interface.py
Reticulum (RNS) interface over a MeshCore LoRa mesh network.

TODO:
    Core optimisations:
    - [Big] Adaptive fragment sizing (get node name, and firmware limit, size the payload accordingly). Adapt fragment size if direct, channel, and based on node names. Save 3 bytes at end for repeater info
    - Make retransmissions fragment-aware (retransmit currently retransmits every fragment, regardless of if some arrived)
    - Replace periodic contact refresh with on direct route failure and/or path length unknown
    - Adaptive transmission delays

    Byte Optimisations:
    - Shorten 'RNS:' to 'R:'
    - Swap to 2 byte packet id instead of 4 byte.
        - Randmise starting packet
        - purge old packet ids
    - Reduce rns advert message sizes

    Usability changes:
    - Add safety flag in config to block certain config options unless safety == false
        - Block config settings that would flood the meshcore

    Announce and path request limiting changes:
    - prioritise announces that get through by amount of hops.
    - Block announces if a node teaches an announce rate threashold
    - Track who each announce is for and either drop or cache the announce for later.
        - Forward cached announces when Meshcore utilisation is low.

Implements a hybrid channel-broadcast / unicast-direct routing strategy with
demand-driven peer discovery and edge-node capability advertisement.  No static
remote-node configuration is required.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WIRE FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Each RNS binary packet is split into payload-sized chunks.  Each chunk is
encoded as a MeshCore channel (or direct) message:

    "RNS:" + Z85( [frag_idx:1][pkt_id:4][frag_total:1] + payload )

Z85 (see z85_encode/z85_decode below) is used instead of base64: it expands
data by only 25% (5 output chars per 4 input bytes) versus base64's 33%,
and its alphabet avoids characters MeshCore's text-message framing could
mistake for delimiters. The first output character is a self-describing
zero-pad count (0-3), so no separate padding scheme is needed on the wire.

RNS HEADER BYTE BIT LAYOUT (single-header packet, bit 7 = 0)
  bits 7-6 : header type     (0b10 = two-byte header; always broadcast)
  bits 5-4 : transport flags
  bits 3-2 : destination type  <- extracted with (flags >> 2) & 0x03
  bits 1-0 : packet type       <- extracted with  flags       & 0x03

  Packet type values:  DATA=0x00  ANNOUNCE=0x01  LINKREQUEST=0x02  PROOF=0x03
  Dest type values:    SINGLE=0x00  GROUP=0x01  PLAIN=0x02  LINK=0x03

  A DATA packet with PLAIN destination (header byte 0x08) is a PATH REQUEST —
  a node searching for a destination it has lost the path to.  AP mode does
  NOT suppress path requests; it only blocks ANNOUNCE re-broadcasting.  If a
  node that was recently reachable goes offline, remote nodes will generate a
  continuous stream of path requests that will pass straight through AP mode
  and onto the LoRa channel.  The outgoing_path_req_rate limiter handles this.

PAYLOAD SIZE
  MeshCore firmware silently truncates channel/direct messages that exceed a
  per-message character limit -- confirmed against the reference companion-
  radio firmware source (MAX_TEXT_LEN = 10*CIPHER_BLOCK_SIZE = 160 chars;
  src/helpers/BaseChatMesh.h / src/MeshCore.h). This is configurable via
  firmware_text_limit (default 160) in case a specific firmware build or BLE
  stack genuinely needs a lower value. The firmware also prepends the
  sender's node name when relaying channel messages, so the effective
  character budget for the encoded portion is:

      budget = firmware_limit - len(node_name) - 2       (": " separator)

  Encoded message length (Z85: 1 pad-count char + 5 chars per 4 raw bytes,
  raw bytes rounded up to a multiple of 4 first):
      msg_len = 1 + 5*ceil((payload_size + HEADER_SIZE) / 4) + len("RNS:")

  With a 4-byte pkt_id, HEADER_SIZE is 6 bytes. With default payload_size = 64:
      msg_len = 1 + 5*ceil(70/4) + 4 = 1 + 90 + 4 = 95 chars
      Safe for node names up to ~58 characters at the default 160-char
      firmware limit (a 4-char safety margin is also subtracted -- see
      _auto_payload_size -- to cover firmware variation and its own
      additional 2-char shrink on a message's 4th+ send attempt).

  To calculate the maximum safe payload size for your node name length (this
  is exactly what _auto_payload_size() computes at runtime once the node's
  own name is known, so payload_size rarely needs to be set by hand):
      budget      = firmware_limit - len(node_name) - 2
      max_payload = floor((budget - 5) / 5) * 4 - HEADER_SIZE - margin

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PEER DISCOVERY PROTOCOL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Peer discovery uses a demand-driven RNSBIND_REQ / RNSBIND exchange rather than
periodic push-based broadcasting, minimising channel airtime consumption.

  1. A node with no known peers sends "RNSBIND_REQ:<pubkey>:<cap>" on the
     channel, advertising its own routing capability alongside its identity.
  2. Each overhearing node immediately records the requester's info and
     capability (passive L2 learning), waits a random delay (BIND_BACKOFF_MIN
     to BIND_BACKOFF_MAX seconds), then responds with "RNSBIND:<pubkey>:<cap>".
  3. The random backoff follows the RFC 2236 (IGMP) report suppression
     principle: responses are spread in time to prevent a simultaneous burst
     on the shared half-duplex LoRa channel.
  4. Every node overhearing any RNSBIND response also records the responder,
     so a single discovery round passively populates all peer tables.
  5. Once peers are known, a quiet RNSBIND heartbeat is sent every
     BIND_HEARTBEAT_S (default 1 hour) — no response is solicited.

CAPABILITY FIELD
  The capability suffix ("R" = router, "E" = edge) is appended to every RNSBIND
  and RNSBIND_REQ message so that peers learn at discovery time whether a node
  can carry transit traffic to the wider Reticulum mesh.

      RNSBIND:<pubkey>:R    — routing node (has upstream connectivity)
      RNSBIND:<pubkey>:E    — edge node (no upstream; do not route through me)
      RNSBIND:<pubkey>      — legacy format (no capability field); treated as :R

  The capability field operates at the discovery layer only.  It is recorded in
  the peer table and logged, but it does NOT affect per-packet routing decisions.
  The _rns_to_mc_map is populated by observed packet flow, so any entry in it
  represents a path that has demonstrably worked — including paths that transit
  through an edge node to reach a client device behind it (e.g. a phone
  connected to a hotspot hosted by the edge node).  Filtering those map entries
  by capability would incorrectly block delivery to legitimate downstream clients.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RNS INTERFACE MODE CONFIGURATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Interface modes have a significant impact on announce traffic, path expiry,
and channel load.  The modes below are sourced from the official Reticulum
manual (https://reticulum.network/manual/interfaces.html).

INFRASTRUCTURE / TRANSPORT NODE  (fixed gateway with backbone connectivity)
──────────────────────────────────────────────────────────────────────────────
  [[MeshCore Dynamic Interface]]   mode = access_point    can_route = yes
  [[Backbone Interface]]           mode = boundary

  access_point
    Announces are NOT automatically re-broadcast on this interface.  Paths to
    destinations on the interface expire faster, matching the transient nature
    of battery-powered or intermittently-connected field devices.  Path requests
    from clients are still forwarded and resolved on their behalf, as with
    gateway mode.

    NOTE: AP mode only suppresses ANNOUNCE re-broadcasting.  DATA+PLAIN path
    requests from the wider mesh for recently-offline nodes will still pass
    through AP mode onto the LoRa channel.  Use outgoing_path_req_rate to
    throttle these independently.

    !! NEVER use gateway mode on a LoRa interface on a node that is also
    !! connected to a high-connectivity backbone.  gateway mode proactively
    !! pushes ALL known announces to clients on that interface.  With thousands
    !! of routes from the public Reticulum mesh, this will flood a shared LoRa
    !! channel indefinitely and render it unusable.

  boundary
    Applied to the backbone/TCP interface connecting the slow radio segment to
    the fast LAN or Internet.  Marks the network edge and prevents the transport
    node from treating the backbone as a client-facing interface for proactive
    path distribution.

  Add announce rate control to the backbone interface to throttle how quickly
  announces from the wider network are re-propagated to other interfaces:

      announce_rate_target  = 3600   # min seconds between re-announces per dest
      announce_rate_grace   = 2      # violations tolerated before enforcement
      announce_rate_penalty = 7200   # extended quiet period after a violation

  Full example — infrastructure / transport node
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ [reticulum]                                                             │
  │   enable_transport = yes                                                │
  │   share_instance = yes                                                  │
  │                                                                         │
  │ [logging]                                                               │
  │   loglevel = 4    # increase to 7 for debug                            │
  │                                                                         │
  │ [interfaces]                                                            │
  │                                                                         │
  │   [[MeshCore Dynamic Interface]]                                        │
  │     type = MeshCore_Dynamic_Interface                                   │
  │     interface_enabled = yes                                             │
  │                                                                         │
  │     # Role                                                              │
  │     mode = access_point                                                 │
  │     can_route = yes                                                     │
  │                                                                         │
  │     # Transport — uncomment exactly one block                          │
  │     # Serial (most common):                                             │
  │     transport = serial                                                  │
  │     port = /dev/ttyUSB0    # adjust to your serial device              │
  │     baudrate = 115200                                                   │
  │     #                                                                   │
  │     # TCP (MeshCore node reachable over IP):                           │
  │     # transport = tcp                                                   │
  │     # host = 127.0.0.1                                                 │
  │     # tcp_port = 4403                                                   │
  │     #                                                                   │
  │     # BLE:                                                              │
  │     # transport = ble                                                   │
  │     # ble_name =           # blank = connect to first found device     │
  │                                                                         │
  │     # BLE/serial/TCP link resilience                                   │
  │     auto_reconnect = yes         # try to recover a dropped link       │
  │     max_reconnect_attempts = 3   # give up after this many tries       │
  │                                                                         │
  │     # Channel — defaults join a shared public channel with zero         │
  │     # coordination needed. RNS already encrypts/authenticates your      │
  │     # traffic end-to-end, so a shared default here isn't a security     │
  │     # concern. Uncomment to run your own private channel instead.       │
  │     # channel_idx = 0                                                   │
  │     # channel_name = RNSTunnel                                          │
  │     # channel_secret = <32 hex chars>  # openssl rand -hex 16           │
  │                                                                         │
  │     # Radio overrides — all four must be non-zero to take effect.      │
  │     # Leave commented to use the values stored on the MeshCore node.   │
  │     # freq = 915.0         # MHz centre frequency                      │
  │     # bw   = 250.0         # kHz bandwidth  (125 / 250 / 500)         │
  │     # sf   = 10            # spreading factor (7–12)                   │
  │     # cr   = 5             # coding rate denominator (5=4/5 … 8=4/8)  │
  │                                                                         │
  │     # Fragmentation                                                     │
  │     payload_size = 64      # bytes/fragment (see PAYLOAD SIZE note)    │
  │     fragment_delay = 1.0   # seconds between channel-mode fragments    │
  │     direct_frag_delay = 0.5  # seconds between direct-message frags   │
  │     fragment_timeout = 300   # 5-minute window for high-latency meshes │
  │                                                                         │
  │     # Outgoing rate limiting (set to 0 to disable)                     │
  │     outgoing_announce_rate = 600    # min s between announces per dest │
  │     outgoing_path_req_rate = 1800   # min s between path reqs per dest │
  │     path_req_burst_window = 60      # s to let RNS's own retry burst   │
  │                                      # through before the cooldown     │
  │                                      # above applies (see code comment)│
  │                                                                         │
  │     # Optional hard bandwidth cap in bits per second (0 = disabled)    │
  │     # rate_limit = 1200                                                 │
  │                                                                         │
  │     # Peer discovery                                                    │
  │     allow_direct = yes      # use unicast direct msgs when route known │
  │     peer_ttl = 86400        # seconds before a silent peer expires     │
  │                                                                         │
  │     # Verbosity: this driver logs its own routing/rate-limiter/peer-     │
  │     # binding events at RNS.LOG_INFO, so they show up at the standard   │
  │     # [logging] loglevel = 4 without needing full RNS-core debug (7).   │
  │                                                                         │
  │   [[Backbone Interface]]                                                │
  │     type = BackboneInterface                                            │
  │     interface_enabled = yes                                             │
  │     mode = boundary                                                     │
  │     target_host = <backbone-server-hostname-or-ip>                     │
  │     target_port = 4242                                                  │
  │     # Rate-limit announce re-propagation from the fast network         │
  │     announce_rate_target  = 3600                                        │
  │     announce_rate_grace   = 2                                           │
  │     announce_rate_penalty = 7200                                        │
  └─────────────────────────────────────────────────────────────────────────┘
"""

import RNS
from RNS.Interfaces.Interface import Interface
import asyncio
import collections
import hashlib
import itertools
import json
import os
import struct
import queue
import random
import threading
import time
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Fragmentation helper
# ─────────────────────────────────────────────────────────────────────────────

class _PacketHandler:
    """
    Encodes one RNS binary packet into one or more channel/direct message
    strings. Each fragment carries a 6-byte binary header:

        [ frag_idx : 1 byte ] [ pkt_id : 4 bytes ] [ frag_total : 1 byte ]

    followed by the raw payload chunk.  The combined bytes are Z85-encoded
    (self-describing zero-padding, no separate padding scheme needed) and
    prefixed with MSG_PREFIX ("RNS:").
    """

    HEADER_SIZE  = 6  # 1 byte idx + 4 bytes pkt_id + 1 byte total
    PAYLOAD_SIZE = 64
    MSG_PREFIX   = "RNS:"

    def __init__(self, data: bytes, pkt_id: int, payload_size: int = 0):
        """Split data into PAYLOAD_SIZE-ish chunks and build a header+Z85
        encoded fragment string for each one."""
        ps = payload_size if payload_size > 0 else self.PAYLOAD_SIZE
        raw_chunks = [data[i:i + ps] for i in range(0, len(data), ps)]
        total = len(raw_chunks)
        
        self.fragments = []
        for idx, chunk in enumerate(raw_chunks):
            # Header layout packed big-endian: B (1B index), I (4B packet ID), B (1B total fragments)
            header = struct.pack(">BIB", idx & 0xFF, pkt_id & 0xFFFFFFFF, total & 0xFF)
            encoded = z85_encode(header + chunk)
            self.fragments.append(self.MSG_PREFIX + encoded)

    def __len__(self):
        """Number of MeshCore fragments this packet was split into."""
        return len(self.fragments)


# ─────────────────────────────────────────────────────────────────────────────
# Session benchmarking stats
# ─────────────────────────────────────────────────────────────────────────────

class _SlidingWindowCounter:
    """Tracks timestamped samples and reports their total / average
    per-second rate within a trailing time window, pruning anything older
    than the window on every read or write. Used for "over the last N
    seconds" style stats (current bitrate, flood sends per minute) where a
    session-long running total or a fixed-bucket peak wouldn't answer "what
    is it doing *right now*"."""

    def __init__(self, window_s: float):
        """window_s: how far back samples are kept before aging out."""
        self.window_s = window_s
        self._samples = collections.deque()  # (monotonic_ts, amount)
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        """Drop samples older than window_s relative to `now`."""
        cutoff = now - self.window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def record(self, amount: float = 1) -> None:
        """Record one sample of `amount` at the current time."""
        now = time.monotonic()
        with self._lock:
            self._samples.append((now, amount))
            self._prune(now)

    def total(self) -> float:
        """Sum of all samples still within the trailing window."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            return sum(a for _, a in self._samples)

    def rate_per_sec(self) -> float:
        """Average per-second rate implied by total() over window_s."""
        return self.total() / self.window_s


class _SessionStats:
    """
    Lightweight in-memory benchmarking counters, purely for comparing
    throughput and reliability across different versions/configurations of
    this interface during testing. Not persisted anywhere -- resets every
    time the interface (re)starts, and carries no meaning beyond a single
    session. Also intended as the eventual data source for automatic
    tuning of send rates/announce behavior -- see the class-level TODO in
    the module docstring.

    Peak TX/RX fragments are measured as the highest number of fragments
    transmitted/received within any single one-second window during the
    session (a throughput-burst metric), not a queue depth or a running
    total. Current TX/RX rate is the average bitrate over the trailing 10
    seconds specifically (RATE_WINDOW_S), separate from the peak-fragments
    metric above -- one is "how much data is moving right now", the other
    is "how many individual radio transmissions happened in the busiest
    second". Direct failure rate is the fraction of direct sends that
    never got ACK'd after exhausting all retries (see direct_send_attempts)
    and had to fall back to CHANNEL, tracked both overall and per-peer.

    Two distinct latency metrics are tracked, since they answer different
    questions: RNS-level TX latency is wall-clock time from
    processOutgoing() first handing an RNS packet to this interface, to
    every one of its fragments being finally accounted for (delivered, or
    given up on for good with nothing further in flight) -- it includes
    queueing, every retry, and a DIRECT->CHANNEL fallback if one happens.
    MeshCore-level latency is the send+ACK round trip for a single DIRECT
    fragment specifically (CHANNEL has no ACK, so there's no equivalent
    per-fragment RTT to measure there) -- the raw radio/link
    responsiveness, independent of our own fragmentation or retry logic.
    """

    BUCKET_SECONDS = 1.0
    RATE_WINDOW_S  = 10.0
    FLOOD_WINDOW_S = 60.0
    LATENCY_SAMPLES_MAX = 200   # bounded memory for rolling latency samples

    def __init__(self):
        """Initialize all counters, buckets, and sample buffers to zero/empty."""
        self._lock = threading.Lock()
        self._session_start = time.monotonic()

        now = time.monotonic()
        self._tx_bucket_start = now
        self._tx_bucket_count = 0
        self.peak_tx_fragments_per_sec = 0

        self._rx_bucket_start = now
        self._rx_bucket_count = 0
        self.peak_rx_fragments_per_sec = 0

        # Cumulative RNS payload bytes for the session (mirrors the
        # interface's own self.txb/self.rxb, kept here too so a single
        # stats snapshot can report bytes and rates together).
        self.tx_bytes_total = 0
        self.rx_bytes_total = 0

        # Trailing-window byte counters for "current" bitrate.
        self._tx_bytes_window = _SlidingWindowCounter(self.RATE_WINDOW_S)
        self._rx_bytes_window = _SlidingWindowCounter(self.RATE_WINDOW_S)

        # CHANNEL-mode (flood) fragment sends in the trailing minute.
        self._flood_tx_window = _SlidingWindowCounter(self.FLOOD_WINDOW_S)

        # Aggregate direct-send outcomes, plus a per-peer breakdown so a
        # single bad link doesn't get averaged away by otherwise-healthy
        # ones.
        self._direct_send_total  = 0
        self._direct_send_failed = 0
        self._direct_by_peer = {}   # peer_key -> [total, failed]

        # Latest polled MeshCore firmware radio/packet stats -- reflects
        # activity from the WHOLE local channel (every node in range), not
        # just this interface, plus the delta-derived RX duty cycle between
        # polls. See MeshCore_Dynamic_Interface._poll_mesh_utilization.
        self.mesh_utilization = None   # dict, or None until first poll

        # Outgoing RNS packet-type mix (DATA/ANNOUNCE/LINK_REQ/PROOF),
        # tallied in processOutgoing -- distinguishes "retrying because of
        # bulk data" from "retrying because of an announce storm" in a way
        # a bare send/fail total can't.
        self._outgoing_ptype_counts = {}   # ptype name -> count

        # Point-in-time routing-health data pushed in from the interface
        # (which owns the underlying lock/queues/counters) rather than
        # computed here -- mirrors mesh_utilization above. See
        # set_routing_health() and MeshCore_Dynamic_Interface._stats_summary_loop.
        # Includes per-target consecutive path-discovery/DIRECT failure
        # counts and current DIRECT/CHANNEL outgoing queue depths.
        self.routing_health = None

        # Latency samples -- see the class docstring for what each measures.
        # Bounded deques rather than session-long averages so the reported
        # numbers track recent behavior rather than being diluted forever
        # by, e.g., a rough first few minutes while paths were resolving.
        self._rns_tx_latencies = collections.deque(maxlen=self.LATENCY_SAMPLES_MAX)
        self._meshcore_latencies = collections.deque(maxlen=self.LATENCY_SAMPLES_MAX)
        self._meshcore_latencies_by_peer = {}   # peer_key -> deque

    @staticmethod
    def _roll_bucket(now, bucket_start, bucket_count, peak):
        """If the current one-second bucket has fully elapsed, fold its
        count into the running peak and start a fresh bucket. Any fully
        idle buckets in between are implicitly zero and can't beat an
        existing peak, so there's no need to iterate one bucket at a time
        -- just re-anchor the window to now."""
        if now - bucket_start >= _SessionStats.BUCKET_SECONDS:
            peak = max(peak, bucket_count)
            bucket_start = now
            bucket_count = 0
        return bucket_start, bucket_count, peak

    def record_tx(self, count: int = 1) -> None:
        """Count `count` fragment(s) transmitted just now, toward the
        current one-second bucket (see peak_tx_fragments_per_sec)."""
        with self._lock:
            now = time.monotonic()
            self._tx_bucket_start, self._tx_bucket_count, self.peak_tx_fragments_per_sec = (
                self._roll_bucket(
                    now, self._tx_bucket_start, self._tx_bucket_count,
                    self.peak_tx_fragments_per_sec
                )
            )
            self._tx_bucket_count += count

    def record_rx(self, count: int = 1) -> None:
        """Count `count` fragment(s) received just now, toward the current
        one-second bucket (see peak_rx_fragments_per_sec)."""
        with self._lock:
            now = time.monotonic()
            self._rx_bucket_start, self._rx_bucket_count, self.peak_rx_fragments_per_sec = (
                self._roll_bucket(
                    now, self._rx_bucket_start, self._rx_bucket_count,
                    self.peak_rx_fragments_per_sec
                )
            )
            self._rx_bucket_count += count

    def record_flood_tx(self) -> None:
        """A fragment went out on CHANNEL -- i.e. actually flooded across
        the mesh, unlike a targeted DIRECT send."""
        self._flood_tx_window.record(1)

    def record_tx_bytes(self, n: int) -> None:
        """Add `n` RNS payload bytes to the session TX total and the
        trailing-window rate counter."""
        with self._lock:
            self.tx_bytes_total += n
        self._tx_bytes_window.record(n)

    def record_rx_bytes(self, n: int) -> None:
        """Add `n` RNS payload bytes to the session RX total and the
        trailing-window rate counter."""
        with self._lock:
            self.rx_bytes_total += n
        self._rx_bytes_window.record(n)

    def record_direct_result(self, success: bool, peer_key=None) -> None:
        """Record the final outcome of one DIRECT send, overall and (if
        peer_key is given) broken out per peer."""
        with self._lock:
            self._direct_send_total += 1
            if not success:
                self._direct_send_failed += 1
            if peer_key:
                entry = self._direct_by_peer.setdefault(peer_key, [0, 0])
                entry[0] += 1
                if not success:
                    entry[1] += 1

    def set_mesh_utilization(self, data: dict) -> None:
        """Store the latest polled MeshCore radio/packet stats snapshot."""
        with self._lock:
            self.mesh_utilization = data

    def record_outgoing_ptype(self, ptype_name: str) -> None:
        with self._lock:
            self._outgoing_ptype_counts[ptype_name] = (
                self._outgoing_ptype_counts.get(ptype_name, 0) + 1
            )

    def set_routing_health(self, data: dict) -> None:
        """Store a point-in-time routing-health snapshot pushed in by the
        interface (per-target path-discovery/DIRECT failure counts,
        current outgoing queue depths) -- see the routing_health field
        docstring above."""
        with self._lock:
            self.routing_health = data

    def get_current_tx_rate_bps(self) -> float:
        """Average TX bitrate (bits/sec) over the trailing RATE_WINDOW_S."""
        return self._tx_bytes_window.rate_per_sec() * 8.0

    def get_current_rx_rate_bps(self) -> float:
        """Average RX bitrate (bits/sec) over the trailing RATE_WINDOW_S."""
        return self._rx_bytes_window.rate_per_sec() * 8.0

    def get_flood_messages_last_minute(self) -> int:
        """Number of CHANNEL (flood) fragment sends in the trailing minute."""
        return int(self._flood_tx_window.total())

    def get_link_failure_rates(self) -> dict:
        """{peer_key: {"total": n, "failed": n, "failure_rate_pct": pct}}
        for every peer that's had at least one direct send attempted this
        session."""
        with self._lock:
            by_peer = {k: tuple(v) for k, v in self._direct_by_peer.items()}
        return {
            peer: {
                "total": total,
                "failed": failed,
                "failure_rate_pct": (failed / total * 100.0) if total else 0.0,
            }
            for peer, (total, failed) in by_peer.items()
        }

    def record_rns_tx_latency(self, seconds: float) -> None:
        """Record one RNS-level TX latency sample (queue-to-delivered)."""
        with self._lock:
            self._rns_tx_latencies.append(seconds)

    def record_meshcore_latency(self, seconds: float, peer_key=None) -> None:
        """Record one MeshCore-level DIRECT send+ACK round-trip sample,
        overall and (if peer_key is given) broken out per peer."""
        with self._lock:
            self._meshcore_latencies.append(seconds)
            if peer_key:
                dq = self._meshcore_latencies_by_peer.setdefault(
                    peer_key, collections.deque(maxlen=self.LATENCY_SAMPLES_MAX)
                )
                dq.append(seconds)

    @staticmethod
    def _latency_summary(samples) -> dict:
        """Reduce a list of latency samples (seconds) to count/avg/min/max."""
        if not samples:
            return {"count": 0, "avg_s": 0.0, "min_s": 0.0, "max_s": 0.0}
        return {
            "count": len(samples),
            "avg_s": sum(samples) / len(samples),
            "min_s": min(samples),
            "max_s": max(samples),
        }

    def get_rns_tx_latency(self) -> dict:
        """Summary (count/avg/min/max, in seconds) of RNS-level TX latency
        over the last LATENCY_SAMPLES_MAX packets."""
        with self._lock:
            samples = list(self._rns_tx_latencies)
        return self._latency_summary(samples)

    def get_meshcore_latency(self) -> dict:
        """Summary (count/avg/min/max, in seconds) of MeshCore-level DIRECT
        send+ACK round trips over the last LATENCY_SAMPLES_MAX fragments,
        across all peers."""
        with self._lock:
            samples = list(self._meshcore_latencies)
        return self._latency_summary(samples)

    def get_meshcore_latency_by_peer(self) -> dict:
        """Same as get_meshcore_latency(), broken out per peer -- useful
        since RTT varies a lot with hop count and link quality."""
        with self._lock:
            by_peer = {k: list(v) for k, v in self._meshcore_latencies_by_peer.items()}
        return {peer: self._latency_summary(s) for peer, s in by_peer.items()}

    def snapshot(self) -> dict:
        """Point-in-time view of all tracked stats. Folds in whatever's
        accumulated in the current, not-yet-elapsed fragment bucket so a
        burst right before a snapshot isn't missed."""
        with self._lock:
            peak_tx     = max(self.peak_tx_fragments_per_sec, self._tx_bucket_count)
            peak_rx     = max(self.peak_rx_fragments_per_sec, self._rx_bucket_count)
            total       = self._direct_send_total
            failed      = self._direct_send_failed
            uptime      = time.monotonic() - self._session_start
            tx_bytes    = self.tx_bytes_total
            rx_bytes    = self.rx_bytes_total
            mesh_util   = self.mesh_utilization
            ptype_counts = dict(self._outgoing_ptype_counts)
            routing_health = self.routing_health
        return {
            "uptime_s": uptime,
            "peak_tx_fragments_per_sec": peak_tx,
            "peak_rx_fragments_per_sec": peak_rx,
            "tx_bytes_total": tx_bytes,
            "rx_bytes_total": rx_bytes,
            "tx_rate_bps": self.get_current_tx_rate_bps(),
            "rx_rate_bps": self.get_current_rx_rate_bps(),
            "flood_messages_last_minute": self.get_flood_messages_last_minute(),
            "direct_send_total": total,
            "direct_send_failed": failed,
            "direct_failure_rate_pct": (failed / total * 100.0) if total else 0.0,
            "link_failure_rates": self.get_link_failure_rates(),
            "mesh_utilization": mesh_util,
            "rns_tx_latency": self.get_rns_tx_latency(),
            "meshcore_latency": self.get_meshcore_latency(),
            "meshcore_latency_by_peer": self.get_meshcore_latency_by_peer(),
            "outgoing_packet_type_counts": ptype_counts,
            "routing_health": routing_health,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Interface
# ─────────────────────────────────────────────────────────────────────────────

class MeshCore_Dynamic_Interface(Interface):

    # -------------------------------------------------------------------------
    # Class-level constants
    # -------------------------------------------------------------------------

    DEFAULT_IFAC_SIZE   = 8
    DEFAULT_IFAC_NAME   = ""
    DEFAULT_IFAC_NETKEY = b""

    MSG_PREFIX      = _PacketHandler.MSG_PREFIX

    BIND_PREFIX     = "RNSBIND:"
    BIND_REQ_PREFIX = "RNSBIND_REQ:"

    CAPABILITY_ROUTER = "R"
    CAPABILITY_EDGE   = "E"

    HEADER_SIZE      = _PacketHandler.HEADER_SIZE
    OUTQUEUE_MAXSIZE = 512
    SETUP_TIMEOUT_S  = 30

    BIND_BACKOFF_MIN   =  3.0
    BIND_BACKOFF_MAX   = 15.0
    BIND_HEARTBEAT_S   = 3600.0
    BIND_RESP_WINDOW_S = 60.0
    BIND_MAX_RETRIES   = 3

    # MeshCore companion-firmware constants for the telemetry-permission
    # grant (see _grant_telemetry_permission / auto_grant_telemetry_permission).
    # TELEM_MODE_ALLOW_FLAGS: answer a base-telemetry (and so path-discovery)
    # request only from contacts with the permission bit below set, rather
    # than TELEM_MODE_ALLOW_ALL (answer everyone) or the firmware default
    # TELEM_MODE_DENY (answer no one). The bit itself: firmware computes
    # `cp = contact.flags >> 1` then gates on `cp & TELEM_PERM_BASE` (0x01)
    # -- i.e. bit 1 (0x02) of the raw contact.flags byte (bit 0 is the
    # unrelated "favourite" flag) -- see MyMesh.cpp's onContactRequest.
    _TELEM_MODE_ALLOW_FLAGS     = 1
    _TELEM_FLAG_BASE_PERMISSION = 0x02

    _RNS_DST_LEN = 16
    _RNS_PTYPE_DATA     = 0x00
    _RNS_PTYPE_ANNOUNCE = 0x01
    _RNS_PTYPE_LINK_REQ = 0x02
    _RNS_PTYPE_PROOF    = 0x03

    _PTYPE_NAMES = {
        _RNS_PTYPE_DATA:     "DATA",
        _RNS_PTYPE_ANNOUNCE: "ANNOUNCE",
        _RNS_PTYPE_LINK_REQ: "LINK_REQ",
        _RNS_PTYPE_PROOF:    "PROOF",
    }

    _RNS_DTYPE_SINGLE = 0x00
    _RNS_DTYPE_GROUP  = 0x01
    _RNS_DTYPE_PLAIN  = 0x02
    _RNS_DTYPE_LINK   = 0x03

    # Outgoing queue priority tiers (lower value = dequeued first, see
    # queue.PriorityQueue in _init_runtime_state). LINK_REQUEST and PROOF packets are
    # small, latency-sensitive handshake/acknowledgement traffic -- RNS's own
    # Link establishment timeout is only a handful of seconds per hop, so if
    # one of these gets stuck behind a bulk data burst in a FIFO queue, RNS
    # gives up before the packet even goes out. Bumping them ahead of
    # ordinary DATA (and ANNOUNCE, which isn't latency-sensitive the same
    # way) fixes that without needing to send anything faster overall.
    _PRIORITY_HANDSHAKE = 0
    _PRIORITY_NORMAL    = 1

    _RNS_MAP_MAX = 512
    
    # Time window (seconds) to block processing duplicate packets that were fully reassembled.
    DEDUPLICATION_TTL_S = 30.0

    # Min gap between opportunistic RNSBIND_REQs fired for the same
    # still-unbound sender. Keeps a chatty unbound neighbor from causing a
    # REQ on every single fragment it emits.
    UNBOUND_REQ_RETRY_S = 120.0

    # Bounds on how much state we'll hold for senders we've observed traffic
    # from but haven't bound yet, so a burst of announces from strangers
    # can't grow these dicts without limit.
    _PENDING_TOKENS_MAX_SENDERS    = 64
    _PENDING_TOKENS_MAX_PER_SENDER = 16

    # Cap on distinct in-progress (sender, pkt_id) reassembly buffers.
    # _cleanup_stale_reassembly only runs every 30s (see _cleanup_loop), so
    # without a cap a nearby transmitter (malicious or just very noisy)
    # could open unbounded incomplete-fragment buffers in the gap between
    # cleanup passes -- each holding real payload bytes in memory. Anyone
    # can transmit on the shared LoRa channel, so this is reachable by any
    # RF source, not just a bound peer. Oldest-half eviction mirrors the
    # existing _RNS_MAP_MAX pattern.
    _ASSEMBLY_MAX_KEYS = 256

    # Cap on distinct bound peers. peer_ttl_s (how long an unheard-from
    # peer stays bound) defaults to 24h, so without a separate cap here
    # anyone transmitting forged RNSBIND messages with distinct fake
    # sender names/keys on the shared channel could grow _peer_table (and
    # its reverse-lookup/capability/last-seen dicts) without bound for
    # that entire window. Oldest-by-last-seen eviction, same pattern as
    # _ASSEMBLY_MAX_KEYS/_RNS_MAP_MAX.
    _PEER_TABLE_MAX_PEERS = 256

    # Cap on the sliding-window dedup record (_seen_pkts). Legitimate
    # traffic only ever has a handful of packet IDs in flight within
    # DEDUPLICATION_TTL_S, but nothing else stops a flood of fabricated
    # single-fragment packets (trivial to construct -- any valid Z85 text
    # with a 6+ byte decode and frag_total=1) from growing this dict
    # between the periodic cleanup passes.
    _SEEN_PKTS_MAX_KEYS = 1024

    # Cap and TTL for _sent_channel_fragments (heard-repeat/truncation
    # tracking, see _send_fragment / _process_tunnel_text). Only ever
    # written from our own sends, but capped for the same reason every
    # other tracking dict in this file is: defense in depth, not because
    # it's directly RF-exposed.
    _SENT_FRAGMENTS_MAX_KEYS = 512
    _SENT_FRAGMENTS_TTL_S    = 300.0

    # Window after sending a CHANNEL packet's first fragment to watch for
    # a flood_rx counter bump (see _check_heard_repeats) -- long enough to
    # span typical repeater rebroadcast jitter (firmware randomizes flood
    # retransmit delay per-hop; not a hard-guaranteed window, just a
    # practical default).
    HEARD_REPEATS_WINDOW_S = 6.0

    # Cap on _last_unbound_req (opportunistic-REQ throttle per unbound
    # sender name). Otherwise cleaned up only on the 24h peer_ttl_s window
    # like _peer_table, so it has the same unbounded-growth exposure to a
    # flood of fabricated sender names.
    _UNBOUND_REQ_MAX_SENDERS = 128

    # -------------------------------------------------------------------------
    # Constructor
    # -------------------------------------------------------------------------

    def __init__(self, owner, configuration):
        """Parse configuration into typed attributes (grouped into the
        _configure_* helpers below by concern), initialize internal runtime
        state, then start the interface's own asyncio event loop and block
        until async setup (_async_setup) either comes online or times out."""
        super().__init__()

        self.owner = owner
        self.name  = configuration.get("name", "MeshCore Dynamic")
        cfg        = configuration

        self._configure_connection(cfg)
        self._configure_channel(cfg)
        self._configure_radio(cfg)
        self._configure_fragmentation(cfg)
        self._configure_path_discovery_and_retry(cfg)
        self._configure_timeouts_and_rate_limits(cfg)
        self._configure_retransmission(cfg)
        self._configure_stale_fragment_dropping(cfg)
        self._configure_routing_and_debug(cfg)

        self._init_runtime_state()
        self._start_event_loop_and_wait()

    def _configure_connection(self, cfg) -> None:
        """Transport selection, connection parameters, and auto-reconnect."""
        self.transport = cfg.get("transport", "serial").lower()

        self.port     = cfg.get("port",     "/dev/ttyUSB0")
        self.baudrate = int(cfg.get("baudrate", 115200))
        self.host     = cfg.get("host",     "127.0.0.1")
        self.tcp_port = int(cfg.get("tcp_port", 4403))
        self.ble_name = cfg.get("ble_name", "")

        # The meshcore library's own connection manager can detect a dropped
        # serial/BLE/TCP link and transparently reconnect (CONNECTED/
        # DISCONNECTED events, see _on_mc_connected/_on_mc_disconnected below).
        # It's off by default in the library itself; we default it on here
        # since an unattended field radio should try to recover from a USB
        # re-enumeration or a brief BLE range loss rather than sitting dead
        # until rnsd is restarted.
        self.auto_reconnect = (
            cfg.get("auto_reconnect", "yes").lower() not in ("no", "false", "0")
        )
        self.max_reconnect_attempts = int(cfg.get("max_reconnect_attempts", 3))

    def _configure_channel(self, cfg) -> None:
        """MeshCore channel identity (idx/name/secret)."""
        # Defaults join a shared, public "RNSTunnel" channel so that two
        # nodes running this interface with no channel config at all can
        # find each other with zero coordination. This is deliberate, not
        # an oversight: RNS already encrypts and authenticates the actual
        # application data end-to-end, so a shared, publicly-known MeshCore
        # channel secret doesn't expose anything RNS-level -- it only
        # decides which MeshCore LoRa channel this radio joins, the same way
        # a WiFi SSID/password picks a network without implying anything
        # about what's encrypted on top of it. Set channel_idx/channel_name/
        # channel_secret explicitly to run your own private channel instead.
        self.channel_idx  = int(str(cfg.get("channel_idx", 0)).strip())
        self.channel_name = cfg.get("channel_name", "RNSTunnel")

        _raw_channel_secret     = cfg.get("channel_secret")
        self._using_default_channel_secret = _raw_channel_secret is None
        self.channel_secret_hex = (
            _raw_channel_secret
            if _raw_channel_secret is not None
            else "b99e9b45f61ab4bd4e355cf812711873"
        )

    def _configure_radio(self, cfg) -> None:
        """Optional radio parameter overrides, and how often to re-poll
        MeshCore's contact list."""
        self.radio_freq = float(cfg.get("freq", 0))
        self.radio_bw   = float(cfg.get("bw",   0))
        self.radio_sf   = int(cfg.get("sf",     0))
        self.radio_cr   = int(cfg.get("cr",     0))
        # This is a local query against the already-connected MeshCore
        # device (serial/BLE/TCP), not an over-the-air request -- it costs
        # no mesh airtime, so it's safe to poll fairly often for fresher
        # cached path info. 30s while performance is the priority during
        # development.
        self.contact_refresh_interval = float(cfg.get("contact_refresh_interval", 30.0))

    def _configure_fragmentation(self, cfg) -> None:
        """Fragment payload size, inter-fragment pacing, and DIRECT delivery
        ACK timeouts."""
        self.payload_size = int(cfg.get("payload_size", 64))

        # MeshCore firmware's actual per-message text-character ceiling
        # (MAX_TEXT_LEN = 10*CIPHER_BLOCK_SIZE = 160 in the reference
        # companion-radio firmware, confirmed directly against
        # src/helpers/BaseChatMesh.h/src/MeshCore.h), used by
        # _auto_payload_size to size fragments. Left configurable in case a
        # particular firmware build/transport genuinely needs a lower
        # value -- e.g. this was previously hardcoded to 128 based on an
        # earlier empirical observation that hasn't been re-validated
        # against every firmware build/BLE stack combination in the field.
        self.firmware_text_limit = int(cfg.get("firmware_text_limit", 160))
        # Pacing between CHANNEL (broadcast) fragments of the same packet --
        # this one does cost shared mesh airtime, so it's the one to relax
        # again once we're past the performance-focused development phase.
        self.fragment_delay_s = float(cfg.get("fragment_delay", 1.0))

        raw_dfd = cfg.get("direct_frag_delay", None)
        self.direct_frag_delay_s = float(raw_dfd) if raw_dfd is not None else 0.5

        # Minimum time to wait for a delivery ACK on a DIRECT send before
        # treating it as failed and falling back to CHANNEL. The radio also
        # hands back its own per-send "suggested_timeout" (based on path
        # length/airtime); we wait whichever of the two is longer.
        self.direct_ack_timeout_s = float(cfg.get("direct_ack_timeout", 4.0))

        # Ceilings on that wait. The firmware reports in MSG_SENT whether it
        # sent the fragment along a cached route or flooded it (no known
        # path), and its suggested_timeout scales with hop count: field logs
        # showed 10.9s for a 1-hop route and 13.7-21.3s for 3 hops, so a
        # single low ceiling declared delivered fragments failed on every
        # multi-hop route while the official client (which never caps) saw
        # them succeed. Routed sends therefore get a ceiling generous enough
        # to clear any realistic multi-hop estimate; only flood-mode sends,
        # where the firmware can suggest minutes, keep the short one -- our
        # CHANNEL fallback is a cheaper way to reach a peer with no path.
        self.direct_ack_timeout_max_s = float(cfg.get("direct_ack_timeout_max", 10.0))
        self.direct_ack_timeout_routed_max_s = float(
            cfg.get("direct_ack_timeout_routed_max", 45.0)
        )

    def _configure_path_discovery_and_retry(self, cfg) -> None:
        """Adaptive path-discovery backoff, and retry counts for both path
        discovery and DIRECT sends."""
        # Adaptive backoff for MeshCore path-discovery retries per peer.
        # base: cooldown after the first failure. max: ceiling regardless of
        # how many consecutive failures. factor: multiplier applied per
        # additional failure (base * factor**failures, capped at max).
        self._path_discovery_base_cooldown_s = float(cfg.get("path_discovery_base_cooldown", 15.0))
        self._path_discovery_max_cooldown_s  = float(cfg.get("path_discovery_max_cooldown", 900.0))
        self._path_discovery_backoff_factor  = float(cfg.get("path_discovery_backoff_factor", 2.0))

        # A single send_path_discovery_sync() attempt is a single flood-out-
        # and-wait-a-few-seconds round trip -- on a real half-duplex LoRa
        # link, losing that one broadcast (or its response) is completely
        # ordinary, and the outer exponential backoff above then delays the
        # next attempt by tens of seconds to minutes. The official MeshCore
        # client never has this problem because it retries a real message
        # send up to 3 times back-to-back (see send_msg_with_retry in the
        # meshcore library) before giving up. This gives discover_path() the
        # same quick-retry behavior instead of relying solely on the slow
        # outer backoff to recover from a single lost broadcast.
        self._path_discovery_quick_attempts = int(cfg.get("path_discovery_quick_attempts", 3))

        # Likewise, a DIRECT send is normally given exactly one ACK wait
        # before falling back to CHANNEL -- but a lost ACK on the return trip
        # doesn't mean the forward packet was lost, so retrying the DIRECT
        # send itself a couple of times is cheaper and less airtime-hungry
        # than immediately escalating to a broadcast CHANNEL resend.
        self.direct_send_attempts = int(cfg.get("direct_send_attempts", 3))

        # Number of consecutive fully-exhausted DIRECT sends (each already
        # having used up direct_send_attempts above) against the SAME cached
        # path before we give up trusting that path and reset it to flood
        # mode instead. Verified empirically: a path that's gone stale
        # (repeater repositioned, shorter route available) can fail 100% of
        # the time while remaining stuck in the contact table, and resetting
        # it to flood mode measurably outperforms continuing to retry it --
        # see reset_path usage in _async_outgoing_worker. Set to 0 to disable
        # (never auto-reset a cached path).
        self.direct_path_reset_threshold = int(cfg.get("direct_path_reset_threshold", 2))

        # reset_path() is irreversible -- it discards the cached path both
        # locally and on the MeshCore device's own persistent contact
        # record, and the only recovery afterward is a full flood-mode
        # path discovery, which is structurally the least reliable of
        # MeshCore's delivery mechanisms over multiple hops (every
        # intermediate repeater has to independently volunteer a
        # randomly-jittered rebroadcast, and firmware explicitly
        # deprioritizes flood traffic further at each hop -- unlike
        # routed/DIRECT forwarding, which designates exactly one relaying
        # node per hop at top priority). Resetting a path that's merely
        # degraded (moving out of range, temporary interference) rather
        # than genuinely stale (repeater physically moved) trades a
        # still-mostly-working unicast path for that worse-odds recovery
        # path, right when it's least likely to succeed.
        #
        # To tell those two cases apart, fall back on the RSSI this
        # interface already polls every ~60s (_poll_mesh_utilization):
        # if the local RF snapshot still looks reasonable, be
        # direct_path_reset_patience_multiplier times more patient before
        # resetting than direct_path_reset_threshold alone would be, on
        # the theory that a path failing despite decent RF conditions is
        # more likely stale-but-fine-to-abandon once retried a bit more;
        # if RSSI has dropped to/below direct_path_reset_rssi_floor (or no
        # reading is available yet), reset at the original threshold
        # unchanged, since that's the case the threshold was empirically
        # tuned against.
        self.direct_path_reset_rssi_floor = float(
            cfg.get("direct_path_reset_rssi_floor", -105.0)
        )
        self.direct_path_reset_patience_multiplier = float(
            cfg.get("direct_path_reset_patience_multiplier", 3.0)
        )

    def _configure_timeouts_and_rate_limits(self, cfg) -> None:
        """Reassembly timeout, optional bandwidth cap, and outgoing
        announce/path-request rate limiting."""
        # Default adjusted to 300s (5 minutes) for high-latency meshes
        self.fragment_timeout_s = float(cfg.get("fragment_timeout", 300.0))
        self.rate_limit_bps     = int(cfg.get("rate_limit", 0))

        self._announce_rate_s = float(cfg.get("outgoing_announce_rate", 600))
        self._path_req_rate_s = float(cfg.get("outgoing_path_req_rate", 1800))

        # RNS's own outbound retry logic fires a burst of path requests to the
        # same destination roughly 4-10s apart (typically ~4 attempts over
        # ~50s) before giving up. If outgoing_path_req_rate suppresses all but
        # the first of those, a single lost broadcast (common on lossy LoRa)
        # means the whole burst fails with no recovery for the full cooldown
        # period. path_req_burst_window lets RNS's own retry burst through
        # unthrottled; outgoing_path_req_rate only takes effect once the
        # burst window has elapsed, to stop genuine long-run spam.
        self._path_req_burst_window_s = float(cfg.get("path_req_burst_window", 60))

        # An incoming path request (DATA+PLAIN) for a destination we host or
        # know a path to causes RNS Transport to answer with a fresh outgoing
        # ANNOUNCE for that destination, almost immediately. That announce is
        # demand-driven and already naturally rate-limited by how often peers
        # ask -- it is NOT the kind of spontaneous re-announce that
        # outgoing_announce_rate exists to throttle. This window lets that
        # one response bypass the announce rate limiter so a routine
        # self-announce sent shortly before a path request doesn't cause the
        # response to be silently dropped. See processOutgoing().
        self._path_response_bypass_s = float(cfg.get("path_response_bypass_window", 15))

    def _configure_retransmission(self, cfg) -> None:
        """Blind retransmission for broadcast-only (CHANNEL-forced) packets."""
        # ANNOUNCE and path-request (DATA+PLAIN) packets can never use the
        # ACK'd DIRECT path -- they're always raw, unacknowledged CHANNEL
        # fragments (see _is_broadcast_packet / processOutgoing). Losing a
        # single fragment silently kills the whole reassembly with no retry.
        # These settings resend the SAME pkt_id and fragment set after a
        # jittered delay: the receiver's reassembly buffer accepts whichever
        # fragments arrive from either attempt (see _process_tunnel_text),
        # so a partial success on pass 1 plus a partial success on pass 2 can
        # still add up to a complete packet, rather than requiring one pass
        # to land end-to-end.
        #
        # Default is higher for announces than path requests: an announce is
        # usually a one-shot, application-scheduled event with no protocol-
        # level retry of its own. A path request already gets ~4 natural
        # retry attempts from RNS Transport itself roughly 4-10s apart (see
        # path_req_burst_window above), so extra retransmission here is
        # additive on top of that and defaults to off -- enable it only if
        # you're seeing path resolution fail even within that natural burst.
        self.announce_retransmit_extra = int(cfg.get("announce_retransmit_extra", 0))
        self.path_req_retransmit_extra = int(cfg.get("path_req_retransmit_extra", 0))
        self.retransmit_jitter_min_s   = float(cfg.get("retransmit_jitter_min", 8.0))
        self.retransmit_jitter_max_s   = float(cfg.get("retransmit_jitter_max", 20.0))
        self.ordinary_data_retransmit_extra = int(cfg.get("ordinary_data_retransmit_extra", 0))

    def _configure_stale_fragment_dropping(self, cfg) -> None:
        """When to give up on and drop an outgoing fragment instead of
        sending it -- see the outgoing-queue-behavior note in the module
        docstring."""
        # A fragment is dropped instead of transmitted only when BOTH of the
        # following hold: it has sat in its outgoing queue longer than
        # stale_fragment_max_age, AND the queue is still backed up behind it
        # (at least stale_fragment_min_queue_depth items waiting). Age alone
        # isn't a good enough signal -- a fragment that's simply had bad luck
        # on an otherwise quiet queue will get sent in a moment regardless,
        # and dropping it would gain nothing. It's only worth sacrificing a
        # fragment when there's an actual backlog to relieve behind it.
        # RNS's own Link establishment timeout is typically a few multiples
        # of DEFAULT_PER_HOP_TIMEOUT (6s/hop) -- well under a minute for
        # anything but a many-hop path -- so a fragment still unsent after
        # stale_fragment_max_age, with a real backlog behind it, is very
        # unlikely to still be wanted, and sending it anyway only delays
        # everything queued behind it further. Broadcast packets (ANNOUNCE /
        # path request) are exempt regardless: they have no single
        # "requester" to give up, and a late one is still useful
        # network-wide, unlike a stale reply/data fragment tied to one
        # already-abandoned Link. Set stale_fragment_max_age to 0 to disable
        # this entirely.
        self.stale_fragment_max_age_s = float(cfg.get("stale_fragment_max_age", 30.0))
        self.stale_fragment_min_queue_depth = int(cfg.get("stale_fragment_min_queue_depth", 10))

    def _configure_routing_and_debug(self, cfg) -> None:
        """Routing capability, RNS-core interface-contract attributes
        (can_route, bitrate), and interface-local debug logging."""
        self.can_route = (
            cfg.get("can_route", "yes").lower() not in ("no", "false", "0")
        )

        self.allow_direct = (
            cfg.get("allow_direct", "yes").lower() not in ("no", "false", "0")
        )

        self.peer_ttl_s = float(cfg.get("peer_ttl", 86400))

        # RNS-core interface-contract attribute (not just internal
        # bookkeeping): RNS.Transport reads interface.bitrate directly to
        # pace outgoing announces and to estimate per-hop/Link-establishment
        # timeouts (tx_time = packet_bits / bitrate). Deliberately
        # conservative relative to raw LoRa PHY rates -- our real achievable
        # throughput is far lower once Z85 overhead, fragment pacing, and
        # ACK round trips are accounted for, and understating it keeps RNS
        # patient (longer computed timeouts) rather than giving up on Links
        # or path requests too quickly over a slow link.
        self.bitrate = int(cfg.get("bitrate", 300))

        # Per-interface debug logging, independent of RNS core's global
        # [logging] loglevel. RNS.log() gates every message (ours and RNS
        # core's own) on a single global level, so raising it to DEBUG (6)
        # to see this interface's own diagnostic logs also turns on RNS
        # core's own debug firehose. Messages logged via self._debug() below
        # are emitted at LOG_INFO -- gated only by this flag -- so they show
        # up under the normal loglevel = 4 default without any core noise.
        self.debug_logs = str(cfg.get("debug_level", "info")).strip().lower() == "debug"

        # RNS core checks `interface.HW_MTU + (interface.ifac_size or 0)` against
        # every inbound packet before it's handed anywhere else — every custom
        # interface must set both or Transport.preprocess_inbound() throws. This
        # is the max size of a single *fully reassembled* RNS packet this
        # interface can carry, not the per-fragment LoRa payload size
        # (self.payload_size handles that).
        self.HW_MTU = RNS.Reticulum.MTU

        # This interface's LoRa channel airtime is shared with every other
        # node listening on it, including non-RNS MeshCore users -- exactly
        # the case this flag exists to describe (every other broadcast/
        # half-duplex interface in RNS core, e.g. SerialInterface,
        # RNodeInterface, KISSInterface, sets it too). Traced against RNS
        # core: nothing currently reads it back, so this is purely correct
        # self-description for now, not a behavior change.
        self.shared_medium = True

        # Enables the r_stat_rssi/r_stat_snr reporting set in
        # _deliver_reassembled_packet -- RNS.Transport only copies those
        # onto an inbound packet (for rnstatus/logging; confirmed it plays
        # no part in any routing/retry/timeout decision) when this is True.
        self.reports_phy_stats = True

        # MeshCore's path-discovery command is, per the firmware source
        # itself ("'Path Discovery' is just a special case of flood +
        # Telemetry req"), secretly a base-telemetry request -- and the
        # firmware silently declines to answer ANY such request at all
        # unless the responding node's telemetry_mode_base preference
        # allows it, which defaults to DENY. Rather than opening that to
        # every MeshCore user in radio range (TELEM_MODE_ALLOW_ALL), this
        # uses MeshCore's per-contact ALLOW_FLAGS mode and grants the
        # permission bit only to peers who've proven they know this
        # channel's secret via a real RNSBIND/RNSBIND_REQ (see
        # _grant_telemetry_permission, called from _handle_bind) --
        # confirmed RNS-tunnel peers, not every device on the shared
        # channel. Set to no if you don't want this node answering base
        # telemetry (battery voltage, MCU temperature) requests from
        # anyone, accepting that path discovery to/from it will then never
        # succeed via that mechanism.
        self.auto_grant_telemetry_permission = (
            cfg.get("auto_grant_telemetry_permission", "yes").lower()
            not in ("no", "false", "0")
        )

    def _init_runtime_state(self) -> None:
        """Initialize all internal mutable state (queues, locks, caches,
        rate-limiter/peer bookkeeping) ahead of async setup. Pure
        initialization -- no config parsing, no I/O."""
        # --- Internal async / threading state ------------------------------
        self._mc          = None
        self._EventType   = None
        self._loop        = None
        self._loop_thread = None

        # Thread-safe queues used to decouple synchronous execution from the
        # worker loops. DIRECT and CHANNEL traffic get independent queues (and
        # independent worker tasks, see _async_outgoing_worker) so a DIRECT
        # send blocked waiting on a delivery ACK (up to
        # direct_ack_timeout_routed_max_s) can never stall CHANNEL broadcasts
        # queued behind it, and vice versa. Only the command round trip
        # itself is serialized across both (see _install_command_serializer).
        #
        # Both are PriorityQueues rather than plain FIFOs: LINK_REQUEST and
        # PROOF packets (see _PRIORITY_HANDSHAKE below) jump ahead of ordinary
        # DATA already waiting in line. Without this, a link establishment
        # attempt that arrives mid-burst of a bulk transfer would sit behind
        # the entire backlog -- easily the multi-minute waits seen during
        # testing -- and RNS would very likely give up on the Link before its
        # request packet ever went out. Items are (priority, seq, payload)
        # tuples; seq is a unique, always-comparable tie-breaker (see
        # _outqueue_seq) so same-priority items still drain in FIFO order
        # and Python's tuple comparison never has to fall through to payload
        # fields that might not be mutually comparable (e.g. pkt_id can be
        # None for retransmits).
        self._direct_outqueue  = queue.PriorityQueue(maxsize=self.OUTQUEUE_MAXSIZE)
        self._channel_outqueue = queue.PriorityQueue(maxsize=self.OUTQUEUE_MAXSIZE)
        self._outqueue_seq = itertools.count()

        # In-memory benchmarking counters for comparing versions/configs
        # across test runs -- see _SessionStats. Purely diagnostic, not used
        # for any routing/behavioral decisions (yet -- see
        # _stats_summary_loop).
        self.stats = _SessionStats()
        self._last_mesh_poll = None   # (monotonic_ts, data dict) or None

        # Tracks in-flight RNS packets for RNS-level TX latency: pkt_id ->
        # {"start": monotonic_ts, "remaining": fragments not yet finally
        # accounted for}. "Finally accounted for" means sent (and ACK'd if
        # direct), or lost for good with nothing further in flight -- see
        # _mark_pkt_fragment_done. Retransmit-originated fragments carry
        # pkt_id=None and are deliberately excluded (see _delayed_retransmits).
        self._pkt_send_tracking = {}
        self._pkt_lock = threading.Lock()

        self._own_node_name = ""
        self._own_mc_key    = ""

        self._pkt_id      = 0
        self._pkt_id_lock = threading.Lock()

        self._assembly      = {}
        self._assembly_meta = {}
        self._asm_lock      = threading.Lock()

        # Sliding time-window cache: (sender, pkt_id) -> expiration_monotonic_timestamp
        self._seen_pkts = {}
        self._seen_lock = threading.Lock()

        # Recently-sent CHANNEL fragments, so that hearing our own message
        # come back (relayed by a nearby repeater) can be logged and
        # checked for truncation, instead of being silently dropped as a
        # self-echo. (pkt_id, frag_idx) -> (sent_char_len, sent_monotonic_ts).
        # Only ever populated by our own sends, so naturally bounded by our
        # own send rate rather than exposed to arbitrary RF input -- still
        # capped defensively and swept on the usual TTL, matching the
        # pattern used for _seen_pkts.
        self._sent_channel_fragments = {}
        self._sent_channel_fragments_lock = threading.Lock()

        self._peer_table     = {}
        self._reverse_peers  = {}
        self._peer_last_seen = {}
        self._peer_caps      = {}
        self._rns_to_mc_map  = {}
        self._peer_lock      = threading.Lock()

        # On-disk cache of confirmed (RNSBIND-learned) peer name<->pubkey
        # bindings, so a plain rnsd restart can skip straight past the
        # RNSBIND_REQ broadcast/backoff cycle for peers the MeshCore device
        # itself still corroborates -- see _load_peer_cache/_save_peer_cache.
        # Left None (persistence silently disabled) if RNS core hasn't set
        # up a storage path for some reason, rather than failing setup over
        # a purely best-effort optimization.
        try:
            safe_name = "".join(
                c if (c.isalnum() or c in ("-", "_")) else "_" for c in self.name
            )
            self._peer_cache_path = os.path.join(
                RNS.Reticulum.storagepath, f"meshcore_dynamic_peers_{safe_name}.json"
            ) if RNS.Reticulum.storagepath else None
        except Exception:
            self._peer_cache_path = None

        # mc_pubkey -> (out_path_len, out_path) last seen for that peer, so
        # we can log when MeshCore's own idea of the path actually changes
        # rather than just what we assume it is. Fed both by contact-update
        # events (NEW_CONTACT/CONTACTS/CONTACTS_FULL/PATH_UPDATE/ADVERTISEMENT
        # -- see _bind_meshcore_contact) and by the periodic contact refresh
        # loop, since ensure_contacts()/get_contacts() dispatches a CONTACTS
        # event that flows through the same handler.
        self._peer_last_path = {}

        # sender_name -> set of RNS tokens observed from them before their
        # MeshCore pubkey was known (RNSBIND not yet complete). Backfilled
        # into _rns_to_mc_map the moment _handle_bind learns that sender's
        # key -- see _process_tunnel_text() and _handle_bind().
        self._pending_tokens      = {}
        self._pending_tokens_lock = threading.Lock()

        # sender_name -> monotonic timestamp of the last opportunistic
        # RNSBIND_REQ fired on their behalf, so we don't spam the channel.
        self._last_unbound_req      = {}
        self._last_unbound_req_lock = threading.Lock()

        self._announce_sent_times = {}
        self._announce_sent_lock  = threading.Lock()
        self._path_req_sent_times = {}
        self._path_req_sent_lock  = threading.Lock()

        # dest_id -> monotonic expiry. Set when we observe an inbound path
        # request; consulted (and consumed) by the outgoing announce rate
        # limiter to bypass it for the resulting path-response announce.
        self._path_response_pending = {}
        self._path_response_pending_lock = threading.Lock()

        self._has_direct_api    = False
        self._pending_resp_task = None

        self._setup_done = threading.Event()
        self._load_meshcore_or_panic()

        #cache of timestamps for the last path request sent to each destination, used to enforce outgoing_path_req_rate
        self._path_req_timestamps = {}

        # Adaptive backoff for path discovery, keyed by target MeshCore key.
        # A peer that resolves cleanly stays at the base cooldown. A peer
        # whose discovery keeps failing (e.g. an asymmetric RF hop through a
        # repeater -- one direction works, the other never resolves a path)
        # backs off exponentially so we stop hammering a link that isn't
        # going to answer, while still periodically re-checking in case
        # conditions change (repeater repositioned, interference clears,
        # etc). Reset to the base cooldown the moment discovery succeeds.
        # (base/max/factor are set from config in _configure_path_discovery_and_retry)
        self._path_req_failures = {}   # target_key -> consecutive failure count
        self._path_req_lock     = threading.Lock()

        # Separate from the above: tracks consecutive DIRECT send failures
        # against a peer's CURRENTLY CACHED path specifically (not path
        # discovery attempts). Verified empirically that a cached path can
        # go stale (repeater repositioned, shorter route now exists) while
        # remaining stuck in the contact table, and that repeatedly retrying
        # a stale path fails far more often than just resetting it back to
        # flood mode and letting the firmware find whatever route currently
        # works. Reset to 0 the moment a DIRECT send succeeds. See
        # direct_path_reset_threshold (_configure_path_discovery_and_retry).
        self._direct_path_failures = {}   # target_key -> consecutive DIRECT failure count

        # Delivery-ACK matching for DIRECT sends. Both dicts are only ever
        # touched from this interface's own event loop, so no lock.
        #   _pending_acks: expected_ack code -> (future, attempt_start) for
        #     every attempt of every fragment currently awaiting delivery;
        #     all attempts of one fragment share one future, so an ACK for
        #     an EARLIER attempt (arriving after that attempt's wait expired)
        #     still resolves the fragment as delivered.
        #   _recent_acks: code -> monotonic time of every ACK heard lately,
        #     so an ACK that beats the registration of its code is not lost.
        self._pending_acks = {}
        self._recent_acks  = {}

    def _start_event_loop_and_wait(self) -> None:
        """Start this interface's own asyncio event loop on a dedicated
        background thread, schedule _async_setup() onto it, and block the
        calling (RNS core) thread until setup either completes or times out
        (SETUP_TIMEOUT_S) -- RNS expects interface construction to be
        synchronous, so this is the bridge between that and our async
        MeshCore driver."""
        self._loop = asyncio.new_event_loop()
        assert self._loop is not None
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True,
            name=f"MCDyn-loop-{self.name}"
        )
        self._loop_thread.start()

        _setup_future = asyncio.run_coroutine_threadsafe(
            self._async_setup(), self._loop
        )

        def _on_setup_done(fut):
            # If _async_setup() raised, it never reached self._setup_done.set()
            # itself -- do that here (after logging the exception) so the
            # wait() below doesn't just block for the full SETUP_TIMEOUT_S.
            if fut.done() and not fut.cancelled():
                exc = fut.exception()
                if exc is not None:
                    import traceback
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"Setup exception: {exc}", RNS.LOG_ERROR
                    )
                    RNS.log(
                        "".join(traceback.format_exception(
                            type(exc), exc, exc.__traceback__
                        )),
                        RNS.LOG_ERROR
                    )
                    self._setup_done.set()

        _setup_future.add_done_callback(_on_setup_done)

        if not self._setup_done.wait(timeout=self.SETUP_TIMEOUT_S):
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: Setup timed out.",
                RNS.LOG_ERROR
            )
        elif not self.online:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: Initialization failed.",
                RNS.LOG_ERROR
            )

    # -------------------------------------------------------------------------
    # Startup helpers
    # -------------------------------------------------------------------------

    def _debug(self, msg: str) -> None:
        """Interface-local debug log, gated by debug_level = debug in the
        interface config rather than RNS core's global loglevel. Emitted at
        LOG_INFO so it shows up under the standard loglevel = 4 without
        needing to raise the global level (which would also enable RNS
        core's own DEBUG output)."""
        if self.debug_logs:
            RNS.log(f"MeshCore_Dynamic_Interface [{self.name}]: {msg}", RNS.LOG_INFO)

    def _log_scheduled_task_exceptions(self, fut, description: str) -> None:
        """Done-callback for a fire-and-forget asyncio.run_coroutine_threadsafe
        future: logs any exception the scheduled coroutine raised instead of
        letting it disappear silently (the default outcome for a future
        whose result/exception is never retrieved)."""
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Background task '{description}' raised: {exc}",
                RNS.LOG_WARNING
            )

    def _auto_payload_size(self):
        """Once our own node name is known, shrink/grow payload_size so a
        fully Z85-encoded fragment (header + payload + "RNS:" prefix, plus
        the node name MeshCore prepends on relay) never exceeds the
        firmware's channel-message character limit. See the module
        docstring's PAYLOAD SIZE section for the derivation. No-op (returns
        the configured payload_size unchanged) until the node name is known."""
        if self._own_node_name:
            firmware_limit = 120 # TODO: Verify
            # Safety margin for firmware variations, and for the reference
            # firmware's own behavior of shrinking its text-length ceiling
            # by 2 more characters once a DIRECT message reaches its 4th+
            # send attempt (see composeMsgPacket/MAX_TEXT_LEN-2 in
            # MeshCore-main's BaseChatMesh.cpp).
            margin = 4
            budget         = firmware_limit - len(self._own_node_name) - 2
            # Z85 inverse: 1 pad-count char + 5 chars per 4 raw bytes, so
            # floor((budget-1 pad char)/5) groups of 4 raw bytes each.
            max_payload    = (((budget - 5) // 5) * 4 - self.HEADER_SIZE) - margin

            if max_payload <= 0:
                # A node name this long relative to firmware_text_limit
                # leaves no room for any payload at all -- not even a
                # single Z85 group. Clamping to 0 here matters: _PacketHandler
                # silently falls back to a hardcoded 64-byte default for any
                # payload_size <= 0, which would be larger than what
                # actually fits and produce fragments the firmware silently
                # truncates -- exactly what this function exists to
                # prevent. Clamp to the smallest usable size instead and
                # warn loudly, since 4 is still almost certainly too small
                # to be practically useful.
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"Node name ({len(self._own_node_name)} chars) leaves no "
                    f"usable payload budget at firmware_text_limit="
                    f"{firmware_limit} (computed max_payload={max_payload}). "
                    f"Clamping payload_size to 4 bytes/fragment -- "
                    f"shorten the node name or raise firmware_text_limit if "
                    f"that's actually safe for your firmware.",
                    RNS.LOG_WARNING
                )
                max_payload = 4

            if max_payload == self.payload_size:
                return self.payload_size

            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Auto-adjusting payload_size from {self.payload_size} "
                f"to {max_payload} due to node name length.",
                RNS.LOG_INFO
            )

            self.payload_size = max_payload
            return max_payload

        return self.payload_size
    
    def _load_meshcore_or_panic(self):
        """Import the meshcore library, or panic the whole RNS instance if
        it's not installed -- there's no usable fallback."""
        try:
            import meshcore as _mc_mod
            self._mc_module = _mc_mod
            self._EventType = _mc_mod.EventType
        except ImportError:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"meshcore library not found — cannot continue.",
                RNS.LOG_CRITICAL
            )
            self.owner.panic()

    def _run_loop(self):
        """Entry point for the dedicated background thread that owns this
        interface's asyncio event loop; runs it until the interface is torn
        down."""
        if self._loop is None:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: Loop crashed: no event loop",
                RNS.LOG_ERROR
            )
            return

        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: Loop crashed: {exc}",
                RNS.LOG_ERROR
            )

    async def _async_setup(self):
        """Asynchronous continuation of __init__ (runs on this interface's
        own event loop -- see _start_event_loop_and_wait): connects to the
        configured transport, fetches node identity, applies radio/channel
        config, detects DIRECT messaging support, subscribes to every event
        this interface cares about, and starts its background tasks. Each
        step below is its own _setup_* method. An early return here (only
        on transport connect failure) leaves self.online False, which
        _start_event_loop_and_wait treats as a startup failure."""
        MeshCore = self._mc_module.MeshCore
        ET       = self._EventType

        if not await self._setup_connect_transport(MeshCore, ET):
            return

        self._install_command_serializer()
        await self._setup_fetch_identity()
        await self._setup_apply_radio_overrides()
        await self._setup_configure_channel()
        await self._setup_detect_direct_api()
        await self._load_peer_cache()
        await self._setup_configure_telemetry_permissions()
        self._setup_subscribe_contact_and_message_events(ET)
        self._setup_subscribe_lifecycle_events(ET)
        await self._setup_start_background_tasks()

        self.online = True
        self._setup_done.set()

        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"Interface ready -- transport={self.transport} "
            f"can_route={self.can_route} allow_direct={self.allow_direct} "
            f"direct_api={self._has_direct_api} payload_size={self.payload_size} "
            f"peer_ttl={self.peer_ttl_s:.0f}s auto_reconnect={self.auto_reconnect}"
            f"{f'({self.max_reconnect_attempts} attempts)' if self.auto_reconnect else ''} "
            f"debug_logs={self.debug_logs}.",
            RNS.LOG_INFO
        )

    async def _setup_connect_transport(self, MeshCore, ET) -> bool:
        """Connect to the configured transport (serial/BLE/TCP) and verify
        the MeshCore driver came up with a usable instance and EventType
        enum. Returns False (after logging why) if setup should abort."""
        try:
            if self.transport == "serial":
                self._mc = await MeshCore.create_serial(
                    self.port, self.baudrate,
                    auto_reconnect=self.auto_reconnect,
                    max_reconnect_attempts=self.max_reconnect_attempts,
                )
            elif self.transport == "ble":
                self._mc = await MeshCore.create_ble(
                    self.ble_name or None,
                    auto_reconnect=self.auto_reconnect,
                    max_reconnect_attempts=self.max_reconnect_attempts,
                )
            elif self.transport == "tcp":
                self._mc = await MeshCore.create_tcp(
                    self.host, self.tcp_port,
                    auto_reconnect=self.auto_reconnect,
                    max_reconnect_attempts=self.max_reconnect_attempts,
                )
            else:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"Unknown transport '{self.transport}'.", RNS.LOG_ERROR
                )
                return False
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Driver init error: {exc}", RNS.LOG_ERROR
            )
            return False

        if self.transport == "serial":
            conn_desc = f"serial port={self.port} baudrate={self.baudrate}"
        elif self.transport == "ble":
            conn_desc = f"ble name={self.ble_name or '<first found>'}"
        else:
            conn_desc = f"tcp host={self.host} port={self.tcp_port}"
        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"Connected to MeshCore device ({conn_desc}).",
            RNS.LOG_INFO
        )

        if self._mc is None:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Driver init returned no MeshCore instance.",
                RNS.LOG_ERROR
            )
            return False

        if ET is None:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"MeshCore EventType is unavailable.",
                RNS.LOG_ERROR
            )
            return False

        return True

    def _install_command_serializer(self) -> None:
        """Serialize every command sent to the radio through one lock.

        The meshcore library's CommandHandler.send() has no locking and
        matches a command's reply by event TYPE alone: it subscribes to the
        expected types, writes the frame, and returns the first such event
        the dispatcher fans out -- to every subscriber. Two in-flight
        commands expecting the same type (send_msg and send_path_discovery
        both wait on MSG_SENT; nearly everything accepts ERROR) are both
        handed whichever reply the radio emits first. Field logs showed a
        DIRECT send adopting a path-discovery request tag as its
        expected_ack (and the discovery adopting the message's reply), so
        neither could ever complete. Wrapping send() itself, rather than each
        call site, also covers the library's own internal commands (auto
        message fetching, ensure_contacts). Only the command round trip is
        held; delivery-ACK waits happen outside the lock."""
        assert self._mc is not None
        commands = self._mc.commands
        original_send = commands.send
        if getattr(original_send, "_rns_serialized", False):
            return
        lock = asyncio.Lock()

        async def serialized_send(*args, **kwargs):
            async with lock:
                return await original_send(*args, **kwargs)

        setattr(serialized_send, "_rns_serialized", True)
        commands.send = serialized_send

    async def _setup_fetch_identity(self) -> None:
        """Fetch this node's own name/pubkey via send_appstart() -- needed
        for RNSBIND announcements and payload_size auto-adjustment."""
        if self._mc is None:
            return
        try:
            result = await self._mc.commands.send_appstart()
            if self._EventType and result.type == self._EventType.SELF_INFO:
                self._own_node_name = result.payload.get("name", "")
                self._own_mc_key    = result.payload.get("public_key", "")
                cap_label = (
                    "router" if self.can_route else "edge (no upstream routing)"
                )
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"Node identity: '{self._own_node_name}' "
                    f"key={self._own_mc_key[:16]}... [{cap_label}]",
                    RNS.LOG_INFO
                )
            else:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"send_appstart() returned unexpected event type "
                    f"{getattr(result, 'type', result)!r} instead of SELF_INFO -- "
                    f"node name/key remain unknown, payload_size auto-adjust "
                    f"and RNSBIND announcements will not work until this resolves.",
                    RNS.LOG_WARNING
                )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Identity fetch failed: {exc}", RNS.LOG_WARNING
            )

    async def _setup_apply_radio_overrides(self) -> None:
        """Apply freq/bw/sf/cr radio overrides if all four are configured;
        otherwise leave the node's currently stored radio settings alone."""
        if self._mc is None:
            return
        if self.radio_freq and self.radio_bw and self.radio_sf and self.radio_cr:
            try:
                await self._mc.commands.set_radio(
                    self.radio_freq, self.radio_bw, self.radio_sf, self.radio_cr
                )
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"Radio override applied: freq={self.radio_freq}MHz "
                    f"bw={self.radio_bw}kHz sf={self.radio_sf} cr={self.radio_cr}.",
                    RNS.LOG_INFO
                )
            except Exception as exc:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"Radio override failed: {exc} -- continuing with the "
                    f"node's currently stored radio settings.",
                    RNS.LOG_WARNING
                )
        else:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"No radio override configured -- using the node's currently "
                f"stored radio settings.",
                RNS.LOG_INFO
            )

    async def _setup_configure_channel(self) -> None:
        """Set the MeshCore channel idx/name/secret this interface tunnels
        RNS traffic over."""
        if self._mc is None:
            return
        try:
            secret_bytes = bytes.fromhex(self.channel_secret_hex)
            await self._mc.commands.set_channel(
                self.channel_idx, self.channel_name, secret_bytes
            )
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Channel configured: idx={self.channel_idx} name='{self.channel_name}'.",
                RNS.LOG_INFO
            )
            if self._using_default_channel_secret:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"No channel_secret configured -- using the shared default "
                    f"channel so nodes can find each other with zero setup. "
                    f"This is fine for RNS traffic (it's already encrypted "
                    f"end-to-end), but means this radio's MeshCore-level "
                    f"traffic shares airtime/visibility with any other "
                    f"default-config node in range. Set channel_idx/"
                    f"channel_name/channel_secret explicitly for a private "
                    f"channel.",
                    RNS.LOG_INFO
                )
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Channel init error: {exc}", RNS.LOG_WARNING
            )

    async def _setup_detect_direct_api(self) -> None:
        """Determine whether DIRECT (unicast) messaging is usable: allowed
        by config, exposed by the meshcore library, and (if so) prime the
        local contact cache."""
        if self._mc is None:
            return
        if self.allow_direct:
            self._has_direct_api = hasattr(self._mc.commands, "send_msg")
            if self._has_direct_api:
                # Keep the local contact cache (self._mc.contacts) populated and
                # current -- this rides the same connection rnsd already owns,
                # it's not a second client. auto_update_contacts re-fetches
                # automatically whenever the firmware reports a path change,
                # so out_path_len is always fresh when we need to log it.
                try:
                    self._mc.auto_update_contacts = True
                    await self._mc.ensure_contacts()
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"Initial contact fetch OK ({len(self._mc.contacts)} "
                        f"contact(s) known).",
                        RNS.LOG_INFO
                    )
                except Exception as exc:
                    self._debug(f"Initial contact fetch failed: {exc}")
            else:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"meshcore library exposes no send_msg() command -- "
                    f"DIRECT sends are unavailable, all outgoing traffic "
                    f"will use CHANNEL.",
                    RNS.LOG_WARNING
                )
        else:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"allow_direct=no -- DIRECT sends disabled by config, all "
                f"outgoing traffic will use CHANNEL.",
                RNS.LOG_INFO
            )

    async def _setup_configure_telemetry_permissions(self) -> None:
        """Switch this node's own telemetry_mode_base preference to
        TELEM_MODE_ALLOW_FLAGS (per-contact, via _grant_telemetry_permission)
        instead of the firmware default TELEM_MODE_DENY. See
        auto_grant_telemetry_permission's config docstring for why this
        matters: without it, this node's firmware silently refuses to
        answer ANY path-discovery request (which is secretly a base-
        telemetry request), regardless of RF conditions -- confirmed
        directly against real hardware in the field. A local, no-mesh-
        airtime command; best-effort since it's a one-time nicety, not a
        setup precondition."""
        if not self.auto_grant_telemetry_permission or self._mc is None:
            return
        try:
            res = await self._mc.commands.set_telemetry_mode_base(
                self._TELEM_MODE_ALLOW_FLAGS
            )
            if res is not None and self._EventType is not None and res.type == self._EventType.ERROR:
                self._debug(f"Failed to set telemetry_mode_base: {res.payload}")
            else:
                self._debug(
                    "telemetry_mode_base set to ALLOW_FLAGS -- base telemetry "
                    "(and so path discovery) will be answered only for peers "
                    "granted the permission bit via a confirmed RNSBIND "
                    "(see _grant_telemetry_permission)."
                )
        except Exception as exc:
            self._debug(f"Failed to set telemetry_mode_base: {exc}")

    async def _grant_telemetry_permission(self, mc_pubkey: str) -> None:
        """Grant this confirmed RNS-tunnel peer permission to receive our
        base telemetry (battery voltage, MCU temperature) -- MeshCore's
        path-discovery command is, per the firmware source itself, secretly
        a base-telemetry request, and the firmware silently declines to
        answer it at all unless the responding node's telemetry_mode_base
        preference allows it (see _setup_configure_telemetry_permissions).
        Rather than opening that to every contact (TELEM_MODE_ALLOW_ALL),
        this uses MeshCore's per-contact ALLOW_FLAGS mode and only grants
        the permission bit to peers who've proven they know this channel's
        secret via a real RNSBIND/RNSBIND_REQ -- i.e. confirmed RNS-tunnel
        peers, not every MeshCore user in radio range. Idempotent: only
        issues the (local, no-mesh-airtime) update command if the bit
        isn't already set, and only ever called from _handle_bind, never
        from a bare contact-table event (which has no comparable proof of
        channel-secret knowledge)."""
        if not self.auto_grant_telemetry_permission or self._mc is None:
            return
        try:
            contact = self._mc.get_contact_by_key_prefix(mc_pubkey)
            if contact is None:
                return
            current_flags = contact.get("flags", 0) or 0
            if current_flags & self._TELEM_FLAG_BASE_PERMISSION:
                return
            await self._mc.commands.change_contact_flags(
                contact, current_flags | self._TELEM_FLAG_BASE_PERMISSION
            )
            self._debug(
                f"Granted base-telemetry permission to {mc_pubkey[:16]}... "
                f"(needed for MeshCore path discovery to work with this peer)."
            )
        except Exception as exc:
            self._debug(
                f"Failed to grant telemetry permission to {mc_pubkey[:16]}...: {exc}"
            )

    async def _load_peer_cache(self) -> None:
        """Seed _peer_table from a previous session's confirmed RNS
        bindings (see _save_peer_cache), so a plain rnsd restart doesn't
        have to sit through a fresh RNSBIND_REQ broadcast/backoff cycle
        (up to BIND_MAX_RETRIES * BIND_RESP_WINDOW_S) before it can send
        anything -- purely because the RNS-name <-> MeshCore-pubkey binding
        normally lives only in this process's memory, even though the
        MeshCore device's own contact table (and any cached path to it)
        already survived the restart untouched.

        Each cached entry is only trusted if the device's own contact
        table still has a live contact for that pubkey right now -- the
        cache file can go stale between runs (the peer's device could be
        factory-reset/re-paired, or simply drop out of the device's own
        contact table, while this process was down), so this only fills in
        bindings the device itself still corroborates rather than trusting
        the file blindly. Capability is deliberately NOT restored from the
        cache -- same as any contact-derived binding, it's only ever
        trusted from an actual RNSBIND message (see can_route=None on
        _register_peer_binding), so a restored peer starts
        capability-unknown until a fresh RNSBIND/heartbeat arrives."""
        if not self._peer_cache_path or self._mc is None:
            return
        try:
            with open(self._peer_cache_path, "r") as f:
                cached = json.load(f)
        except FileNotFoundError:
            return
        except Exception as exc:
            self._debug(f"Failed to load peer cache: {exc}")
            return

        if not isinstance(cached, dict):
            return

        restored = 0
        for name, mc_pubkey in cached.items():
            if not isinstance(name, str) or not isinstance(mc_pubkey, str):
                continue
            try:
                contact = self._mc.get_contact_by_key_prefix(mc_pubkey)
            except Exception:
                contact = None
            if not contact:
                continue
            if self._register_peer_binding(name, mc_pubkey, None):
                restored += 1

        if restored:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Restored {restored} peer binding(s) from a previous "
                f"session (still confirmed present in the MeshCore "
                f"device's own contact table).",
                RNS.LOG_INFO
            )

    def _save_peer_cache(self) -> None:
        """Persist the current name<->pubkey peer bindings to a small local
        JSON file (see _load_peer_cache). Only called after a binding
        confirmed by a real RNSBIND/RNSBIND_REQ message -- never from a
        bare MeshCore contact-table event, which carries no RNS-specific
        signal at all (see _bind_meshcore_contact) and would otherwise let
        an arbitrary non-RNS MeshCore contact get persisted as if it were a
        confirmed RNS peer. Best-effort: a write failure only costs the
        next restart its fast-path, not correctness, so it's logged at
        debug level rather than treated as an error."""
        if not self._peer_cache_path:
            return
        try:
            with self._peer_lock:
                snapshot = dict(self._peer_table)
            tmp_path = self._peer_cache_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(snapshot, f)
            os.replace(tmp_path, self._peer_cache_path)
        except Exception as exc:
            self._debug(f"Failed to save peer cache: {exc}")

    def _setup_subscribe_contact_and_message_events(self, ET) -> None:
        """Subscribe to channel messages, contact-table updates, direct
        messages, and delivery ACKs -- whichever of these event types this
        version of the meshcore library actually exposes."""
        if self._mc is None:
            return
        def _channel_msg_callback(e) -> None:
            """Bridge a sync library callback into our async event loop."""
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(
                    self._on_channel_msg(e), self._loop
                )

        self._mc.subscribe(
            ET.CHANNEL_MSG_RECV,
            _channel_msg_callback
        )

        def _meshcore_contact_callback(event) -> None:
            """Bridge a sync contact/advertisement event into the async loop."""
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(
                    self._on_meshcore_contact_event(event), self._loop
                )

        _bound_contact_ets = []
        for _name in ("NEW_CONTACT", "CONTACTS", "CONTACTS_FULL",
                      "PATH_UPDATE", "ADVERTISEMENT"):
            _contact_et = getattr(ET, _name, None)
            if _contact_et is not None:
                self._mc.subscribe(_contact_et, _meshcore_contact_callback)
                _bound_contact_ets.append(_name)
        _contact_ets_desc = (
            ", ".join(_bound_contact_ets) if _bound_contact_ets
            else "NONE -- peer table will only update from RNSBIND traffic"
        )
        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"Contact-update events bound: {_contact_ets_desc}.",
            RNS.LOG_INFO if _bound_contact_ets else RNS.LOG_WARNING
        )

        _direct_recv_et = None
        _direct_recv_name = None
        for _name in ("CONTACT_MSG_RECV", "DIRECT_MSG_RECV", "PRIVATE_MSG_RECV",
                      "MSG_RECV", "PRIV_MSG_RECV"):
            _direct_recv_et = getattr(ET, _name, None)
            if _direct_recv_et is not None:
                _direct_recv_name = _name
                def _direct_msg_callback(e) -> None:
                    """Bridge a sync direct-message-received event into the async loop."""
                    if self._loop is not None:
                        asyncio.run_coroutine_threadsafe(
                            self._on_direct_msg(e), self._loop
                        )

                self._mc.subscribe(
                    _direct_recv_et,
                    _direct_msg_callback
                )
                break

        if _direct_recv_et is None:
            if self._has_direct_api:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"No recognized direct-message receive event on this "
                    f"meshcore library -- disabling DIRECT sends (we could "
                    f"send them, but would never see delivery ACKs or "
                    f"replies come back). Falling back to CHANNEL-only.",
                    RNS.LOG_WARNING
                )
            self._has_direct_api = False
        elif self.allow_direct and self._has_direct_api:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"DIRECT messaging ENABLED (receive event={_direct_recv_name}).",
                RNS.LOG_INFO
            )

        _ack_et = None
        for _name in ("ACK", "MSG_ACKED", "MESSAGE_ACKED", "CHAN_ACK"):
            _ack_et = getattr(ET, _name, None)
            if _ack_et is not None:
                def _ack_callback(e) -> None:
                    """Bridge a sync delivery-ACK event into the async loop."""
                    if self._loop is not None:
                        asyncio.run_coroutine_threadsafe(
                            self._on_msg_ack(e), self._loop
                        )

                self._mc.subscribe(
                    _ack_et,
                    _ack_callback
                )
                break

        # DIRECT delivery confirmation relies entirely on that subscription
        # (_on_msg_ack resolves the waiting fragment). Without it every
        # DIRECT send would time out and fall back to CHANNEL, which looks
        # like a flaky link rather than a library incompatibility.
        if self.allow_direct and self._has_direct_api and _ack_et is None:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"This meshcore library exposes no delivery-ACK event type -- "
                f"every DIRECT send that expects a delivery ACK will time out "
                f"and fall back to CHANNEL.",
                RNS.LOG_WARNING
            )

    def _setup_subscribe_lifecycle_events(self, ET) -> None:
        """Subscribe to CONNECTED/DISCONNECTED so a dropped serial/BLE/TCP
        link is actually noticed, rather than the interface sitting
        "online" with a dead connection underneath."""
        if self._mc is None:
            return
        # Connection lifecycle: the meshcore library's connection manager
        # detects a dropped serial/BLE/TCP link and (with auto_reconnect, see
        # _configure_connection) transparently retries before giving up.
        # Without this subscription we'd have no idea a USB re-enumeration
        # or BLE range loss ever happened -- the interface would just sit
        # "online" with a dead connection underneath, silently failing every
        # send.
        _connected_et = getattr(ET, "CONNECTED", None)
        if _connected_et is not None:
            def _connected_callback(e) -> None:
                """Bridge a sync CONNECTED event into the async loop."""
                if self._loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        self._on_mc_connected(e), self._loop
                    )
            self._mc.subscribe(_connected_et, _connected_callback)

        _disconnected_et = getattr(ET, "DISCONNECTED", None)
        if _disconnected_et is not None:
            def _disconnected_callback(e) -> None:
                """Bridge a sync DISCONNECTED event into the async loop."""
                if self._loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        self._on_mc_disconnected(e), self._loop
                    )
            self._mc.subscribe(_disconnected_et, _disconnected_callback)

        if _connected_et is None or _disconnected_et is None:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"This meshcore library has no CONNECTED/DISCONNECTED events "
                f"-- a dropped link (USB unplug, BLE out of range) will not "
                f"be detected; the interface may stay marked online while "
                f"the underlying connection is dead.",
                RNS.LOG_WARNING
            )

    async def _setup_start_background_tasks(self) -> None:
        """Start MeshCore's own message-fetching loop, then spawn every
        long-running background task this interface depends on."""
        if self._mc is None:
            return
        await self._mc.start_auto_message_fetching()

        asyncio.create_task(self._cleanup_loop())
        asyncio.create_task(self._bind_discovery_loop())
        asyncio.create_task(self._async_outgoing_worker(self._direct_outqueue))
        asyncio.create_task(self._async_outgoing_worker(self._channel_outqueue))
        asyncio.create_task(self._contact_refresh_loop())
        asyncio.create_task(self._stats_summary_loop())
        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"Direct and channel outgoing worker tasks started "
            f"(independent queues, maxsize={self.OUTQUEUE_MAXSIZE} each).",
            RNS.LOG_INFO
        )

    # -------------------------------------------------------------------------
    # Peer discovery
    # -------------------------------------------------------------------------

    def _own_capability(self) -> str:
        """This node's RNSBIND capability letter ("R" router / "E" edge)."""
        return self.CAPABILITY_ROUTER if self.can_route else self.CAPABILITY_EDGE

    async def _contact_refresh_loop(self):
        """
        Periodically re-fetch MeshCore's contact list so cached out_path_len
        values don't go stale between the one-time ensure_contacts() call in
        _async_setup and whenever auto_update_contacts happens to fire on its
        own. Without this, a contact that resolves a path to a peer behind a
        repeater sometime after startup can stay looking like out_path_len=-1
        in our local cache indefinitely, causing DIRECT sends to fail and
        silently fall back to CHANNEL forever (see _async_outgoing_worker).
        """
        while True:
            await asyncio.sleep(self.contact_refresh_interval)
            if not self.online or self._mc is None:
                continue
            try:
                # follow=True is required here: the library's ensure_contacts()
                # is a no-op once contacts have been fetched once unless both
                # follow=True is passed AND its internal dirty flag is set (see
                # meshcore.MeshCore.ensure_contacts). Without follow=True this
                # call has silently done nothing since the initial fetch in
                # _async_setup, defeating the whole point of this loop.
                await self._mc.ensure_contacts(follow=True)
            except Exception as exc:
                self._debug(f"Periodic contact refresh failed: {exc}")

    async def _poll_mesh_utilization(self):
        """Poll MeshCore's own firmware-level radio/packet counters. These
        reflect activity from the WHOLE local channel -- every node in
        range, not just this interface -- which is the cheapest way to get
        any sense of "how busy is this mesh" without adding passive-
        monitoring infrastructure of our own: two lightweight command round
        trips, no different in cost to a handful of the other periodic
        housekeeping calls this interface already makes. Stores the raw
        counters plus a delta-derived RX duty-cycle percentage (fraction of
        wall-clock time spent receiving *anything*, since the last poll)
        into self.stats for reporting and, eventually, automatic tuning.
        """
        if self._mc is None or self._EventType is None:
            return
        try:
            radio_res = await self._mc.commands.get_stats_radio()
            pkts_res  = await self._mc.commands.get_stats_packets()
        except Exception as exc:
            self._debug(f"Mesh utilization poll failed: {exc}")
            return

        if (
            radio_res is None or pkts_res is None
            or radio_res.type == self._EventType.ERROR
            or pkts_res.type == self._EventType.ERROR
        ):
            self._debug("Mesh utilization poll returned no usable data.")
            return

        # get_stats_core() is a third, equally cheap local (no mesh
        # airtime) round trip on the same connection, sitting right next
        # to the two calls above. It's optional/best-effort and kept in
        # its own try/except so a firmware/library version without it
        # doesn't take down the radio/packet stats this loop already
        # depends on -- unlike those two, nothing else here relies on it.
        # tx_queue_len is the MeshCore device's OWN outgoing queue depth --
        # a direct "is the local mesh side backed up" signal distinct from
        # rx_channel_utilization_pct below (which only reflects RX
        # activity from everyone in range, not this node's own backlog).
        core = None
        try:
            core_res = await self._mc.commands.get_stats_core()
            if core_res is not None and core_res.type != self._EventType.ERROR:
                core = core_res.payload
        except Exception as exc:
            self._debug(f"Mesh utilization core-stats poll failed: {exc}")

        radio = radio_res.payload
        pkts  = pkts_res.payload
        now   = time.monotonic()

        data = {
            "noise_floor":  radio.get("noise_floor"),
            "last_rssi":    radio.get("last_rssi"),
            "last_snr":     radio.get("last_snr"),
            "tx_air_secs":  radio.get("tx_air_secs"),
            "rx_air_secs":  radio.get("rx_air_secs"),
            "recv":         pkts.get("recv"),
            "sent":         pkts.get("sent"),
            "flood_tx":     pkts.get("flood_tx"),
            "direct_tx":    pkts.get("direct_tx"),
            "flood_rx":     pkts.get("flood_rx"),
            "direct_rx":    pkts.get("direct_rx"),
            "recv_errors":  pkts.get("recv_errors"),
            "battery_mv":   core.get("battery_mv") if core else None,
            "tx_queue_len": core.get("queue_len") if core else None,
            "rx_channel_utilization_pct": None,
        }

        if self._last_mesh_poll is not None:
            prev_time, prev_data = self._last_mesh_poll
            elapsed      = now - prev_time
            prev_rx_air  = prev_data.get("rx_air_secs")
            cur_rx_air   = data.get("rx_air_secs")
            if elapsed > 0 and prev_rx_air is not None and cur_rx_air is not None:
                delta_rx_air = cur_rx_air - prev_rx_air
                # A negative delta means the device rebooted between polls
                # (its counters reset to 0) -- skip this cycle rather than
                # report a nonsensical negative utilization.
                if delta_rx_air >= 0:
                    data["rx_channel_utilization_pct"] = min(
                        100.0, (delta_rx_air / elapsed) * 100.0
                    )

        self._last_mesh_poll = (now, data)
        self.stats.set_mesh_utilization(data)

    def _refresh_routing_health(self) -> None:
        """Push a point-in-time routing-health snapshot into self.stats:
        per-target consecutive path-discovery/DIRECT failure counts (both
        already tracked for the adaptive-backoff/reset-to-flood logic --
        see _path_discovery_cooldown_for/_handle_send_failure -- but never
        previously surfaced anywhere outside that internal bookkeeping),
        plus current DIRECT/CHANNEL outgoing queue depths (already read ad
        hoc elsewhere for stale-fragment/logging purposes, but not tracked
        as a reportable stat). Together these are exactly the "which
        peers/paths are unreliable" and "is RNS producing faster than
        MeshCore can absorb" signals a future tuning controller would need
        -- see the module docstring's TODO on adaptive behavior."""
        with self._path_req_lock:
            path_req_failures    = dict(self._path_req_failures)
            direct_path_failures = dict(self._direct_path_failures)
        self.stats.set_routing_health({
            "path_discovery_failures": path_req_failures,
            "direct_path_failures":    direct_path_failures,
            "direct_outqueue_depth":   self._direct_outqueue.qsize(),
            "channel_outqueue_depth":  self._channel_outqueue.qsize(),
        })

    async def _stats_summary_loop(self):
        """Every 60s, refresh the mesh-utilization snapshot (see
        _poll_mesh_utilization) and, only if debug_level = debug, log a
        full human-readable stats summary. The underlying data is always
        collected regardless of debug_logs -- it's also the intended input
        for automatic tuning of send rates/announce behavior later on --
        only the printing is gated behind debug_logs.
        """
        while True:
            await asyncio.sleep(60)
            if not self.online or self._mc is None:
                continue

            await self._poll_mesh_utilization()
            self._refresh_routing_health()

            if not self.debug_logs:
                continue

            s = self.stats.snapshot()

            hops = self.get_peer_hop_counts()
            hops_str = ", ".join(f"{name}={n}" for name, n in hops.items()) or "none known"

            links_str = ", ".join(
                f"{key[:12]}...={info['failure_rate_pct']:.0f}% "
                f"({info['failed']}/{info['total']})"
                for key, info in s["link_failure_rates"].items()
            ) or "no direct sends yet"

            mesh = s["mesh_utilization"]
            if mesh is not None and mesh.get("rx_channel_utilization_pct") is not None:
                mesh_str = (
                    f"noise_floor={mesh['noise_floor']}dBm "
                    f"last_rssi={mesh['last_rssi']}dBm last_snr={mesh['last_snr']}dB "
                    f"rx_channel_util={mesh['rx_channel_utilization_pct']:.1f}% "
                    f"(ALL traffic on this channel, not just ours) "
                    f"recv={mesh['recv']} sent={mesh['sent']} "
                    f"recv_errors={mesh['recv_errors']} "
                    f"tx_queue_len={mesh.get('tx_queue_len', 'n/a')} "
                    f"battery_mv={mesh.get('battery_mv', 'n/a')}"
                )
            else:
                mesh_str = "not yet available"

            self._debug(
                f"[STATS] uptime={s['uptime_s']:.0f}s -- "
                f"TX: {s['tx_bytes_total']}B total, {s['tx_rate_bps']:.0f}bps "
                f"(10s avg), peak {s['peak_tx_fragments_per_sec']} frag/s -- "
                f"RX: {s['rx_bytes_total']}B total, {s['rx_rate_bps']:.0f}bps "
                f"(10s avg), peak {s['peak_rx_fragments_per_sec']} frag/s -- "
                f"flood sends (last 60s): {s['flood_messages_last_minute']}"
            )
            self._debug(
                f"[STATS] direct sends: {s['direct_send_total']} total, "
                f"{s['direct_send_failed']} failed "
                f"({s['direct_failure_rate_pct']:.1f}%) -- "
                f"per-link failure rate: {links_str}"
            )

            rns_lat = s["rns_tx_latency"]
            mc_lat  = s["meshcore_latency"]
            mc_lat_by_peer = s["meshcore_latency_by_peer"]
            mc_lat_str = ", ".join(
                f"{key[:12]}...avg={info['avg_s']*1000:.0f}ms"
                for key, info in mc_lat_by_peer.items()
            ) or "no direct ACKs yet"
            self._debug(
                f"[STATS] RNS-level TX latency (queue-to-delivered, "
                f"n={rns_lat['count']}): "
                f"avg={rns_lat['avg_s']*1000:.0f}ms "
                f"min={rns_lat['min_s']*1000:.0f}ms "
                f"max={rns_lat['max_s']*1000:.0f}ms -- "
                f"MeshCore-level DIRECT send+ACK RTT (n={mc_lat['count']}): "
                f"avg={mc_lat['avg_s']*1000:.0f}ms "
                f"min={mc_lat['min_s']*1000:.0f}ms "
                f"max={mc_lat['max_s']*1000:.0f}ms -- per-link: {mc_lat_str}"
            )

            self._debug(
                f"[STATS] peer hops: {hops_str} -- "
                f"local mesh utilization: {mesh_str}"
            )

            routing_health = s["routing_health"] or {}
            path_fail_str = ", ".join(
                f"{key[:12]}...={count}"
                for key, count in routing_health.get("path_discovery_failures", {}).items()
            ) or "none"
            direct_fail_str = ", ".join(
                f"{key[:12]}...={count}"
                for key, count in routing_health.get("direct_path_failures", {}).items()
            ) or "none"
            ptype_str = ", ".join(
                f"{name}={count}" for name, count in s["outgoing_packet_type_counts"].items()
            ) or "none sent yet"
            self._debug(
                f"[STATS] outgoing queue depth: "
                f"direct={routing_health.get('direct_outqueue_depth', 'n/a')} "
                f"channel={routing_health.get('channel_outqueue_depth', 'n/a')} -- "
                f"consecutive path-discovery failures: {path_fail_str} -- "
                f"consecutive DIRECT-path failures: {direct_fail_str} -- "
                f"outgoing packet-type mix: {ptype_str}"
            )

    async def _bind_discovery_loop(self):
        """Periodically broadcast RNSBIND_REQ while we have no known peers,
        then settle into a slower RNSBIND heartbeat once we do, so peers can
        discover/rediscover each other's capability over the channel."""
        await asyncio.sleep(5)  # Let connection settle
        if self._mc is None:
            return
        retries = 0
        # _load_peer_cache() (see _async_setup) can populate _peer_table
        # with restored bindings before this loop's very first check below
        # -- which would otherwise make have_peers true immediately and
        # skip the active RNSBIND_REQ phase entirely on every startup that
        # has a valid cache, forced instead straight into the quiet,
        # unsolicited RNSBIND heartbeat branch and its BIND_HEARTBEAT_S
        # (1 hour) sleep. That silences this node's own startup
        # advertisement, AND denies any node that doesn't already have
        # this one cached (or whose cache expired) the chance to learn
        # about it -- a real regression from the fast-restart benefit the
        # cache is supposed to provide, confirmed against a pre-cache
        # build in the field. So the REQ phase still always runs at least
        # once on a fresh start regardless of what the cache restored;
        # only after it completes does have_peers (now reflecting
        # whatever was restored, live-bound, or learned during that REQ
        # round) start gating things the original way.
        startup_req_done = False

        while True:
            with self._peer_lock:
                have_peers = bool(self._peer_table)
            if not startup_req_done:
                have_peers = False

            if not have_peers and retries < self.BIND_MAX_RETRIES:
                if self.online and self._own_mc_key:
                    with self._peer_lock:
                        really_has_peers = bool(self._peer_table)
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"{'Startup announcement' if really_has_peers else 'No peers'} "
                        f"— sending RNSBIND_REQ "
                        f"(attempt {retries + 1}/{self.BIND_MAX_RETRIES}, "
                        f"cap={self._own_capability()})",
                        RNS.LOG_INFO
                    )
                    try:
                        await self._mc.commands.send_chan_msg(
                            self.channel_idx,
                            f"{self.BIND_REQ_PREFIX}"
                            f"{self._own_mc_key}:{self._own_capability()}"
                        )
                    except Exception:
                        pass
                retries += 1
                if retries >= self.BIND_MAX_RETRIES:
                    startup_req_done = True
                await asyncio.sleep(self.BIND_RESP_WINDOW_S)

            else:
                retries = 0
                startup_req_done = True
                if self.online and self._own_mc_key:
                    try:
                        await self._mc.commands.send_chan_msg(
                            self.channel_idx,
                            f"{self.BIND_PREFIX}"
                            f"{self._own_mc_key}:{self._own_capability()}"
                        )
                        RNS.log(
                            f"MeshCore_Dynamic_Interface [{self.name}]: "
                            f"Sent RNSBIND heartbeat [cap={self._own_capability()}] "
                            f"(next in {self.BIND_HEARTBEAT_S:.0f}s).",
                            RNS.LOG_INFO
                        )
                    except Exception as exc:
                        RNS.log(
                            f"MeshCore_Dynamic_Interface [{self.name}]: "
                            f"RNSBIND heartbeat send failed: {exc}",
                            RNS.LOG_WARNING
                        )
                await asyncio.sleep(self.BIND_HEARTBEAT_S)

    async def _delayed_bind_response(self):
        """Wait a random backoff, then broadcast our RNSBIND response --
        spreads out replies when multiple peers answer the same REQ at once."""
        delay = random.uniform(self.BIND_BACKOFF_MIN, self.BIND_BACKOFF_MAX)
        await asyncio.sleep(delay)
        if not self.online or not self._own_mc_key or self._mc is None:
            return
        try:
            await self._mc.commands.send_chan_msg(
                self.channel_idx,
                f"{self.BIND_PREFIX}{self._own_mc_key}:{self._own_capability()}"
            )
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Sent RNSBIND response [cap={self._own_capability()}] "
                f"after {delay:.1f}s backoff.",
                RNS.LOG_INFO
            )
        except Exception:
            pass

    async def _opportunistic_bind_req(self, sender: str):
        """
        Fired when we reassemble an RNS packet from a sender we haven't
        bound with yet. _bind_discovery_loop's active REQ phase only runs
        while we have zero peers total, so it never re-triggers to chase
        down one specific new/unbound neighbor once we already have at
        least one bound peer (e.g. a stable gateway). This closes that gap
        by requesting a bind as soon as we notice we need one, independent
        of how many other peers we already know -- rate-limited per sender
        so a burst of fragments from the same stranger doesn't flood the
        channel with REQs.
        """
        now = time.monotonic()
        with self._last_unbound_req_lock:
            last = self._last_unbound_req.get(sender, 0)
            if now - last < self.UNBOUND_REQ_RETRY_S:
                return
            if sender not in self._last_unbound_req and len(self._last_unbound_req) >= self._UNBOUND_REQ_MAX_SENDERS:
                oldest = sorted(
                    self._last_unbound_req, key=lambda k: self._last_unbound_req[k]
                )[: self._UNBOUND_REQ_MAX_SENDERS // 2]
                for k in oldest:
                    del self._last_unbound_req[k]
            self._last_unbound_req[sender] = now

        if not self.online or not self._own_mc_key or self._mc is None:
            return
        try:
            await self._mc.commands.send_chan_msg(
                self.channel_idx,
                f"{self.BIND_REQ_PREFIX}"
                f"{self._own_mc_key}:{self._own_capability()}"
            )
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Saw RNS traffic from unbound peer '{sender}' -- "
                f"sent opportunistic RNSBIND_REQ.",
                RNS.LOG_INFO
            )
        except Exception:
            pass
    
    def _path_discovery_cooldown_for(self, target_key: str) -> float:
        """Current cooldown to apply before the next discovery attempt for
        this target, based on its consecutive-failure count. Doubles per
        failure (capped) so a persistently-broken hop is retried less and
        less often instead of at a fixed interval forever."""
        with self._path_req_lock:
            failures = self._path_req_failures.get(target_key, 0)
        cooldown = self._path_discovery_base_cooldown_s * (
            self._path_discovery_backoff_factor ** failures
        )
        # +/-20% jitter so multiple peers backing off together don't all
        # retry in the same instant.
        cooldown *= random.uniform(0.8, 1.2)
        return min(cooldown, self._path_discovery_max_cooldown_s)

    def _record_path_discovery_result(self, target_key: str, success: bool) -> None:
        """Update the consecutive-failure counter behind
        _path_discovery_cooldown_for's backoff: reset on success, increment
        on failure."""
        with self._path_req_lock:
            if success:
                # Any success resets the peer back to fast retries -- the
                # link is currently working, no reason to stay backed off.
                self._path_req_failures.pop(target_key, None)
            else:
                self._path_req_failures[target_key] = (
                    self._path_req_failures.get(target_key, 0) + 1
                )

    async def discover_path(self, contact):
        """Run a one-shot MeshCore path-discovery query for this contact and
        return the resolved out_path (or None on failure/timeout). Does not
        update the device's own persistent contact table -- see the comment
        below on send_path_discovery_sync's firmware behavior."""
        if self._mc is None:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Cannot discover path: MeshCore not initialized.",
                RNS.LOG_ERROR
            )
            return None

        await self._mc.ensure_contacts()

        key = contact["public_key"]

        RNS.log(
            f"PATH DISCOVERY BEFORE: "
            f"key={key[:16]} "
            f"out_path_len={contact.get('out_path_len')} "
            f"out_path={contact.get('out_path')}",
            RNS.LOG_INFO
        )

        timeout = contact.get("timeout", 0)

        # IMPORTANT (verified against the MeshCore firmware source,
        # examples/companion_radio/MyMesh.cpp): CMD_SEND_PATH_DISCOVERY_REQ
        # (what send_path_discovery_sync sends) is handled as a one-shot
        # diagnostic query. When the response arrives, MyMesh::onContactPathRecv()
        # matches it against `pending_discovery` and returns false BEFORE
        # calling into BaseChatMesh::onContactPathRecv() -- the function that
        # would otherwise write out_path_len/out_path into the device's own
        # persistent contact record and mark it dirty for a flash write. It
        # only pushes a one-off PATH_DISCOVERY_RESPONSE frame back to
        # whichever client happens to be connected. In other words: a
        # successful discovery here tells US the path, but the MeshCore
        # device's own contact table -- the one the official app reads when
        # it connects -- is NEVER updated by this command. That's why a path
        # "discovered" this way can look resolved to our interface while the
        # official app still sees it as unresolved.
        #
        # An ordinary message exchange doesn't have this problem: its ACK
        # flows through the normal onContactPathRecv() path, which does
        # persist it -- that's why manually sending a message from the
        # official app was enough to fix it previously.
        #
        # The fix: explicitly persist a successful discovery ourselves via
        # change_contact_path() (CMD_ADD_UPDATE_CONTACT, opcode 0x09), which
        # the firmware confirms DOES write to the device's contact table
        # (examples/companion_radio/MyMesh.cpp, CMD_ADD_UPDATE_CONTACT
        # handler: updateContactFromFrame() + dirty_contacts_expiry). This
        # also updates our own local contact dict in place (get_contact_by_
        # key_prefix returns a live reference, not a copy), so there's no
        # need for a separate re-fetch afterward.
        #
        # A single send_path_discovery_sync() call is one flood-out-and-wait
        # round trip; losing that one broadcast (or its response) on a real
        # lossy LoRa link is ordinary. Retry a few times back-to-back before
        # giving up and handing off to the slower outer cooldown -- mirrors
        # how the official MeshCore client retries a real message send
        # multiple times rather than giving up after one attempt.
        res      = None
        resolved = False
        attempts = max(1, self._path_discovery_quick_attempts)
        for attempt in range(1, attempts + 1):
            res = await self._mc.commands.send_path_discovery_sync(
                contact, timeout
            )

            # A non-None result here means the firmware's isValidPathLen()
            # check passed on both path directions -- see MyMesh.cpp's
            # onContactPathRecv() special-case branch, which only sends the
            # PATH_DISCOVERY_RESPONSE frame at all when that check succeeds.
            # So getting a result here IS a genuinely resolved path, not a
            # maybe.
            if res is not None:
                out_path      = res.payload.get("out_path")
                out_path_len  = res.payload.get("out_path_len")
                out_hash_len  = res.payload.get("out_path_hash_len", 1) or 1
                RNS.log(
                    f"PATH DISCOVERY AFTER (attempt {attempt}/{attempts}): "
                    f"resolved out_path_len={out_path_len} out_path={out_path} "
                    f"-- persisting to device contact table.",
                    RNS.LOG_INFO
                )
                try:
                    persist_res = await self._mc.commands.change_contact_path(
                        contact, out_path, path_hash_mode=out_hash_len - 1
                    )
                    ET = self._EventType
                    if persist_res is not None and ET is not None and persist_res.type == ET.ERROR:
                        RNS.log(
                            f"MeshCore_Dynamic_Interface [{self.name}]: "
                            f"Discovered a path for {key[:16]}... but the "
                            f"device rejected persisting it: {persist_res.payload} "
                            f"-- the official app may still show it as unresolved.",
                            RNS.LOG_WARNING
                        )
                    else:
                        resolved = True
                except Exception as exc:
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"Discovered a path for {key[:16]}... but failed to "
                        f"persist it to the device: {exc} -- the official "
                        f"app may still show it as unresolved.",
                        RNS.LOG_WARNING
                    )
            else:
                RNS.log(
                    f"PATH DISCOVERY AFTER (attempt {attempt}/{attempts}): "
                    f"no response within timeout -- path still unresolved.",
                    RNS.LOG_INFO
                )

            if resolved:
                break

        self._record_path_discovery_result(key, resolved)
        if not resolved:
            with self._path_req_lock:
                failures = self._path_req_failures.get(key, 0)
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Path discovery for {key[:16]}... still unresolved after "
                f"{attempts} quick attempt(s) ({failures} consecutive "
                f"discovery round(s) failed) -- next round backed off to "
                f"~{self._path_discovery_cooldown_for(key):.0f}s.",
                RNS.LOG_INFO
            )

        return res

    # -------------------------------------------------------------------------
    # Maintenance
    # -------------------------------------------------------------------------
    
    async def _cleanup_loop(self):
        """Runs every 30s for the life of the interface: sweeps expired
        state out of the reassembly buffers, dedup cache, peer table,
        rate-limiter history, and pending-token bookkeeping, so none of it
        grows unbounded over a long-running session. Each sweep is
        independent -- see the individual _cleanup_* methods."""
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            self._cleanup_stale_reassembly(now)
            self._cleanup_expired_dedup(now)
            self._cleanup_expired_sent_fragments(now)
            self._cleanup_expired_peers(now)
            self._cleanup_rate_limiter_history(now)
            self._cleanup_stale_pending_tokens(now)
            self._cleanup_expired_path_response_pending(now)

    def _cleanup_stale_reassembly(self, now: float) -> None:
        """Drop incomplete multi-fragment reassembly buffers that have been
        waiting longer than fragment_timeout_s -- the remaining fragments
        clearly aren't coming."""
        frag_deadline = now - self.fragment_timeout_s
        with self._asm_lock:
            stale = [
                k for k, (_, ts) in self._assembly_meta.items()
                if ts < frag_deadline
            ]
            for k in stale:
                sender, pkt_id = k
                got, total = len(self._assembly[k]), self._assembly_meta[k][0]
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"Dropping incomplete reassembly for pkt_id {pkt_id} "
                    f"from '{sender}' -- only {got}/{total} fragment(s) "
                    f"arrived within {self.fragment_timeout_s:.0f}s.",
                    RNS.LOG_INFO
                )
                del self._assembly[k]
                del self._assembly_meta[k]

    def _cleanup_expired_dedup(self, now: float) -> None:
        """Drop sliding-window duplicate-packet records once their
        suppression window has elapsed."""
        with self._seen_lock:
            expired_seen = [k for k, exp in self._seen_pkts.items() if now >= exp]
            for k in expired_seen:
                del self._seen_pkts[k]

    def _cleanup_expired_sent_fragments(self, now: float) -> None:
        """Drop sent-CHANNEL-fragment records (see _record_sent_channel_fragment)
        older than _SENT_FRAGMENTS_TTL_S -- a repeater relay that's going
        to be heard at all is heard within seconds, not minutes, so this
        is generous headroom, not a tight budget."""
        deadline = now - self._SENT_FRAGMENTS_TTL_S
        with self._sent_channel_fragments_lock:
            expired = [
                k for k, (_, ts) in self._sent_channel_fragments.items()
                if ts < deadline
            ]
            for k in expired:
                del self._sent_channel_fragments[k]

    def _cleanup_expired_peers(self, now: float) -> None:
        """Drop peers not heard from within peer_ttl_s, along with their
        reverse-lookup and RNS-token-to-MeshCore-key mappings.

        Also clears the same mc_key's path-discovery/DIRECT failure
        counters and last-known-path record. Without this, a peer that
        goes quiet long enough to expire, then later re-announces and
        rebinds to the SAME MeshCore key, would inherit a stale failure
        count from its previous (now-forgotten) binding -- causing a
        premature reset-to-flood on what the interface otherwise treats as
        a brand-new peer. Lock order here is _peer_lock then
        _path_req_lock -- keep it that way everywhere both are held
        together, to avoid a lock-ordering deadlock."""
        peer_deadline = now - self.peer_ttl_s
        with self._peer_lock:
            expired = [
                name for name, ts in self._peer_last_seen.items()
                if ts < peer_deadline
            ]
            expired_keys = []
            for name in expired:
                mc_key = self._peer_table.pop(name, None)
                self._peer_last_seen.pop(name, None)
                self._peer_caps.pop(name, None)
                if mc_key:
                    expired_keys.append(mc_key)
                    self._reverse_peers.pop(mc_key, None)
                    for pfx_len in (8, 12, 16, 24):
                        self._reverse_peers.pop(mc_key[:pfx_len], None)
                    stale_tokens = [
                        t for t, k in self._rns_to_mc_map.items()
                        if k == mc_key
                    ]
                    for t in stale_tokens:
                        del self._rns_to_mc_map[t]
                    self._peer_last_path.pop(mc_key, None)

            if expired_keys:
                with self._path_req_lock:
                    for mc_key in expired_keys:
                        self._direct_path_failures.pop(mc_key, None)
                        self._path_req_failures.pop(mc_key, None)
                        self._path_req_timestamps.pop(mc_key, None)

            if expired:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"Expired {len(expired)} stale peer(s).",
                    RNS.LOG_INFO
                )

    def _cleanup_rate_limiter_history(self, now: float) -> None:
        """Drop per-destination announce/path-request rate-limiter
        timestamps once they're old enough to no longer affect a future
        rate decision (2x the configured rate window)."""
        if self._announce_rate_s > 0:
            ar_deadline = now - (self._announce_rate_s * 2)
            with self._announce_sent_lock:
                stale_ar = [
                    k for k, ts in self._announce_sent_times.items()
                    if ts < ar_deadline
                ]
                for k in stale_ar:
                    del self._announce_sent_times[k]

        if self._path_req_rate_s > 0:
            pr_deadline = now - (self._path_req_rate_s * 2)
            with self._path_req_sent_lock:
                stale_pr = [
                    k for k, (_, last_ts) in self._path_req_sent_times.items()
                    if last_ts < pr_deadline
                ]
                for k in stale_pr:
                    del self._path_req_sent_times[k]

    def _cleanup_stale_pending_tokens(self, now: float) -> None:
        """Drop bookkeeping for senders that stashed pending RNS tokens but
        never completed an RNSBIND handshake within peer_ttl_s (e.g. they
        went out of range for good)."""
        peer_deadline = now - self.peer_ttl_s
        with self._last_unbound_req_lock:
            stale_unbound = [
                name for name, ts in self._last_unbound_req.items()
                if ts < peer_deadline
            ]
            for name in stale_unbound:
                del self._last_unbound_req[name]
        if stale_unbound:
            with self._pending_tokens_lock:
                for name in stale_unbound:
                    self._pending_tokens.pop(name, None)

    def _cleanup_expired_path_response_pending(self, now: float) -> None:
        """Drop path-response-bypass entries whose window elapsed without
        the expected outgoing announce ever happening (e.g. we don't
        actually own/have a path to the requested destination), so entries
        don't accumulate forever."""
        with self._path_response_pending_lock:
            stale_prp = [
                k for k, expiry in self._path_response_pending.items()
                if now >= expiry
            ]
            for k in stale_prp:
                del self._path_response_pending[k]

    # -------------------------------------------------------------------------
    # Inbound event handlers
    # -------------------------------------------------------------------------

    async def _on_channel_msg(self, event):
        """Dispatch an incoming channel broadcast: RNSBIND/RNSBIND_REQ
        messages go to _handle_bind, RNS-tunneled text goes to
        _process_tunnel_text. Ignores anything matching neither prefix."""
        text = event.payload.get("text", "")

        rns_idx  = text.find(self.MSG_PREFIX)
        bind_idx = text.find(self.BIND_PREFIX)
        req_idx  = text.find(self.BIND_REQ_PREFIX)

        eff_bind = -1
        if req_idx != -1 and (bind_idx == -1 or req_idx <= bind_idx):
            eff_bind = req_idx
        elif bind_idx != -1:
            eff_bind = bind_idx

        if eff_bind != -1 and (rns_idx == -1 or eff_bind < rns_idx):
            await self._handle_bind(text, bind_idx, req_idx)
            return

        if rns_idx != -1:
            sender = text[:rns_idx].rstrip(": ") if rns_idx > 0 else ""
            # SNR/RSSI (when the connected firmware/library version reports
            # them at all -- only the "_V3" event payload variants carry
            # them, and RSSI only for logged channel messages) reflect
            # whatever node last relayed this flood message, not
            # necessarily the named sender beyond one hop -- see
            # _deliver_reassembled_packet's note on this being reported to
            # RNS as last-hop link quality, not end-to-end.
            await self._process_tunnel_text(
                text[rns_idx:], sender, rx_mode="CHANNEL",
                snr=event.payload.get("SNR"), rssi=event.payload.get("RSSI"),
            )

    async def _on_direct_msg(self, event):
        """Dispatch an incoming DIRECT message: ignore anything without our
        RNS tunnel prefix, otherwise resolve the sender and hand the text to
        _process_tunnel_text."""
        payload = event.payload
        sender_key = (
            payload.get("pubkey_prefix") or payload.get("sender_pubkey") or
            payload.get("pubkey")        or payload.get("from_pubkey") or ""
        )
        text = payload.get("text", "")
        if not text.startswith(self.MSG_PREFIX):
            return
        sender_id = self._resolve_sender_key(sender_key)
        await self._process_tunnel_text(
            text, sender_id, rx_mode="DIRECT",
            snr=payload.get("SNR"), rssi=payload.get("RSSI"),
        )

    _RECENT_ACKS_MAX = 64

    async def _on_msg_ack(self, event):
        """Resolve the DIRECT fragment (if any) awaiting this delivery ACK
        -- see _pending_acks in _init_runtime_state -- and remember the
        code briefly so an ACK arriving before its code is registered
        (possible under load: MSG_SENT and the ACK are dispatched back to
        back) is still found by _send_direct_with_retry."""
        code = event.payload.get("code") if isinstance(event.payload, dict) else None
        if not code:
            code = (event.attributes or {}).get("code")
        if not code:
            return
        if len(self._recent_acks) >= self._RECENT_ACKS_MAX:
            oldest = sorted(
                self._recent_acks, key=lambda k: self._recent_acks[k]
            )[: self._RECENT_ACKS_MAX // 2]
            for k in oldest:
                del self._recent_acks[k]
        self._recent_acks[code] = time.monotonic()
        entry = self._pending_acks.get(code)
        if entry is not None and not entry[0].done():
            entry[0].set_result(code)

    async def _on_mc_connected(self, event):
        """Mark the interface online and log whether this was an initial
        connect or a recovery from a dropped link."""
        payload = getattr(event, "payload", {}) or {}
        was_offline = not self.online
        self.online = True
        if payload.get("reconnected"):
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Reconnected to MeshCore device after a dropped link.",
                RNS.LOG_NOTICE
            )
        elif was_offline:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"MeshCore connection established.",
                RNS.LOG_INFO
            )

    async def _on_mc_disconnected(self, event):
        """Mark the interface offline and log why the underlying link dropped."""
        payload = getattr(event, "payload", {}) or {}
        reason = payload.get("reason", "unknown")
        self.online = False
        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"MeshCore connection lost (reason={reason}"
            f"{', reconnect attempts exhausted' if payload.get('max_attempts_exceeded') else ''}). "
            f"Interface marked offline -- outgoing sends will be skipped until "
            f"the link recovers"
            f"{' (auto_reconnect is off, so this requires an rnsd restart or a manual reconnect)' if not self.auto_reconnect else ''}.",
            RNS.LOG_WARNING
        )

    def _evict_oldest_peers_locked(self, count: int) -> None:
        """Drop the `count` least-recently-seen peers, along with their
        reverse-lookup/capability/token mappings. Caller must already hold
        self._peer_lock (mirrors the field cleanup in
        _cleanup_expired_peers, just picked by rank instead of TTL) --
        including the same _direct_path_failures/_path_req_failures/
        _path_req_timestamps/_peer_last_path cleanup, for the same reason
        (see _cleanup_expired_peers' docstring)."""
        oldest = sorted(
            self._peer_last_seen.keys(),
            key=lambda n: self._peer_last_seen[n]
        )[:count]
        evicted_keys = []
        for name in oldest:
            mc_key = self._peer_table.pop(name, None)
            self._peer_last_seen.pop(name, None)
            self._peer_caps.pop(name, None)
            if mc_key:
                evicted_keys.append(mc_key)
                self._reverse_peers.pop(mc_key, None)
                for pfx_len in (8, 12, 16, 24):
                    self._reverse_peers.pop(mc_key[:pfx_len], None)
                stale_tokens = [
                    t for t, k in self._rns_to_mc_map.items()
                    if k == mc_key
                ]
                for t in stale_tokens:
                    del self._rns_to_mc_map[t]
                self._peer_last_path.pop(mc_key, None)
        if evicted_keys:
            with self._path_req_lock:
                for mc_key in evicted_keys:
                    self._direct_path_failures.pop(mc_key, None)
                    self._path_req_failures.pop(mc_key, None)
                    self._path_req_timestamps.pop(mc_key, None)
        if oldest:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Peer table at capacity ({self._PEER_TABLE_MAX_PEERS}) -- "
                f"evicted {len(oldest)} least-recently-seen peer(s).",
                RNS.LOG_WARNING
            )

    def _register_peer_binding(self, sender_name: str, mc_pubkey: str,
                              can_route: Optional[bool] = True):
        """Record/update the RNS-name <-> MeshCore-pubkey binding for a peer
        (plus reverse-lookup prefixes and, when known, capability), logging
        only when something actually changed. can_route=None means the
        caller has no real capability signal -- a MeshCore contact-table
        event (NEW_CONTACT/CONTACTS/PATH_UPDATE/etc, see
        _bind_meshcore_contact) has no concept of RNS router/edge at all,
        so in that case any capability already learned from an actual
        RNSBIND/RNSBIND_REQ message (see _handle_bind) is left untouched
        rather than being silently overwritten with a guess. Returns
        whether anything changed."""
        if not sender_name or not mc_pubkey:
            return False

        with self._peer_lock:
            existing     = self._peer_table.get(sender_name)
            previous_cap = self._peer_caps.get(sender_name)
            cap_changed  = can_route is not None and previous_cap != can_route

            if existing is None and len(self._peer_table) >= self._PEER_TABLE_MAX_PEERS:
                self._evict_oldest_peers_locked(self._PEER_TABLE_MAX_PEERS // 2)

            if existing != mc_pubkey:
                self._peer_table[sender_name]  = mc_pubkey
                self._reverse_peers[mc_pubkey] = sender_name
                for pfx_len in (8, 12, 16, 24):
                    pfx = mc_pubkey[:pfx_len]
                    if pfx:
                        self._reverse_peers[pfx] = sender_name

            if can_route is not None:
                self._peer_caps[sender_name] = can_route
            self._peer_last_seen[sender_name] = time.monotonic()

            display_cap = can_route if can_route is not None else previous_cap

        changed = (existing != mc_pubkey) or cap_changed
        if changed:
            if display_cap is None:
                cap_label = "capability unknown"
            else:
                cap_label = "router" if display_cap else "edge — no upstream routing"
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Bound peer '{sender_name}' -> {mc_pubkey[:16]}... "
                f"[{cap_label}]",
                RNS.LOG_INFO
            )
        return changed

    async def _handle_bind(self, text: str, bind_idx: int, req_idx: int = -1):
        """Parse an RNSBIND/RNSBIND_REQ channel message, register the peer
        binding, backfill any RNS tokens received before we knew this
        sender's key, and (for a REQ) schedule our own delayed response."""
        is_req  = (req_idx != -1 and (bind_idx == -1 or req_idx <= bind_idx))
        prefix  = self.BIND_REQ_PREFIX if is_req else self.BIND_PREFIX
        pfx_idx = req_idx              if is_req else bind_idx

        sender_name = text[:pfx_idx].rstrip(": ") if pfx_idx > 0 else ""
        raw_value   = text[pfx_idx + len(prefix):].strip()

        if not sender_name or not raw_value or sender_name == self._own_node_name:
            return

        if ":" in raw_value:
            mc_pubkey, cap_str = raw_value.rsplit(":", 1)
            peer_can_route = (cap_str.strip().upper() != self.CAPABILITY_EDGE)
        else:
            mc_pubkey      = raw_value
            peer_can_route = True  

        mc_pubkey = mc_pubkey.strip()
        if not mc_pubkey:
            return

        # A real RNSBIND/RNSBIND_REQ is proof this sender knows our channel
        # secret -- exactly the "posts in the encrypted channel" signal
        # _grant_telemetry_permission needs before sharing base telemetry
        # (and so answering path discovery) with them. Never granted from
        # a bare MeshCore contact-table event, which has no equivalent
        # proof. Idempotent (no-ops once already granted), so safe to call
        # on every bind including repeat heartbeats.
        await self._grant_telemetry_permission(mc_pubkey)

        peer_changed = self._register_peer_binding(sender_name, mc_pubkey, peer_can_route)

        # Backfill: this sender may have sent us RNS packets before we knew
        # their key. Those tokens were stashed instead of dropped -- drain
        # them into _rns_to_mc_map now instead of waiting to observe fresh
        # traffic from them (which, on a quiet LoRa-only link, might not
        # arrive again for a long time).
        with self._pending_tokens_lock:
            pending = self._pending_tokens.pop(sender_name, None)
        if pending:
            with self._peer_lock:
                for tok in pending:
                    if tok not in self._rns_to_mc_map:
                        self._rns_to_mc_map[tok] = mc_pubkey
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Backfilled {len(pending)} pending RNS token(s) for "
                f"newly-bound peer '{sender_name}'.",
                RNS.LOG_INFO
            )

        if peer_changed:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"{'REQ from' if is_req else 'Peer'} '{sender_name}' "
                f"-> {mc_pubkey[:16]}... "
                f"[{'router' if peer_can_route else 'edge — no upstream routing'}]",
                RNS.LOG_INFO
            )
            # A real RNSBIND/RNSBIND_REQ is the only source of a confirmed
            # RNS-peer binding (as opposed to a bare MeshCore contact-table
            # event, which _bind_meshcore_contact never persists here) --
            # see _load_peer_cache. Offloaded to the default executor
            # rather than called directly: this coroutine runs on the
            # interface's own event loop, and a blocking open()/write()/
            # os.replace() here would stall every other coroutine (fragment
            # sends, other peers' binds) for the duration of the file write.
            try:
                asyncio.get_running_loop().run_in_executor(None, self._save_peer_cache)
            except RuntimeError:
                pass

        if is_req and self._own_mc_key:
            if self._pending_resp_task is None or self._pending_resp_task.done():
                self._pending_resp_task = asyncio.create_task(
                    self._delayed_bind_response()
                )

    def _log_path_if_changed(self, key: str, contact: dict, source: str) -> None:
        """Log MeshCore's own reported path for a peer whenever it actually
        changes, so it's possible to see from the logs alone whether path
        updates are arriving at all (vs. what routing decisions merely
        assume the path is). Called both from contact-update events
        (NEW_CONTACT/CONTACTS/CONTACTS_FULL/PATH_UPDATE/ADVERTISEMENT) and
        from the periodic contact refresh loop's resulting CONTACTS event."""
        if "out_path_len" not in contact:
            return

        out_path_len = contact.get("out_path_len")
        out_path     = contact.get("out_path")
        current      = (out_path_len, out_path)

        with self._peer_lock:
            previous = self._peer_last_path.get(key)
            if previous == current:
                return
            self._peer_last_path[key] = current

        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"Path for {key[:16]}... changed via {source}: "
            f"{previous} -> out_path_len={out_path_len} out_path={out_path}.",
            RNS.LOG_INFO
        )

    def _bind_meshcore_contact(self, contact, source: str = "contact event"):
        """Extract a peer's key/name from a raw MeshCore contact dict and
        register the binding; logs any path change too. A MeshCore contact
        record (public_key/out_path*/adv_name/...) has no concept of RNS
        router/edge capability at all, so this never guesses one -- see the
        can_route=None note on _register_peer_binding. Capability is only
        ever learned from an actual RNSBIND/RNSBIND_REQ message, via
        _handle_bind."""
        if not isinstance(contact, dict):
            return

        key = (
            contact.get("public_key")
            or contact.get("pubkey")
            or contact.get("peer_pubkey")
            or contact.get("node_pubkey")
            or ""
        )
        key = str(key).strip()
        if not key:
            return

        self._log_path_if_changed(key, contact, source)

        name = (
            contact.get("adv_name")
            or contact.get("name")
            or contact.get("node_name")
            or contact.get("advertised_name")
            or ""
        )
        name = str(name).strip()

        if name:
            self._register_peer_binding(name, key, None)

    async def _on_meshcore_contact_event(self, event):
        """Normalize the several possible contact-event payload shapes
        (single contact dict, dict-of-contacts, or list/tuple/set) and bind
        each one via _bind_meshcore_contact."""
        source = str(getattr(event, "type", "contact event"))
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict):
            if any(key in payload for key in ("public_key", "pubkey", "peer_pubkey", "node_pubkey")):
                self._bind_meshcore_contact(payload, source)
            else:
                for contact in payload.values():
                    self._bind_meshcore_contact(contact, source)
        elif isinstance(payload, (list, tuple, set)):
            for contact in payload:
                self._bind_meshcore_contact(contact, source)

    def _resolve_sender_key(self, key_str: str) -> str:
        """Resolve a MeshCore pubkey/prefix to its bound RNS peer name,
        falling back to a prefix match and finally to the raw key string
        if no binding is known."""
        if not key_str:
            return key_str
        with self._peer_lock:
            name = self._reverse_peers.get(key_str)
            if name:
                return name
            for stored_key, stored_name in self._reverse_peers.items():
                if stored_key.startswith(key_str) or key_str.startswith(stored_key):
                    return stored_name
        return key_str

    def get_peer_hop_counts(self) -> dict:
        """{peer_name: out_path_len} for every currently bound peer, read
        live from MeshCore's own contact cache (no I/O -- safe to call from
        any thread). -1 means unknown/flood (no resolved path yet).

        Note this is MeshCore-level hop count (LoRa repeater hops), not RNS
        hop count -- RNS itself always sees exactly one hop through this
        interface no matter how many repeaters a MeshCore path actually
        takes, so MeshCore's own path length is the only hop-count number
        that's actually meaningful here."""
        result = {}
        if self._mc is None:
            return result
        with self._peer_lock:
            peers = dict(self._peer_table)   # sender_name -> mc_pubkey
        for name, mc_key in peers.items():
            contact = self._mc.get_contact_by_key_prefix(mc_key)
            result[name] = contact.get("out_path_len", -1) if contact else -1
        return result

    def _register_pkt_send(self, pkt_id: int, fragment_count: int) -> None:
        """Called once per outgoing RNS packet (from processOutgoing), to
        start RNS-level TX latency tracking for it. fragment_count is how
        many fragments must each be finally accounted for (see
        _mark_pkt_fragment_done) before the packet is considered done."""
        if pkt_id is None or fragment_count <= 0:
            return
        with self._pkt_lock:
            self._pkt_send_tracking[pkt_id] = {
                "start": time.monotonic(),
                "remaining": fragment_count,
            }

    def _mark_pkt_fragment_done(self, pkt_id) -> None:
        """Called whenever a fragment has been *finally* handled -- sent
        (and ACK'd if direct), or lost for good with nothing further in
        flight for it (a DIRECT failure that's about to be retried via
        CHANNEL is NOT final -- don't call this until the CHANNEL attempt
        itself resolves). Once every fragment of the packet has been
        accounted for, records the total elapsed time as this send's
        RNS-level TX latency. Fragments from retransmit passes carry
        pkt_id=None and are silently ignored, since they're extra copies
        of an already-completed original, not a new packet to track."""
        if pkt_id is None:
            return
        start = None
        with self._pkt_lock:
            entry = self._pkt_send_tracking.get(pkt_id)
            if entry is None:
                return
            entry["remaining"] -= 1
            if entry["remaining"] <= 0:
                start = entry["start"]
                del self._pkt_send_tracking[pkt_id]
        if start is not None:
            self.stats.record_rns_tx_latency(time.monotonic() - start)

    async def _process_tunnel_text(self, text: str, sender: str = "", rx_mode: str = "UNKNOWN",
                                    snr=None, rssi=None):
        """Decode one Z85-encoded RNS-tunnel fragment, dedupe it, feed it
        into the reassembly buffer for its packet ID, and hand the
        reassembled RNS packet to processIncoming once all fragments have
        arrived. Drops anything unparsable, too short, or already seen.
        snr/rssi (from the event that carried THIS fragment -- the one that
        happened to complete reassembly, not necessarily every fragment of
        the packet) are passed through to _deliver_reassembled_packet for
        RNS's phy-stats reporting; see the note there."""
        if sender and sender == self._own_node_name:
            if rx_mode == "CHANNEL":
                self._log_heard_channel_repeat(text)
            return

        parsed = self._decode_tunnel_fragment(text, sender, rx_mode)
        if parsed is None:
            return
        frag_idx, pkt_id, frag_total, payload = parsed

        # Counted here rather than after dedup/reassembly: this is a
        # structurally valid fragment that genuinely arrived over the air,
        # which is what a throughput benchmark cares about, regardless of
        # whether it later turns out to be a duplicate.
        self.stats.record_rx()

        key = (sender, pkt_id)
        now = time.monotonic()

        if self._is_duplicate_packet(key, now, pkt_id, sender):
            return

        full_packet = self._reassemble_fragment(key, frag_idx, payload, frag_total, now, sender, pkt_id)
        if full_packet is None:
            return

        # Mark as completely reassembled inside sliding time window
        with self._seen_lock:
            if key not in self._seen_pkts and len(self._seen_pkts) >= self._SEEN_PKTS_MAX_KEYS:
                # Evict the earliest-expiring (i.e. earliest-inserted, since
                # all entries share the same TTL) half to make room.
                oldest = sorted(self._seen_pkts, key=lambda k: self._seen_pkts[k])[
                    : self._SEEN_PKTS_MAX_KEYS // 2
                ]
                for k in oldest:
                    del self._seen_pkts[k]
            self._seen_pkts[key] = now + self.DEDUPLICATION_TTL_S

        if not full_packet:
            return

        self._learn_rns_token_binding(full_packet, sender)
        self._deliver_reassembled_packet(full_packet, sender, rx_mode, snr=snr, rssi=rssi)

    def _decode_tunnel_fragment(self, text: str, sender: str, rx_mode: str):
        """Z85-decode a tunnel fragment and unpack/validate its header.
        Returns (frag_idx, pkt_id, frag_total, payload), or None if the
        fragment is unparsable, too short, or has an invalid header."""
        z85_text = text[len(self.MSG_PREFIX):].strip()
        try:
            raw = z85_decode(z85_text)
        except Exception as exc:
            self._debug(
                f"Dropped unparsable {rx_mode} fragment from '{sender}' "
                f"({len(z85_text)} char(s)): {exc}."
            )
            return None

        # Header unpacked big-endian matching structural change (1B index, 4B packet ID, 1B total fragments)
        if len(raw) < self.HEADER_SIZE:
            self._debug(
                f"Dropped {rx_mode} fragment from '{sender}' -- decoded to "
                f"{len(raw)}b, shorter than the {self.HEADER_SIZE}b header."
            )
            return None

        frag_idx, pkt_id, frag_total = struct.unpack(">BIB", raw[:6])
        payload = raw[self.HEADER_SIZE:]

        if frag_total == 0 or frag_idx >= frag_total:
            self._debug(
                f"Dropped {rx_mode} fragment from '{sender}' -- invalid "
                f"header (frag_idx={frag_idx}, frag_total={frag_total})."
            )
            return None

        return frag_idx, pkt_id, frag_total, payload

    def _is_duplicate_packet(self, key, now, pkt_id, sender) -> bool:
        """Sliding time-window dedup check against _seen_pkts: True (after
        logging) if this (sender, pkt_id) was already fully reassembled
        within the last DEDUPLICATION_TTL_S; also prunes the entry once its
        window has expired."""
        with self._seen_lock:
            if key in self._seen_pkts:
                if now < self._seen_pkts[key]:
                    self._debug(
                        f"Duplicate pkt_id {pkt_id} from '{sender}' suppressed "
                        f"(already fully reassembled within the last "
                        f"{self.DEDUPLICATION_TTL_S:.0f}s -- likely a "
                        f"retransmit pass or overheard repeat)."
                    )
                    return True
                else:
                    del self._seen_pkts[key]
        return False

    def _reassemble_fragment(self, key, frag_idx, payload, frag_total, now, sender, pkt_id):
        """Store one fragment's payload in the reassembly buffer for `key`,
        returning the joined full packet once all frag_total fragments
        have arrived, or None while still incomplete (or on a duplicate
        fragment index / corrupt buffer)."""
        with self._asm_lock:
            if key not in self._assembly:
                if len(self._assembly) >= self._ASSEMBLY_MAX_KEYS:
                    # Evict the oldest half by start time rather than the
                    # incoming fragment -- an attacker flooding fresh fake
                    # keys should lose ground against genuine in-progress
                    # transfers, not evict them.
                    oldest = sorted(
                        self._assembly_meta.keys(),
                        key=lambda k: self._assembly_meta[k][1]
                    )[: self._ASSEMBLY_MAX_KEYS // 2]
                    for k in oldest:
                        self._assembly.pop(k, None)
                        self._assembly_meta.pop(k, None)
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"Reassembly buffer at capacity ({self._ASSEMBLY_MAX_KEYS}) -- "
                        f"evicted {len(oldest)} oldest incomplete transfer(s).",
                        RNS.LOG_WARNING
                    )
                self._assembly[key]      = {}
                self._assembly_meta[key] = (frag_total, now)

            if frag_idx in self._assembly[key]:
                self._debug(
                    f"Duplicate fragment idx={frag_idx} for pkt_id {pkt_id} "
                    f"from '{sender}' ignored (already have it, "
                    f"{len(self._assembly[key])}/{frag_total} collected)."
                )
                return None

            self._assembly[key][frag_idx] = payload

            if len(self._assembly[key]) < self._assembly_meta[key][0]:
                return None

            try:
                expected    = self._assembly_meta[key][0]
                full_packet = b"".join(
                    self._assembly[key][i] for i in range(expected)
                )
                del self._assembly[key]
                del self._assembly_meta[key]
                return full_packet
            except Exception:
                self._assembly.pop(key, None)
                self._assembly_meta.pop(key, None)
                return None

    def _learn_rns_token_binding(self, full_packet: bytes, sender: str) -> None:
        """Extract the RNS token from a reassembled packet and, if the
        sender is already a bound peer, link the token (and any
        LINK_REQUEST link_id) to their MeshCore key; otherwise stash the
        token for _handle_bind to backfill once the sender's bind
        completes, and nudge that along with an opportunistic REQ."""
        rns_token = self._extract_rns_token(full_packet)
        if rns_token is None or not sender:
            return

        with self._peer_lock:
            mc_key = self._peer_table.get(sender)
            if mc_key:
                if rns_token not in self._rns_to_mc_map:
                    self._rns_to_mc_map[rns_token] = mc_key
                    if len(self._rns_to_mc_map) > self._RNS_MAP_MAX:
                        trim = list(self._rns_to_mc_map.keys())[
                            : self._RNS_MAP_MAX // 2
                        ]
                        for t in trim:
                            del self._rns_to_mc_map[t]
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"Linked RNS token {rns_token.hex()[:8]} "
                        f"-> '{sender}'",
                        RNS.LOG_INFO
                    )

                if full_packet[0] & 0x03 == self._RNS_PTYPE_LINK_REQ:
                    link_id = self._link_id_from_lr_packet(full_packet)
                    if link_id is not None and link_id not in self._rns_to_mc_map:
                        self._rns_to_mc_map[link_id] = mc_key
                        RNS.log(
                            f"MeshCore_Dynamic_Interface [{self.name}]: "
                            f"Pre-bound link_id {link_id.hex()[:8]} "
                            f"-> '{sender}' from LINK_REQUEST",
                            RNS.LOG_INFO
                        )

        if not mc_key:
            # sender hasn't completed RNSBIND with us yet. Previously
            # this token was just dropped here -- if RNSBIND never
            # happened to complete afterwards (or completed too late),
            # this destination stayed CHANNEL-only forever even after
            # the peer became known, because nothing re-checked it.
            # Stash it so _handle_bind() can backfill it the moment the
            # bind completes, and nudge that along instead of waiting
            # on the passive heartbeat/zero-peer REQ cycle.
            with self._pending_tokens_lock:
                bucket = self._pending_tokens.get(sender)
                if bucket is None:
                    if len(self._pending_tokens) < self._PENDING_TOKENS_MAX_SENDERS:
                        bucket = set()
                        self._pending_tokens[sender] = bucket
                if (
                    bucket is not None
                    and len(bucket) < self._PENDING_TOKENS_MAX_PER_SENDER
                ):
                    bucket.add(rns_token)
            asyncio.create_task(self._opportunistic_bind_req(sender))

    def _deliver_reassembled_packet(self, full_packet: bytes, sender: str, rx_mode: str,
                                     snr=None, rssi=None) -> None:
        """Log the reassembled packet, flag its destination as awaiting a
        path-response announce if it's itself a path request, and hand it
        up to processIncoming.

        snr/rssi, when available, are reported to RNS core via the
        reports_phy_stats/r_stat_snr/r_stat_rssi hooks (read by
        RNS.Transport at the moment processIncoming's inbound() call
        processes this packet) purely for display (rnstatus/logs) -- traced
        against RNS core, nothing in Transport's routing/retry/timeout
        logic reads these values. They reflect the LAST HOP only (whichever
        node actually transmitted the packet we received), not
        end-to-end/whole-path quality -- for a multi-hop CHANNEL flood or a
        multi-hop DIRECT path, that's not the link quality "to sender" once
        more than one hop away. Always assigned (even to None) rather than
        only when present, so a reading from an earlier packet can't
        linger and get misattributed to a later one that had none."""
        self.r_stat_snr  = snr
        self.r_stat_rssi = rssi

        ptype = full_packet[0] & 0x03
        ptype_str = self._PTYPE_NAMES.get(ptype, "UNKNOWN")

        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"RX -> {rx_mode} from '{sender}'. Reassembled {len(full_packet)}b {ptype_str} packet.",
            RNS.LOG_INFO
        )

        # An incoming DATA+PLAIN packet is a path request. If Transport
        # owns this destination (or has a cached path to it), it will
        # turn around and call processOutgoing() with a fresh ANNOUNCE
        # for it almost immediately. Flag the destination so that
        # announce isn't mistaken for a spontaneous re-announce and
        # suppressed by the outgoing announce rate limiter below.
        dest_type = (full_packet[0] >> 2) & 0x03
        if (
            ptype == self._RNS_PTYPE_DATA
            and dest_type == self._RNS_DTYPE_PLAIN
            and len(full_packet) >= 2 + self._RNS_DST_LEN
        ):
            # NOTE: doesn't distinguish HEADER_2 (two-address/transport)
            # packets the way _extract_rns_token does -- for those this
            # grabs the transport ID rather than the real destination hash
            # at bytes [2+DST_LEN:2+2*DST_LEN]. Low-impact (only weakens
            # this specific rate-limit bypass's key for transit traffic on
            # a can_route=yes node, not a correctness/security issue), left
            # as a known follow-up rather than expanded here.
            dest_id = bytes(full_packet[2:2 + self._RNS_DST_LEN])
            with self._path_response_pending_lock:
                self._path_response_pending[dest_id] = (
                    time.monotonic() + self._path_response_bypass_s
                )
        try:
            self.processIncoming(full_packet)
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Delivery error: {exc}", RNS.LOG_ERROR
            )

    # -------------------------------------------------------------------------
    # Outbound
    # -------------------------------------------------------------------------

    def _is_broadcast_packet(self, data: bytes) -> bool:
        """True for ANNOUNCE and DATA+PLAIN (path request) packets -- the
        two types that can only ever go out as unacknowledged CHANNEL
        broadcasts, never DIRECT."""
        if len(data) < 1:
            return True
        flags     = data[0]
        ptype     = flags & 0x03         
        dest_type = (flags >> 2) & 0x03  
        if ptype == self._RNS_PTYPE_ANNOUNCE:
            return True
        if ptype == self._RNS_PTYPE_DATA and dest_type == self._RNS_DTYPE_PLAIN:
            return True
        return False

    def _extract_rns_token(self, data: bytes):
        """Pull the destination hash out of a raw RNS packet's header, used
        as the key into _rns_to_mc_map. Returns None if the packet is too
        short to contain one."""
        if len(data) < 2:
            return None
        header_type = (data[0] & 0x40) >> 6
        DST_LEN = self._RNS_DST_LEN
        if header_type == 1: 
            end = 2 + 2 * DST_LEN
            if len(data) < end:
                return None
            return bytes(data[2 + DST_LEN:end])
        else: 
            end = 2 + DST_LEN
            if len(data) < end:
                return None
            return bytes(data[2:end])

    def _link_id_from_lr_packet(self, raw: bytes):
        """Derive the ephemeral Link ID from a LINK_REQUEST packet, matching
        RNS's own hashing so DIRECT routing keeps working for the life of
        the Link (whose destination field becomes this Link ID post-
        handshake, rather than the original destination hash)."""
        if len(raw) < 2:
            return None
        DST_LEN = self._RNS_DST_LEN
        header_type = (raw[0] & 0x40) >> 6
        hashable = bytes([raw[0] & 0b00001111])
        if header_type == 1:
            if len(raw) < 2 + DST_LEN:
                return None
            hashable += raw[2 + DST_LEN:]
        else:
            hashable += raw[2:]
        return hashlib.sha256(hashable).digest()[:DST_LEN]

    def process_outgoing(self, data):
        """RNS calls this snake_case alias; delegate to processOutgoing."""
        return self.processOutgoing(data)

    def processOutgoing(self, data):
        """RNS-facing entry point for a packet leaving this interface:
        applies announce/path-request rate limiting, picks DIRECT vs
        CHANNEL routing, fragments/encodes the packet, enqueues each
        fragment, and schedules any extra best-effort retransmits."""
        if not self.online:
            return

        hdr_byte  = data[0] if data else 0
        ptype     = hdr_byte & 0x03
        dest_type = (hdr_byte >> 2) & 0x03

        # Cheap to compute (ptype is already extracted above for the rate
        # limiters below) -- distinguishes "lots of retries because of
        # bulk data" from "lots of retries because of an announce storm"
        # in the stats snapshot, which a bare send/fail counter can't.
        self.stats.record_outgoing_ptype(self._PTYPE_NAMES.get(ptype, "UNKNOWN"))

        if self._rate_limit_announce(data, ptype):
            return
        if self._rate_limit_path_request(data, ptype, dest_type):
            return

        with self._pkt_id_lock:
            pkt_id       = self._pkt_id
            self._pkt_id = (self._pkt_id + 1) & 0xFFFFFFFF  # 32-bit bound integer tracking

        handler   = _PacketHandler(data, pkt_id, self._auto_payload_size())
        self._register_pkt_send(pkt_id, len(handler.fragments))

        broadcast = self._is_broadcast_packet(data)

        #Log Fragmentation Performance Metrics
        RNS.log(
            f"[PERF {pkt_id}] OUT "
            f"size={len(data)} "
            f"ptype={ptype} "
            f"broadcast={broadcast}",
            RNS.LOG_INFO
        )

        route = self._resolve_outgoing_route(data, broadcast)
        mode, target = route[0]
        priority = (
            self._PRIORITY_HANDSHAKE
            if ptype in (self._RNS_PTYPE_LINK_REQ, self._RNS_PTYPE_PROOF)
            else self._PRIORITY_NORMAL
        )

        self._enqueue_fragments(handler, mode, target, priority, pkt_id, broadcast)
        self._schedule_extra_retransmits(handler, route, ptype, dest_type, broadcast, priority)

        self.txb += len(data)
        self.stats.record_tx_bytes(len(data))

    def _rate_limit_announce(self, data: bytes, ptype: int) -> bool:
        """Per-destination outgoing announce rate limiter. Bypassed for
        announces answering a path request we recently saw come in for this
        same destination (see _path_response_pending) -- those are demand-
        driven responses, not spontaneous re-announces, and dropping them
        silently is what causes intermittent "path request timed out"
        failures on the requesting side when a routine self-announce
        happened to go out shortly beforehand. Returns True if the announce
        should be suppressed."""
        if not (self._announce_rate_s > 0 and len(data) >= 2 + self._RNS_DST_LEN
                and ptype == self._RNS_PTYPE_ANNOUNCE):
            return False

        # Full 16-byte destination hash (_RNS_DST_LEN), matching
        # _extract_rns_token -- previously only the first 10 bytes, which
        # worked in practice (collision odds are negligible) but was an
        # unnecessary inconsistency with the key _deliver_reassembled_packet
        # uses to populate _path_response_pending, which this same dest_id
        # is looked up against just below.
        dest_id = bytes(data[2:2 + self._RNS_DST_LEN])
        now     = time.monotonic()

        with self._path_response_pending_lock:
            expiry = self._path_response_pending.pop(dest_id, None)
        answering_path_request = expiry is not None and now < expiry

        if not answering_path_request:
            with self._announce_sent_lock:
                last = self._announce_sent_times.get(dest_id, 0)
                if now - last < self._announce_rate_s:
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"Suppressing outgoing announce for "
                        f"{dest_id.hex()[:8]} -- {now - last:.0f}s "
                        f"since last (< {self._announce_rate_s:.0f}s limit).",
                        RNS.LOG_INFO
                    )
                    return True
        else:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Announce for {dest_id.hex()[:8]} bypassing rate "
                f"limiter -- answering a recent path request.",
                RNS.LOG_INFO
            )

        with self._announce_sent_lock:
            self._announce_sent_times[dest_id] = now
        return False

    def _rate_limit_path_request(self, data: bytes, ptype: int, dest_type: int) -> bool:
        """Per-destination outgoing path request rate limiter, with a burst
        window that lets RNS's own natural retry cluster through before the
        long-run anti-spam cooldown kicks in. See path_req_burst_window
        comment in _configure_path_discovery_and_retry for rationale.
        Returns True if the path request should be suppressed."""
        if not (self._path_req_rate_s > 0 and len(data) >= 2 + self._RNS_DST_LEN
                and ptype == self._RNS_PTYPE_DATA and dest_type == self._RNS_DTYPE_PLAIN):
            return False

        dest_id = bytes(data[2:2 + self._RNS_DST_LEN])   # see _rate_limit_announce
        now     = time.monotonic()
        with self._path_req_sent_lock:
            entry = self._path_req_sent_times.get(dest_id)
            if entry is None:
                # First request for this destination: starts a new burst.
                self._path_req_sent_times[dest_id] = (now, now)
            else:
                first_ts, last_ts = entry
                if now - first_ts < self._path_req_burst_window_s:
                    # Still inside the burst window -- let it through,
                    # just refresh last_ts for cleanup purposes.
                    self._path_req_sent_times[dest_id] = (first_ts, now)
                elif now - last_ts < self._path_req_rate_s:
                    # Burst window elapsed and still within the
                    # long-run cooldown -- suppress.
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"Suppressing outgoing path request for "
                        f"{dest_id.hex()[:8]} -- {now - last_ts:.0f}s "
                        f"since last (burst window elapsed, "
                        f"< {self._path_req_rate_s:.0f}s limit).",
                        RNS.LOG_INFO
                    )
                    return True
                else:
                    # Cooldown expired -- this starts a fresh burst.
                    self._path_req_sent_times[dest_id] = (now, now)
        return False

    def _resolve_outgoing_route(self, data: bytes, broadcast: bool):
        """Decide whether this packet should go DIRECT (bound peer with a
        resolved MeshCore path) or CHANNEL (broadcast, no direct route, or
        an unresolved path -- which also kicks off a rate-limited path-
        discovery attempt). Returns a single-element [(mode, target)] route
        list."""
        target_key = None
        channel_reason = ""

        if broadcast:
            channel_reason = "Mandatory broadcast packet (e.g., Announce)"
        elif not self._has_direct_api:
            channel_reason = "Direct routing API disabled or undetected by interface"
        else:
            next_hop_token = self._extract_rns_token(data)
            if next_hop_token is None:
                channel_reason = f"Packet too short to extract destination token (len: {len(data)})"
            else:
                with self._peer_lock:
                    target_key = self._rns_to_mc_map.get(next_hop_token)
                if not target_key:
                    channel_reason = f"No direct route bound for RNS token {next_hop_token.hex()[:8]}"
                else:
                    # Check MeshCore's contact cache to see if it has a resolved path to the peer yet. If not, fall back to channel for now. (saves time and avoids a failed direct send attempt that would have to be retried later)
                    if self._mc:
                        contact = self._mc.get_contact_by_key_prefix(target_key)
                        opl = contact.get("out_path_len", -1) if contact else -1
                        # In your routing selection logic:
                        if opl == -1:
                            channel_reason = f"Peer bound but no resolved MeshCore path yet (out_path_len=-1) for {target_key}"

                            now = time.monotonic()
                            last_req = self._path_req_timestamps.get(target_key, 0)
                            cooldown = self._path_discovery_cooldown_for(target_key)
                            if (now - last_req) > cooldown:
                                self._path_req_timestamps[target_key] = now
                                if self._loop is not None:
                                    if contact is not None:
                                        RNS.log(f"requesting path discovery for peer key {target_key}", RNS.LOG_INFO)
                                        asyncio.run_coroutine_threadsafe(
                                            self.discover_path(contact),
                                            self._loop
                                        )
                                    else:
                                        RNS.log(f"requesting path discovery for peer key {target_key} (no contact found)", RNS.LOG_INFO)

                                    asyncio.run_coroutine_threadsafe(
                                        self._mc.commands.send_advert(flood=True),
                                        self._loop
                                        )
                            else:
                                RNS.log(
                                    f"Skipping path discovery request for peer key {target_key} "
                                    f"-- backed off (retry in ~{cooldown - (now - last_req):.0f}s).",
                                    RNS.LOG_INFO
                                )

                            target_key = None

        if channel_reason:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Routing -> CHANNEL. Reason: {channel_reason}",
                RNS.LOG_INFO
            )
            return [("channel", None)]

        assert target_key is not None
        # Re-fetch fresh rather than trusting a `contact` variable that
        # may have been set several branches up (or not at all, in the
        # unlikely case self._mc was falsy above) -- this is a log line,
        # not a routing decision, so it should reflect exactly what
        # MeshCore's cache holds for this peer right now.
        _log_contact = self._mc.get_contact_by_key_prefix(target_key) if self._mc else None
        _path_desc = (
            f"out_path_len={_log_contact.get('out_path_len')} "
            f"out_path={_log_contact.get('out_path')}"
            if _log_contact is not None else "no cached contact"
        )
        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"Routing -> DIRECT via peer key {target_key[:12]}... [{_path_desc}]",
            RNS.LOG_INFO
        )
        return [("direct", target_key)]

    def _enqueue_fragments(self, handler, mode, target, priority, pkt_id, broadcast) -> None:
        """Push every fragment of a packet onto the correct (direct/channel)
        outgoing queue, blocking on backpressure if the queue is full."""
        outq = self._direct_outqueue if mode == "direct" else self._channel_outqueue
        for frag_str in handler.fragments:
            try:
                # Thread-safe blocking put handles backpressure cleanly
                queued_at = time.monotonic()

                outq.put(
                    (
                        priority,
                        next(self._outqueue_seq),
                        (mode, target, frag_str, queued_at, pkt_id, broadcast),
                    ),
                    block=True,
                    timeout=None
                )

                RNS.log(
                    f"[PERF {pkt_id}] QUEUE({mode}) "
                    f"priority={priority} depth={outq.qsize()}",
                    RNS.LOG_INFO
                )

            except Exception:
                pass

    def _schedule_extra_retransmits(self, handler, route, ptype, dest_type, broadcast, priority) -> None:
        """Kick off _delayed_retransmits for packet types that benefit from
        unacknowledged best-effort duplication (announces, path requests,
        and ordinary data that fell back to CHANNEL) -- a no-op for
        anything already ACK'd via DIRECT.

        Path RESPONSES aren't a distinct packet type in this system --
        they're just an ANNOUNCE that happened to be triggered by an
        inbound path request (see _path_response_pending) -- so they're
        already covered by the announce_retransmit_extra branch below with
        no separate handling needed. ordinary_data_retransmit_extra only
        makes sense for non-broadcast packets that ended up on the
        unacknowledged CHANNEL path (e.g. no bound peer / no resolved route
        yet); a DIRECT send is already ACK'd by the firmware (see
        _async_outgoing_worker), so blindly retransmitting it too would
        just double-deliver a packet that's already confirmed received."""
        retransmit_extra = 0
        if broadcast:
            if ptype == self._RNS_PTYPE_ANNOUNCE:
                retransmit_extra = self.announce_retransmit_extra
            elif ptype == self._RNS_PTYPE_DATA and dest_type == self._RNS_DTYPE_PLAIN:
                retransmit_extra = self.path_req_retransmit_extra
        elif route[0][0] == "channel":
            retransmit_extra = self.ordinary_data_retransmit_extra

        if retransmit_extra > 0 and self._loop is not None:
            asyncio.run_coroutine_threadsafe(
                self._delayed_retransmits(handler.fragments, route, retransmit_extra, broadcast, priority),
                self._loop
            )

    async def _delayed_retransmits(self, fragments, route, count, broadcast, priority):
        """Re-queue every fragment of a packet `count` more times, each
        after an independent random jitter delay, to opportunistically
        improve delivery odds over an unreliable link without blocking the
        original send."""
        # route always holds exactly one (mode, target) pair -- unpack once
        # rather than re-iterating a single-element list per fragment.
        mode, target = route[0]
        outq = self._direct_outqueue if mode == "direct" else self._channel_outqueue
        for i in range(count):
            delay = random.uniform(self.retransmit_jitter_min_s, self.retransmit_jitter_max_s)
            await asyncio.sleep(delay)
            if not self.online:
                return
            for frag_str in fragments:
                try:
                    queued_at = time.monotonic()
                    outq.put_nowait((
                        priority,
                        next(self._outqueue_seq),
                        (mode, target, frag_str, queued_at, None, broadcast),
                    ))
                except queue.Full:
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"Retransmit pass {i + 1}/{count} failed to enqueue "
                        f"({len(fragments)} fragment(s), same pkt_id) -- queue full.",
                        RNS.LOG_WARNING
                    )
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Retransmit pass {i + 1}/{count} sent "
                f"({len(fragments)} fragment(s), same pkt_id).",
                RNS.LOG_INFO
            )

    async def _send_direct_with_retry(self, target, frag_str) -> None:
        """Attempt a DIRECT send+ACK cycle against `target` up to
        direct_send_attempts times, with a fresh send (and fresh
        expected_ack) each retry -- a lost ACK on the return trip doesn't
        mean the forward frame was lost, so retrying the send itself is
        cheaper and far less airtime-hungry than immediately escalating to
        a broadcast CHANNEL resend. Mirrors send_msg_with_retry's
        multi-attempt behavior in the official meshcore client -- including
        now considering a reset-to-flood WITHIN this same retry loop (see
        _maybe_reset_stale_path) after each failed attempt, the same way
        official's flood_after does, rather than only after this AND a
        subsequent packet have both fully failed. Returns normally on
        success; raises the last attempt's exception if every attempt
        failed."""
        # Guaranteed non-None here: only called from _send_fragment, which
        # the outgoing worker only invokes after checking both are set.
        assert self._mc is not None and self._EventType is not None
        max_direct_attempts = max(1, self.direct_send_attempts)
        last_exc = RuntimeError("direct send failed")
        # Only attempts that waited the firmware's FULL suggested ACK time
        # count as evidence of a stale path (see _maybe_reset_stale_path);
        # an attempt cut short by our own ceiling proves nothing about the
        # route.
        full_wait_failures = 0
        # One future shared by every attempt of this fragment: the peer's
        # firmware ACKs each transmission it receives, so an ACK for ANY
        # attempt's expected_ack code -- including one that lands after
        # that attempt's own wait already expired -- means the fragment
        # was delivered.
        delivered = asyncio.get_running_loop().create_future()
        codes = []
        peer = target[:12] if target else "?"

        try:
            for attempt in range(1, max_direct_attempts + 1):
                counts_toward_reset = False
                try:
                    attempt_start = time.monotonic()
                    result = await self._mc.commands.send_msg(target, frag_str)
                    if result is None or result.type != self._EventType.MSG_SENT:
                        reason = (
                            result.payload.get("reason", "no path/unknown")
                            if result is not None else "no response"
                        )
                        raise RuntimeError(f"direct send rejected: {reason}")

                    # MSG_SENT confirms the radio put this fragment on air
                    # (each retry is a real separate transmission) but says
                    # nothing about delivery: that is the later ACK event
                    # carrying the expected_ack tag handed back here.
                    self.stats.record_tx()

                    exp_ack = result.payload.get("expected_ack")
                    if exp_ack is None:
                        return
                    exp_ack_hex = (
                        exp_ack.hex() if isinstance(exp_ack, (bytes, bytearray))
                        else str(exp_ack)
                    )
                    if exp_ack_hex not in codes:
                        codes.append(exp_ack_hex)
                    self._pending_acks[exp_ack_hex] = (delivered, attempt_start)

                    suggested_ms = result.payload.get("suggested_timeout", 0) or 0
                    # MSG_SENT's type byte is 1 when the firmware had no
                    # route and flooded the fragment, 0 when it sent it
                    # along the cached path -- the two get different
                    # ceilings (see _configure_fragmentation).
                    sent_flood = result.payload.get("type") == 1
                    raw_ack_timeout = max(
                        self.direct_ack_timeout_s, (suggested_ms / 1000.0) * 1.2
                    )
                    ceiling = (
                        self.direct_ack_timeout_max_s if sent_flood
                        else self.direct_ack_timeout_routed_max_s
                    )
                    ack_timeout = min(raw_ack_timeout, ceiling)
                    counts_toward_reset = raw_ack_timeout <= ack_timeout
                    if raw_ack_timeout > ack_timeout:
                        RNS.log(
                            f"MeshCore_Dynamic_Interface [{self.name}]: "
                            f"Firmware suggested {suggested_ms}ms ACK timeout for "
                            f"peer key {peer}... "
                            f"({'flood/no-path' if sent_flood else 'routed'} send) "
                            f"-- capping wait at {ack_timeout:.1f}s instead of "
                            f"{raw_ack_timeout:.1f}s to avoid blocking the "
                            f"outgoing queue.",
                            RNS.LOG_INFO
                        )

                    # The ACK can be dispatched before this coroutine gets
                    # to register its code (MSG_SENT and the ACK arrive
                    # back to back on a 0-hop link) -- check what was
                    # already heard.
                    if not delivered.done():
                        for c in codes:
                            if c in self._recent_acks:
                                delivered.set_result(c)
                                break
                    try:
                        acked_code = await asyncio.wait_for(
                            asyncio.shield(delivered), ack_timeout
                        )
                    except asyncio.TimeoutError:
                        acked_code = None
                    if acked_code is None:
                        raise RuntimeError(
                            f"no delivery ACK within {ack_timeout:.1f}s "
                            f"(expected_ack={exp_ack_hex}, "
                            f"firmware suggested {suggested_ms}ms)"
                        )

                    _, acked_start = self._pending_acks.get(
                        acked_code, (None, attempt_start)
                    )
                    late = "" if acked_code == exp_ack_hex else (
                        f" -- late ACK for an earlier attempt of this "
                        f"fragment (expected_ack={acked_code})"
                    )
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"Direct send to peer key {peer}... ACK received "
                        f"(expected_ack={exp_ack_hex}){late}.",
                        RNS.LOG_INFO
                    )
                    # MeshCore-level latency: send-to-ACK for the
                    # transmission that was actually acknowledged.
                    self.stats.record_meshcore_latency(
                        time.monotonic() - acked_start, peer_key=target
                    )
                    return
                except Exception as exc:
                    last_exc = exc
                    if counts_toward_reset:
                        full_wait_failures += 1
                    if attempt < max_direct_attempts:
                        RNS.log(
                            f"MeshCore_Dynamic_Interface [{self.name}]: "
                            f"DIRECT send attempt {attempt}/{max_direct_attempts} "
                            f"to peer key {peer}... failed ({exc}) -- retrying.",
                            RNS.LOG_INFO
                        )
                        await self._maybe_reset_stale_path(target, full_wait_failures)

            raise last_exc
        finally:
            for c in codes:
                self._pending_acks.pop(c, None)

    async def _maybe_reset_stale_path(self, target, consecutive_failures: int) -> None:
        """Called from inside _send_direct_with_retry's own retry loop,
        after each failed attempt except the last -- decides whether to
        give up on this peer's currently cached path and reset it to
        flood mode before the very next attempt, the same way the
        official meshcore library's send_msg_with_retry resets after
        flood_after (default 2) attempts within ONE message's own retry
        loop. consecutive_failures counts only attempts that waited the
        firmware's full suggested ACK time: an attempt our own ceiling cut
        short says nothing about whether the route is stale, and resetting
        on that basis is exactly what destroyed working multi-hop routes in
        field testing.

        Resetting is irreversible -- it discards the path both locally and
        on the MeshCore device's own persistent contact record, and forces
        recovery through flood-mode discovery, which is structurally the
        least reliable of MeshCore's delivery mechanisms over multiple
        hops. So, as before: be direct_path_reset_patience_multiplier times
        more patient when the last-polled RSSI still looks reasonable
        (more likely worth one more retry), and fall back to the
        unmultiplied threshold when it doesn't, or isn't available yet."""
        if self.direct_path_reset_threshold <= 0 or self._mc is None:
            return
        try:
            contact = self._mc.get_contact_by_key_prefix(target) if target else None
        except Exception:
            contact = None
        if contact is None:
            return
        opl = contact.get("out_path_len", -1)
        if opl == -1:
            return  # already flood mode -- nothing cached left to reset

        mesh_util = self.stats.mesh_utilization
        last_rssi = mesh_util.get("last_rssi") if mesh_util else None
        conditions_look_ok = (
            last_rssi is not None and last_rssi > self.direct_path_reset_rssi_floor
        )
        effective_threshold = (
            self.direct_path_reset_threshold * self.direct_path_reset_patience_multiplier
        ) if conditions_look_ok else self.direct_path_reset_threshold

        if consecutive_failures < effective_threshold:
            return

        with self._path_req_lock:
            self._direct_path_failures[target] = (
                self._direct_path_failures.get(target, 0) + 1
            )
        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"Peer key {target[:12] if target else '?'}... has failed "
            f"{consecutive_failures} consecutive DIRECT send attempt(s), each "
            f"waiting the firmware's full suggested ACK time, on "
            f"its cached path (out_path_len={opl}, last_rssi="
            f"{last_rssi if last_rssi is not None else 'unknown'}dBm) -- "
            f"resetting to flood mode instead of continuing to retry what "
            f"looks like a stale route.",
            RNS.LOG_WARNING
        )
        try:
            await self._mc.commands.reset_path(contact)
        except Exception as exc:
            self._debug(
                f"reset_path failed for {target[:12] if target else '?'}...: {exc}"
            )

    async def _async_outgoing_worker(self, outq):
        """
        Worker task pulling payload chunks from one thread-safe synchronized
        queue using the event loop executor pool to preserve pure async
        interface execution. Run twice concurrently -- once bound to
        self._direct_outqueue, once to self._channel_outqueue -- so items
        already carry a fixed mode ("direct" or "channel") matching the queue
        they were enqueued on. Keeping the two queues independent means a
        DIRECT send stuck waiting on a delivery ACK can't stall CHANNEL
        broadcasts (or other DIRECT sends bound for a different peer, in the
        case of a stuck channel worker) queued behind it, and vice versa.
        """
        while True:
            if not self.online or self._mc is None or self._loop is None:
                await asyncio.sleep(0.5)
                continue

            if self._EventType is None:
                await asyncio.sleep(0.5)
                continue

            # Safe non-blocking cross-thread extraction via run_in_executor
            priority, _seq, item = await self._loop.run_in_executor(None, outq.get)

            mode, target, frag_str, queued_at, pkt_id, broadcast = item
            queue_wait   = time.monotonic() - queued_at
            depth_behind = outq.qsize()

            #Log queue wait time
            RNS.log(
                f"[PERF {pkt_id if pkt_id is not None else 'unknown'}] DEQUEUE({mode}) "
                f"priority={priority} queue_wait={queue_wait:.3f}s "
                f"depth={depth_behind}",
                RNS.LOG_INFO
            )

            if self._drop_stale_fragment_if_needed(mode, pkt_id, broadcast, queue_wait, depth_behind):
                # A dropped fragment is a terminal outcome for it, same as
                # a successful send or a fallback -- without this,
                # _pkt_send_tracking's entry for this pkt_id never reaches
                # a "remaining" count of zero and is never cleaned up
                # (there's no separate sweep for it in _cleanup_loop), and
                # RNS-level TX latency for this packet is silently never
                # recorded.
                self._mark_pkt_fragment_done(pkt_id)
                outq.task_done()
                continue

            try:
                await self._send_fragment(mode, target, frag_str, pkt_id)
            except Exception as exc:
                self._handle_send_failure(mode, target, frag_str, priority, queued_at, pkt_id, broadcast, exc)
                outq.task_done()
                continue

            await self._pace_after_send(mode, frag_str)
            outq.task_done()

    def _drop_stale_fragment_if_needed(self, mode, pkt_id, broadcast, queue_wait, depth_behind) -> bool:
        """Age alone isn't a good enough reason to drop a fragment -- one
        that's simply had bad luck on an otherwise quiet queue will get
        sent in a moment regardless, and dropping it gains nothing. Only
        sacrifice it when there's an ACTUAL backlog behind it too
        (depth_behind, i.e. how many items are still waiting once this one
        is removed): that's the case where continuing to send it anyway is
        genuinely costing everything queued after it more time, and where
        the RNS-level request/Link that created it (RNS's own Link
        establishment timeout is typically a handful of seconds per hop --
        see stale_fragment_max_age in _configure_stale_fragment_dropping)
        has, in all likelihood, already given up. Broadcast packets are
        exempt regardless -- they have no single requester to give up, and
        a late announce/path-response is still useful network-wide.
        Returns True (after logging) if the fragment should be dropped."""
        if not (
            not broadcast
            and self.stale_fragment_max_age_s > 0
            and queue_wait > self.stale_fragment_max_age_s
            and depth_behind >= self.stale_fragment_min_queue_depth
        ):
            return False

        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"Dropping stale {mode} fragment for pkt_id "
            f"{pkt_id if pkt_id is not None else 'unknown'} -- sat "
            f"{queue_wait:.1f}s in queue (> "
            f"{self.stale_fragment_max_age_s:.0f}s max age) with "
            f"{depth_behind} item(s) still backed up behind it "
            f"(>= {self.stale_fragment_min_queue_depth}); the "
            f"requester has very likely already given up.",
            RNS.LOG_INFO
        )
        return True

    async def _send_fragment(self, mode, target, frag_str, pkt_id) -> None:
        """Send one fragment via DIRECT (with ACK-gated retry) or CHANNEL
        broadcast, recording stats and marking the packet fragment done on
        success. Raises on total failure -- caller handles the CHANNEL
        fallback/logging."""
        # Guaranteed non-None here: only called from the outgoing worker
        # after it has already checked self._mc is set.
        assert self._mc is not None
        if mode == "direct":
            # A lost delivery ACK on the return trip doesn't mean the
            # forward frame was lost -- retrying the DIRECT send a
            # couple of times is cheaper and far less airtime-hungry
            # than immediately escalating to a broadcast CHANNEL
            # resend. Mirrors send_msg_with_retry's multi-attempt
            # behavior in the official meshcore client, instead of
            # giving DIRECT exactly one shot before falling back.
            # Raises on total failure -- caught by the caller, which
            # handles the CHANNEL fallback.
            await self._send_direct_with_retry(target, frag_str)

            self.stats.record_direct_result(success=True, peer_key=target)
            self._mark_pkt_fragment_done(pkt_id)

            # A successful DIRECT delivery means whatever path is
            # currently cached for this peer is working -- clear any
            # accumulated failure count so a future blip doesn't
            # inherit credit toward resetting a path that just proved
            # itself fine.
            with self._path_req_lock:
                self._direct_path_failures.pop(target, None)
        else:
            await self._mc.commands.send_chan_msg(self.channel_idx, frag_str)
            self.stats.record_tx()
            self.stats.record_flood_tx()
            self._mark_pkt_fragment_done(pkt_id)
            self._record_sent_channel_fragment(frag_str)

    def _record_sent_channel_fragment(self, frag_str: str) -> None:
        """Remember one just-sent CHANNEL fragment's (pkt_id, frag_idx) and
        character length, so that later hearing it echoed back (relayed by
        a nearby repeater) can be logged and checked for truncation -- see
        _process_tunnel_text's self-echo branch. Decodes frag_str with the
        same _decode_tunnel_fragment used on the receive side rather than
        trusting the pkt_id passed into _send_fragment, since that's None
        for retransmit-originated fragments (see _delayed_retransmits)."""
        parsed = self._decode_tunnel_fragment(frag_str, self._own_node_name, "CHANNEL-TX")
        if parsed is None:
            return
        frag_idx, pkt_id, frag_total, _payload = parsed
        key = (pkt_id, frag_idx)
        with self._sent_channel_fragments_lock:
            if (key not in self._sent_channel_fragments
                    and len(self._sent_channel_fragments) >= self._SENT_FRAGMENTS_MAX_KEYS):
                oldest = sorted(
                    self._sent_channel_fragments,
                    key=lambda k: self._sent_channel_fragments[k][1]
                )[: self._SENT_FRAGMENTS_MAX_KEYS // 2]
                for k in oldest:
                    del self._sent_channel_fragments[k]
            self._sent_channel_fragments[key] = (len(frag_str), time.monotonic())

        # Once per packet (its first fragment going out), not once per
        # fragment -- see _check_heard_repeats for why a raw packet-count
        # delta, not content-echo detection, is what can actually observe
        # a repeat of our own transmission at all.
        if frag_idx == 0 and self._loop is not None:
            asyncio.create_task(self._check_heard_repeats(pkt_id, frag_total))

    async def _check_heard_repeats(self, pkt_id, frag_total: int) -> None:
        """Detect repeats of our own CHANNEL send the way the raw radio
        actually can, rather than via message content.

        Traced through the firmware source: Dispatcher::checkRecv()
        increments n_recv_flood (surfaced as get_stats_packets()'s
        flood_rx) for every physically-received flood packet BEFORE
        handing it to Mesh::onRecvPacket() -- which is where the wasSeen()
        dedup check lives. Mesh::sendFlood() marks our own outgoing packet
        as already-seen at the moment we send it ("in case it is
        rebroadcast back to us"), so a nearby repeater relaying our
        message back to us never reaches onGroupDataRecv() (the source of
        CHANNEL_MSG_RECV) at all -- it's dropped by that dedup check. But
        flood_rx already counted it before that check ever ran. So a raw
        flood_rx delta across a short window after sending is the only
        way this interface (or any companion app on the same firmware)
        can observe a repeat of its own message at all.

        Important caveat, logged alongside the result: flood_rx counts
        ALL flood traffic heard in the window, not verified to be
        specifically repeats of THIS packet -- any other channel activity
        from other senders in the same window shows up in the same delta.
        This is an approximation, not a confirmed per-message repeat
        count."""
        if self._mc is None or self._EventType is None:
            return
        try:
            before = await self._mc.commands.get_stats_packets()
        except Exception as exc:
            self._debug(f"Heard-repeats check: before-poll failed: {exc}")
            return
        if before is None or before.type == self._EventType.ERROR:
            return
        before_flood_rx = before.payload.get("flood_rx")
        if before_flood_rx is None:
            return

        await asyncio.sleep(self.HEARD_REPEATS_WINDOW_S)

        try:
            after = await self._mc.commands.get_stats_packets()
        except Exception as exc:
            self._debug(f"Heard-repeats check: after-poll failed: {exc}")
            return
        if after is None or after.type == self._EventType.ERROR:
            return
        after_flood_rx = after.payload.get("flood_rx")
        if after_flood_rx is None:
            return

        delta = after_flood_rx - before_flood_rx
        if delta < 0:
            return  # device rebooted between polls; counters reset, not meaningful
        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"Heard {delta} flood packet(s) on the channel in the "
            f"{self.HEARD_REPEATS_WINDOW_S:.0f}s after sending pkt_id="
            f"{pkt_id} ({frag_total} fragment(s)) -- NOTE: this counts ALL "
            f"flood traffic in that window, not confirmed to be "
            f"specifically repeats of this message (MeshCore's own "
            f"per-packet dedup means an exact repeat of our own message "
            f"never reaches this interface's content-level handling, so "
            f"this raw counter is the closest approximation available).",
            RNS.LOG_INFO
        )

    def _log_heard_channel_repeat(self, text: str) -> None:
        """Called when a CHANNEL message comes back with our own node name
        as sender -- i.e. we heard a nearby repeater relay something we
        sent. Logs whether it came back byte-for-byte intact or shorter
        than what we actually sent (direct evidence of truncation
        happening somewhere between us and whatever relayed it, rather
        than at our own encoding step) -- see the firmware_text_limit
        investigation in the module docstring. Best-effort: if we don't
        have a record of the original send (evicted, or from before this
        process started), still logs the raw length/parseability so a
        human can compare against other sources."""
        received_len = len(text)
        parsed = self._decode_tunnel_fragment(text, self._own_node_name, "CHANNEL-ECHO")
        if parsed is None:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Heard our own CHANNEL message repeated, but it no longer "
                f"decodes ({received_len} chars received) -- likely "
                f"truncated or corrupted in transit.",
                RNS.LOG_WARNING
            )
            return

        frag_idx, pkt_id, frag_total, _payload = parsed
        with self._sent_channel_fragments_lock:
            sent_record = self._sent_channel_fragments.get((pkt_id, frag_idx))

        if sent_record is None:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Heard our own CHANNEL message repeated (pkt_id={pkt_id} "
                f"frag={frag_idx + 1}/{frag_total}, {received_len} chars) -- "
                f"no record of the original send length to compare against.",
                RNS.LOG_INFO
            )
            return

        sent_len, _sent_ts = sent_record
        if received_len == sent_len:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Heard our own CHANNEL message repeated intact (pkt_id="
                f"{pkt_id} frag={frag_idx + 1}/{frag_total}, {sent_len} "
                f"chars) -- relay confirmed working at this length.",
                RNS.LOG_INFO
            )
        else:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Heard our own CHANNEL message repeated but TRUNCATED "
                f"(pkt_id={pkt_id} frag={frag_idx + 1}/{frag_total}: sent "
                f"{sent_len} chars, received {received_len} chars) -- a "
                f"repeater or firmware in the relay path likely has a "
                f"lower text-length limit than firmware_text_limit.",
                RNS.LOG_WARNING
            )

    def _handle_send_failure(self, mode, target, frag_str, priority, queued_at, pkt_id, broadcast, exc) -> None:
        """Handle a _send_fragment failure: for DIRECT, log path
        diagnostics, maybe trigger path discovery or a reset-to-flood on
        repeated failure, and fall back to enqueuing the fragment on
        CHANNEL; for CHANNEL there's no fallback, so the fragment is simply
        marked done (lost)."""
        if mode == "direct":
            # Guaranteed non-None here: only called from the outgoing
            # worker after it has already checked both are set.
            assert self._mc is not None and self._loop is not None
            self.stats.record_direct_result(success=False, peer_key=target)
            # Diagnostic: pull the target's out_path status from the
            # meshcore library's local contact cache (self._mc.contacts).
            # This reads in-memory state populated by earlier
            # CONTACTS/PATH_UPDATE/ADVERTISEMENT events -- it does NOT
            # touch the serial port, so it's safe to call from here
            # without contending with rnsd's own use of the connection.
            path_info = "unknown (no cached contact)"
            try:
                contact = self._mc.get_contact_by_key_prefix(target) if target else None
                if contact:
                    opl = contact.get("out_path_len", -1)
                    path_info = (
                        f"out_path_len={opl}"
                        if opl != -1 else "out_path_len=-1 (no known route) - Requesting new path discovery"
                    )
                    if opl > 0:
                        now = time.monotonic()
                        last_req = self._path_req_timestamps.get(target, 0)
                        cooldown = self._path_discovery_cooldown_for(target)
                        if (now - last_req) > cooldown:
                            self._path_req_timestamps[target] = now
                            # Fire-and-forget: run_coroutine_threadsafe's
                            # returned future is otherwise never awaited or
                            # checked, so any exception raised inside
                            # discover_path (e.g. a future meshcore library
                            # version handing back a contact dict shaped
                            # differently than expected) would be silently
                            # discarded, and path discovery for this peer
                            # would then fail with nothing in the logs to
                            # explain why. _log_scheduled_task_exceptions
                            # surfaces that instead of swallowing it.
                            fut = asyncio.run_coroutine_threadsafe(self.discover_path(contact), self._loop) # Request path
                            fut.add_done_callback(
                                lambda f: self._log_scheduled_task_exceptions(f, "discover_path")
                            )
                    # NOTE: reset-to-flood on repeated failure used to be
                    # decided here, from a cross-PACKET counter requiring
                    # two entire packets (direct_send_attempts each) to
                    # fully fail before ever resetting -- ~3x more raw
                    # attempts than the official meshcore client's own
                    # send_msg_with_retry, which resets after flood_after
                    # (default 2) attempts WITHIN one message's own retry
                    # loop. That's now handled by _maybe_reset_stale_path,
                    # called from inside _send_direct_with_retry itself, so
                    # by the time we get here a reset may already have
                    # happened -- opl above already reflects that (reset_path
                    # updates the local contact dict immediately).
            except Exception:
                pass
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"DIRECT send to peer key {target[:12] if target else '?'}... "
                f"failed after {max(1, self.direct_send_attempts)} attempt(s) "
                f"({exc}) [{path_info}] -- falling back to CHANNEL.",
                RNS.LOG_INFO
            )
            try:
                # Fallback to channel if targeted routing exceptions happen
                # mid-transit -- always goes to the channel queue regardless
                # of which queue this worker instance drains. broadcast is
                # always False here: only non-broadcast packets ever route
                # DIRECT in the first place (see _is_broadcast_packet).
                # priority carries over unchanged -- it's the same
                # original packet, just switching transport mode.
                self._channel_outqueue.put_nowait((
                    priority,
                    next(self._outqueue_seq),
                    ("channel", None, frag_str, queued_at, pkt_id, broadcast),
                ))
            except queue.Full:
                RNS.log(
                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                    f"Channel queue full ({self.OUTQUEUE_MAXSIZE}) -- "
                    f"dropped fragment {pkt_id if pkt_id is not None else 'unknown'} "
                    f"on DIRECT->CHANNEL fallback.",
                    RNS.LOG_WARNING
                )
                # Truly lost -- nothing further will be attempted for
                # this fragment, so it's "finally accounted for" now
                # (as opposed to a successful re-enqueue just above,
                # which isn't done until the CHANNEL attempt itself
                # resolves).
                self._mark_pkt_fragment_done(pkt_id)
        else:
            # CHANNEL send raised -- there's no retry/fallback for
            # CHANNEL itself, so this fragment is also finally done
            # (lost) right here.
            self._mark_pkt_fragment_done(pkt_id)

    async def _pace_after_send(self, mode, frag_str) -> None:
        """Sleep the configured per-mode pacing delay, extended if needed
        so throughput stays within rate_limit_bps.

        NOTE: with independent DIRECT/CHANNEL workers, rate_limit_bps is
        enforced per-queue rather than as one combined interface-wide cap --
        each stream paces itself to the configured bps rather than the two
        sharing a single budget."""
        delay = (
            self.direct_frag_delay_s
            if mode == "direct"
            else self.fragment_delay_s
        )
        if self.rate_limit_bps > 0:
            # Actual wire length -- what's really transmitted, and so what
            # actually consumes airtime -- not a decoded-payload estimate.
            # This used to assume base64's 3-bytes-per-4-chars ratio, left
            # over from before the switch to Z85 (5 chars per 4 bytes, a
            # different ratio); using the real transmitted length instead
            # of decoding-estimating it sidesteps needing to keep this in
            # sync with whatever encoding is in use at all.
            bits  = len(frag_str) * 8
            delay = max(delay, bits / self.rate_limit_bps)

        await asyncio.sleep(delay)

    # -------------------------------------------------------------------------
    # Inbound delivery
    # -------------------------------------------------------------------------

    def processIncoming(self, data: bytes):
        """Hand a fully-reassembled RNS packet up to RNS's Transport layer."""
        if self.online and not self.detached:
            self.rxb += len(data)
            self.stats.record_rx_bytes(len(data))
            self.owner.inbound(data, self)

    def __str__(self):
        """Human-readable identifier RNS uses in its own logs."""
        return f"MeshCore_Dynamic_Interface[{self.name}]"

# ------------------------------------------------------------------------
# z85 encode
# ------------------------------------------------------------------------

_Z85_ALPHABET = (
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-:+=^!/*?&<>()[]{}@%$#"
)
_Z85_DECODE = {c: i for i, c in enumerate(_Z85_ALPHABET)}


def z85_encode(data: bytes) -> str:
    """Encode arbitrary-length bytes as a Z85 string (85-char safe alphabet,
    no comma/quote/backslash). Self-describing padding: first output char
    is a digit 0-3 giving how many zero bytes were appended before encoding."""
    pad = (-len(data)) % 4
    padded = data + b"\x00" * pad

    out = []
    for i in range(0, len(padded), 4):
        chunk = padded[i:i + 4]
        value = int.from_bytes(chunk, "big")
        chars = []
        for _ in range(5):
            chars.append(_Z85_ALPHABET[value % 85])
            value //= 85
        out.append("".join(reversed(chars)))

    return str(pad) + "".join(out)
#test

def z85_decode(text: str) -> bytes:
    """Inverse of z85_encode. Raises ValueError on malformed input."""
    if not text or text[0] not in "0123":
        raise ValueError("missing/invalid Z85 pad-count prefix")
    pad = int(text[0])
    body = text[1:]

    if len(body) % 5 != 0:
        raise ValueError(f"Z85 body length {len(body)} not a multiple of 5")

    out = bytearray()
    for i in range(0, len(body), 5):
        group = body[i:i + 5]
        value = 0
        for ch in group:
            try:
                value = value * 85 + _Z85_DECODE[ch]
            except KeyError:
                raise ValueError(f"invalid Z85 character: {ch!r}")
        if value > 0xFFFFFFFF:
            raise ValueError("Z85 group overflows 32 bits")
        out.extend(value.to_bytes(4, "big"))

    if pad:
        out = out[:-pad]
    return bytes(out)

interface_class = MeshCore_Dynamic_Interface
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

    "RNS:" + base64url( [frag_idx:1][pkt_id:4][frag_total:1] + payload )

No base64 padding is transmitted; the receiver restores it before decode.

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
  MeshCore firmware silently truncates channel messages that exceed a hardware-
  dependent character limit (observed ~128 chars on common firmware builds).
  The firmware also prepends the sender's node name when relaying channel
  messages, so the effective character budget for the encoded portion is:

      budget = firmware_limit - len(node_name) - 2       (": " separator)

  Encoded message length:
      msg_len = ceil((payload_size + HEADER_SIZE) * 4/3) + len("RNS:")

  With a 4-byte pkt_id, HEADER_SIZE is 6 bytes. With default payload_size = 64:
      msg_len = ceil(70 * 4/3) + 4 = 94 + 4 = 98 chars
      Safe for node names up to ~28 characters at a 128-char firmware limit.

  To calculate the maximum safe payload size for your node name length:
      budget      = firmware_limit - len(node_name) - 2
      max_payload = floor((budget - 4) * 3/4) - HEADER_SIZE

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
  │     # Channel — all nodes on the same tunnel must share these values   │
  │     channel_idx = 0                                                     │
  │     channel_name = RNSTunnel                                            │
  │     channel_secret = <32 hex chars>  # openssl rand -hex 16           │
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
  │     fragment_delay = 2.5   # seconds between channel-mode fragments    │
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
import hashlib
import itertools
import struct
import queue
import random
import threading
import time


# ─────────────────────────────────────────────────────────────────────────────
# Fragmentation helper
# ─────────────────────────────────────────────────────────────────────────────

class _PacketHandler:
    """
    Encodes one RNS binary packet into one or more channel/direct message
    strings. Each fragment carries a 6-byte binary header:

        [ frag_idx : 1 byte ] [ pkt_id : 4 bytes ] [ frag_total : 1 byte ]

    followed by the raw payload chunk.  The combined bytes are base64url-
    encoded (no padding) and prefixed with MSG_PREFIX ("RNS:").
    """

    HEADER_SIZE  = 6  # 1 byte idx + 4 bytes pkt_id + 1 byte total
    PAYLOAD_SIZE = 64
    MSG_PREFIX   = "RNS:"

    def __init__(self, data: bytes, pkt_id: int, payload_size: int = 0):
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
        return len(self.fragments)


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

    _RNS_DST_LEN = 16
    _RNS_PTYPE_DATA     = 0x00
    _RNS_PTYPE_ANNOUNCE = 0x01
    _RNS_PTYPE_LINK_REQ = 0x02
    _RNS_PTYPE_PROOF    = 0x03
    
    _RNS_DTYPE_SINGLE = 0x00
    _RNS_DTYPE_GROUP  = 0x01
    _RNS_DTYPE_PLAIN  = 0x02
    _RNS_DTYPE_LINK   = 0x03

    # Outgoing queue priority tiers (lower value = dequeued first, see
    # queue.PriorityQueue in __init__). LINK_REQUEST and PROOF packets are
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

    # -------------------------------------------------------------------------
    # Constructor
    # -------------------------------------------------------------------------

    def __init__(self, owner, configuration):
        super().__init__()

        self.owner = owner
        self.name  = configuration.get("name", "MeshCore Dynamic")
        cfg        = configuration

        # --- Transport selection -------------------------------------------
        self.transport = cfg.get("transport", "serial").lower()

        # --- Connection parameters -----------------------------------------
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

        # --- Channel identity ----------------------------------------------
        self.channel_idx        = int(str(cfg.get("channel_idx", 0)).strip())
        self.channel_name       = cfg.get("channel_name", "RNSTunnel")
        self.channel_secret_hex = cfg.get("channel_secret",
                                          "10000000000000000000000000000000")

        # --- Optional radio parameter overrides ----------------------------
        self.radio_freq = float(cfg.get("freq", 0))
        self.radio_bw   = float(cfg.get("bw",   0))
        self.radio_sf   = int(cfg.get("sf",     0))
        self.radio_cr   = int(cfg.get("cr",     0))
        self.contact_refresh_interval = float(cfg.get("contact_refresh_interval", 120.0))

        # --- Protocol tuning -----------------------------------------------
        self.payload_size = int(cfg.get("payload_size", 64))
        self.fragment_delay_s = float(cfg.get("fragment_delay", 2.5))

        raw_dfd = cfg.get("direct_frag_delay", None)
        self.direct_frag_delay_s = float(raw_dfd) if raw_dfd is not None else 0.5

        # Minimum time to wait for a delivery ACK on a DIRECT send before
        # treating it as failed and falling back to CHANNEL. The radio also
        # hands back its own per-send "suggested_timeout" (based on path
        # length/airtime); we wait whichever of the two is longer.
        self.direct_ack_timeout_s = float(cfg.get("direct_ack_timeout", 4.0))

        # Hard ceiling on that wait, regardless of what the firmware suggests.
        # A contact with no known path (flood mode) can report a suggested
        # timeout of many seconds to minutes; since the outgoing worker is a
        # single shared queue, waiting that long would stall every other
        # queued fragment behind it. Our own CHANNEL fallback is cheap, so we
        # cap the wait and let the fallback handle it instead.
        self.direct_ack_timeout_max_s = float(cfg.get("direct_ack_timeout_max", 8.0))

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
        # (repeater repositioned, shorter route now available) can fail
        # 100% of the time while remaining stuck in the contact table, and
        # resetting it to flood mode measurably outperforms continuing to
        # retry it -- see reset_path usage in _async_outgoing_worker. Set to
        # 0 to disable (never auto-reset a cached path).
        self.direct_path_reset_threshold = int(cfg.get("direct_path_reset_threshold", 2))

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

        # --- Retransmission for broadcast-only (CHANNEL-forced) packets ----
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

        # --- Routing capability --------------------------------------------
        self.can_route = (
            cfg.get("can_route", "yes").lower() not in ("no", "false", "0")
        )

        self.allow_direct = (
            cfg.get("allow_direct", "yes").lower() not in ("no", "false", "0")
        )

        self.peer_ttl_s = float(cfg.get("peer_ttl", 86400))
        self.bitrate = int(cfg.get("bitrate", 300))

        # Per-interface debug logging, independent of RNS core's global
        # [logging] loglevel. RNS.log() gates every message (ours and RNS
        # core's own) on a single global level, so raising it to DEBUG (6)
        # to see this interface's own diagnostic logs also turns on RNS
        # core's own debug firehose. Messages logged via self._debug() below
        # are emitted at LOG_INFO -- gated only by this flag -- so they show
        # up under the normal loglevel = 4 default without any core noise.
        self.debug_logs = str(cfg.get("debug_level", "info")).strip().lower() == "debug"

        # --- RNS core interface-contract attributes -------------------------
        # RNS core checks `interface.HW_MTU + (interface.ifac_size or 0)` against
        # every inbound packet before it's handed anywhere else — every custom
        # interface must set both or Transport.preprocess_inbound() throws. This
        # is the max size of a single *fully reassembled* RNS packet this
        # interface can carry, not the per-fragment LoRa payload size
        # (self.payload_size handles that).
        self.HW_MTU    = RNS.Reticulum.MTU
        
        # --- Internal async / threading state ------------------------------
        self._mc          = None
        self._EventType   = None
        self._loop        = None
        self._loop_thread = None
        
        # Thread-safe queues used to decouple synchronous execution from the
        # worker loops. DIRECT and CHANNEL traffic get independent queues (and
        # independent worker tasks, see _async_outgoing_worker) so a DIRECT
        # send blocked waiting on a delivery ACK (up to direct_ack_timeout_max_s)
        # can never stall CHANNEL broadcasts queued behind it, and vice versa.
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

        self._peer_table     = {}
        self._reverse_peers  = {}
        self._peer_last_seen = {}
        self._peer_caps      = {}
        self._rns_to_mc_map  = {}
        self._peer_lock      = threading.Lock()

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
        # (base/max/factor are set from config above in __init__)
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
        # direct_path_reset_threshold in __init__.
        self._direct_path_failures = {}   # target_key -> consecutive DIRECT failure count

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

    def _auto_payload_size(self):
        if self._own_node_name:
            # MeshCore firmware silently truncates channel messages that exceed a
            # hardware-dependent character limit (observed ~128 chars on common
            # firmware builds). The firmware also prepends the sender's node name
            # when relaying channel messages, so the effective character budget
            # for the encoded portion is:
            #
            #     budget = firmware_limit - len(node_name) - 2       (": " separator)
            #
            # Encoded message length:
            #     msg_len = ceil((payload_size + HEADER_SIZE) * 4/3) + len("RNS:")
            #
            # With a 4-byte pkt_id, HEADER_SIZE is 6 bytes. With default payload_size = 64:
            #     msg_len = ceil(70 * 4/3) + 4 = 94 + 4 = 98 chars
            #     Safe for node names up to ~28 characters at a 128-char firmware limit.
            #
            # To calculate the maximum safe payload size for your node name length:
            #     budget      = firmware_limit - len(node_name) - 2
            #     max_payload = floor((budget - 4) * 3/4) - HEADER_SIZE

            firmware_limit = 128
            margin = 2 #safety margin for firmware variations and future changes
            budget         = firmware_limit - len(self._own_node_name) - 2
            max_payload    = ((budget - 4) * 3 // 4 - self.HEADER_SIZE) - margin

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
        MeshCore = self._mc_module.MeshCore
        ET       = self._EventType

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
                return
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Driver init error: {exc}", RNS.LOG_ERROR
            )
            return

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
            return

        if ET is None:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"MeshCore EventType is unavailable.",
                RNS.LOG_ERROR
            )
            return

        try:
            result = await self._mc.commands.send_appstart()
            if ET and result.type == ET.SELF_INFO:
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
        except Exception as exc:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Channel init error: {exc}", RNS.LOG_WARNING
            )

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

        def _channel_msg_callback(e) -> None:
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(
                    self._on_channel_msg(e), self._loop
                )

        self._mc.subscribe(
            ET.CHANNEL_MSG_RECV,
            _channel_msg_callback
        )

        def _meshcore_contact_callback(event) -> None:
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

        for _name in ("ACK", "MSG_ACKED", "MESSAGE_ACKED", "CHAN_ACK"):
            _ack_et = getattr(ET, _name, None)
            if _ack_et is not None:
                def _ack_callback(e) -> None:
                    if self._loop is not None:
                        asyncio.run_coroutine_threadsafe(
                            self._on_msg_ack(e), self._loop
                        )

                self._mc.subscribe(
                    _ack_et,
                    _ack_callback
                )
                break

        # The DIRECT delivery-confirmation wait in _async_outgoing_worker
        # references self._EventType.ACK directly (not whichever alias was
        # matched above), so that's the one that actually has to exist for
        # ACK-gated direct sends to work at all. If it's missing, every
        # direct send with an expected_ack will raise AttributeError inside
        # the worker's try block and get silently treated as a normal send
        # failure (falling back to CHANNEL) -- which looks like a flaky link
        # rather than a library incompatibility, so flag it clearly here.
        if self.allow_direct and self._has_direct_api and getattr(ET, "ACK", None) is None:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"This meshcore library has no EventType.ACK -- every DIRECT "
                f"send that expects a delivery ACK will error out and fall "
                f"back to CHANNEL. If you see repeated 'DIRECT send ... "
                f"failed' log lines mentioning AttributeError, this is why.",
                RNS.LOG_WARNING
            )

        # Connection lifecycle: the meshcore library's connection manager
        # detects a dropped serial/BLE/TCP link and (with auto_reconnect, see
        # __init__) transparently retries before giving up. Without this
        # subscription we'd have no idea a USB re-enumeration or BLE range
        # loss ever happened -- the interface would just sit "online" with a
        # dead connection underneath, silently failing every send.
        _connected_et = getattr(ET, "CONNECTED", None)
        if _connected_et is not None:
            def _connected_callback(e) -> None:
                if self._loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        self._on_mc_connected(e), self._loop
                    )
            self._mc.subscribe(_connected_et, _connected_callback)

        _disconnected_et = getattr(ET, "DISCONNECTED", None)
        if _disconnected_et is not None:
            def _disconnected_callback(e) -> None:
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

        await self._mc.start_auto_message_fetching()

        asyncio.create_task(self._cleanup_loop())
        asyncio.create_task(self._bind_discovery_loop())
        asyncio.create_task(self._async_outgoing_worker(self._direct_outqueue))
        asyncio.create_task(self._async_outgoing_worker(self._channel_outqueue))
        asyncio.create_task(self._contact_refresh_loop())
        RNS.log(
            f"MeshCore_Dynamic_Interface [{self.name}]: "
            f"Direct and channel outgoing worker tasks started "
            f"(independent queues, maxsize={self.OUTQUEUE_MAXSIZE} each).",
            RNS.LOG_INFO
        )

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

    # -------------------------------------------------------------------------
    # Peer discovery
    # -------------------------------------------------------------------------

    def _own_capability(self) -> str:
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

    async def _bind_discovery_loop(self):
        await asyncio.sleep(5)  # Let connection settle
        if self._mc is None:
            return
        retries = 0

        while True:
            with self._peer_lock:
                have_peers = bool(self._peer_table)

            if not have_peers and retries < self.BIND_MAX_RETRIES:
                if self.online and self._own_mc_key:
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"No peers — sending RNSBIND_REQ "
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
                await asyncio.sleep(self.BIND_RESP_WINDOW_S)

            else:
                retries = 0
                if self.online and self._own_mc_key:
                    try:
                        await self._mc.commands.send_chan_msg(
                            self.channel_idx,
                            f"{self.BIND_PREFIX}"
                            f"{self._own_mc_key}:{self._own_capability()}"
                        )
                    except Exception:
                        pass
                await asyncio.sleep(self.BIND_HEARTBEAT_S)

    async def _delayed_bind_response(self):
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
        while True:
            await asyncio.sleep(30)  
            now = time.monotonic()

            # --- Stale fragment buffers ------------------------------------
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

            # --- Expired sliding window deduplication records --------------
            with self._seen_lock:
                expired_seen = [k for k, exp in self._seen_pkts.items() if now >= exp]
                for k in expired_seen:
                    del self._seen_pkts[k]

            # --- Expired peers ---------------------------------------------
            peer_deadline = now - self.peer_ttl_s
            with self._peer_lock:
                expired = [
                    name for name, ts in self._peer_last_seen.items()
                    if ts < peer_deadline
                ]
                for name in expired:
                    mc_key = self._peer_table.pop(name, None)
                    self._peer_last_seen.pop(name, None)
                    self._peer_caps.pop(name, None)
                    if mc_key:
                        self._reverse_peers.pop(mc_key, None)
                        for pfx_len in (8, 12, 16, 24):
                            self._reverse_peers.pop(mc_key[:pfx_len], None)
                        stale_tokens = [
                            t for t, k in self._rns_to_mc_map.items()
                            if k == mc_key
                        ]
                        for t in stale_tokens:
                            del self._rns_to_mc_map[t]
                if expired:
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"Expired {len(expired)} stale peer(s).",
                        RNS.LOG_INFO
                    )

            # --- Old announce rate entries ---------------------------------
            if self._announce_rate_s > 0:
                ar_deadline = now - (self._announce_rate_s * 2)
                with self._announce_sent_lock:
                    stale_ar = [
                        k for k, ts in self._announce_sent_times.items()
                        if ts < ar_deadline
                    ]
                    for k in stale_ar:
                        del self._announce_sent_times[k]

            # --- Old path request rate entries ----------------------------
            if self._path_req_rate_s > 0:
                pr_deadline = now - (self._path_req_rate_s * 2)
                with self._path_req_sent_lock:
                    stale_pr = [
                        k for k, (_, last_ts) in self._path_req_sent_times.items()
                        if last_ts < pr_deadline
                    ]
                    for k in stale_pr:
                        del self._path_req_sent_times[k]

            # --- Stale pending-token / opportunistic-req bookkeeping --------
            # For senders that stashed tokens but never completed RNSBIND
            # (e.g. they went out of range for good). Uses the same
            # peer_ttl_s window as bound peers.
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

            # --- Expired path-response bypass entries -----------------------
            # Cleans up cases where the expected outgoing announce never
            # happened (e.g. we don't actually own/have a path to the
            # requested destination), so entries don't accumulate forever.
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
            await self._process_tunnel_text(text[rns_idx:], sender, rx_mode="CHANNEL")

    async def _on_direct_msg(self, event):
        payload = event.payload
        sender_key = (
            payload.get("pubkey_prefix") or payload.get("sender_pubkey") or
            payload.get("pubkey")        or payload.get("from_pubkey") or ""
        )
        text = payload.get("text", "")
        if not text.startswith(self.MSG_PREFIX):
            return
        sender_id = self._resolve_sender_key(sender_key)
        await self._process_tunnel_text(text, sender_id, rx_mode="DIRECT")

    async def _on_msg_ack(self, event):
        pass

    async def _on_mc_connected(self, event):
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

    def _register_peer_binding(self, sender_name: str, mc_pubkey: str,
                              can_route: bool = True):
        if not sender_name or not mc_pubkey:
            return False

        with self._peer_lock:
            existing    = self._peer_table.get(sender_name)
            cap_changed = self._peer_caps.get(sender_name) != can_route

            if existing != mc_pubkey:
                self._peer_table[sender_name]  = mc_pubkey
                self._reverse_peers[mc_pubkey] = sender_name
                for pfx_len in (8, 12, 16, 24):
                    pfx = mc_pubkey[:pfx_len]
                    if pfx:
                        self._reverse_peers[pfx] = sender_name

            self._peer_caps[sender_name]      = can_route
            self._peer_last_seen[sender_name] = time.monotonic()

        changed = (existing != mc_pubkey) or cap_changed
        if changed:
            RNS.log(
                f"MeshCore_Dynamic_Interface [{self.name}]: "
                f"Bound peer '{sender_name}' -> {mc_pubkey[:16]}... "
                f"[{'router' if can_route else 'edge — no upstream routing'}]",
                RNS.LOG_INFO
            )
        return changed

    async def _handle_bind(self, text: str, bind_idx: int, req_idx: int = -1):
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

        cap_value = contact.get("can_route", True)
        if isinstance(cap_value, str):
            cap_value = cap_value.lower() not in ("no", "false", "0", "edge")
        if not isinstance(cap_value, bool):
            cap_value = bool(cap_value)

        name = (
            contact.get("adv_name")
            or contact.get("name")
            or contact.get("node_name")
            or contact.get("advertised_name")
            or ""
        )
        name = str(name).strip()

        if name:
            self._register_peer_binding(name, key, cap_value)

    async def _on_meshcore_contact_event(self, event):
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

    async def _process_tunnel_text(self, text: str, sender: str = "", rx_mode: str = "UNKNOWN"):
        if sender and sender == self._own_node_name:
            return

        z85_text = text[len(self.MSG_PREFIX):].strip()
        try:
            raw = z85_decode(z85_text)
        except Exception as exc:
            self._debug(
                f"Dropped unparsable {rx_mode} fragment from '{sender}' "
                f"({len(z85_text)} char(s)): {exc}."
            )
            return

        # Header unpacked big-endian matching structural change (1B index, 4B packet ID, 1B total fragments)
        if len(raw) < self.HEADER_SIZE:
            self._debug(
                f"Dropped {rx_mode} fragment from '{sender}' -- decoded to "
                f"{len(raw)}b, shorter than the {self.HEADER_SIZE}b header."
            )
            return

        frag_idx, pkt_id, frag_total = struct.unpack(">BIB", raw[:6])
        payload    = raw[self.HEADER_SIZE:]

        if frag_total == 0 or frag_idx >= frag_total:
            self._debug(
                f"Dropped {rx_mode} fragment from '{sender}' -- invalid "
                f"header (frag_idx={frag_idx}, frag_total={frag_total})."
            )
            return

        key = (sender, pkt_id)
        now = time.monotonic()

        # Sliding time-window deduplication check
        with self._seen_lock:
            if key in self._seen_pkts:
                if now < self._seen_pkts[key]:
                    self._debug(
                        f"Duplicate pkt_id {pkt_id} from '{sender}' suppressed "
                        f"(already fully reassembled within the last "
                        f"{self.DEDUPLICATION_TTL_S:.0f}s -- likely a "
                        f"retransmit pass or overheard repeat)."
                    )
                    return
                else:
                    del self._seen_pkts[key]

        # Fragment reassembly
        with self._asm_lock:
            if key not in self._assembly:
                self._assembly[key]      = {}
                self._assembly_meta[key] = (frag_total, now)

            if frag_idx in self._assembly[key]:
                self._debug(
                    f"Duplicate fragment idx={frag_idx} for pkt_id {pkt_id} "
                    f"from '{sender}' ignored (already have it, "
                    f"{len(self._assembly[key])}/{frag_total} collected)."
                )
                return

            self._assembly[key][frag_idx] = payload

            if len(self._assembly[key]) < self._assembly_meta[key][0]:
                return  

            try:
                expected    = self._assembly_meta[key][0]
                full_packet = b"".join(
                    self._assembly[key][i] for i in range(expected)
                )
                del self._assembly[key]
                del self._assembly_meta[key]
            except Exception:
                self._assembly.pop(key, None)
                self._assembly_meta.pop(key, None)
                return

        # Mark as completely reassembled inside sliding time window
        with self._seen_lock:
            self._seen_pkts[key] = now + self.DEDUPLICATION_TTL_S

        if not full_packet:
            return

        rns_token = self._extract_rns_token(full_packet)
        if rns_token is not None and sender:
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
        if full_packet:
            ptype = full_packet[0] & 0x03
            ptype_str = {
                0x00: "DATA",
                0x01: "ANNOUNCE",
                0x02: "LINK_REQ",
                0x03: "PROOF"
            }.get(ptype, "UNKNOWN")
            
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
                and len(full_packet) >= 12
            ):
                dest_id = bytes(full_packet[2:12])
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
        return self.processOutgoing(data)

    def processOutgoing(self, data):
        if not self.online:
            return

        hdr_byte  = data[0] if data else 0
        ptype     = hdr_byte & 0x03         
        dest_type = (hdr_byte >> 2) & 0x03  
        perf_start = time.monotonic()

        

        # Per-destination outgoing announce rate limiter. Bypassed for
        # announces that are answering a path request we recently saw come
        # in for this same destination (see _path_response_pending) -- those
        # are demand-driven responses, not spontaneous re-announces, and
        # dropping them silently is what causes intermittent "path request
        # timed out" failures on the requesting side when a routine
        # self-announce happened to go out shortly beforehand.
        if self._announce_rate_s > 0 and len(data) >= 12:
            if ptype == self._RNS_PTYPE_ANNOUNCE:
                dest_id = bytes(data[2:12])
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
                            return
                else:
                    RNS.log(
                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                        f"Announce for {dest_id.hex()[:8]} bypassing rate "
                        f"limiter -- answering a recent path request.",
                        RNS.LOG_INFO
                    )

                with self._announce_sent_lock:
                    self._announce_sent_times[dest_id] = now

        # Per-destination outgoing path request rate limiter, with a burst
        # window that lets RNS's own natural retry cluster through before the
        # long-run anti-spam cooldown kicks in. See path_req_burst_window
        # comment in __init__ for rationale.
        if self._path_req_rate_s > 0 and len(data) >= 12:
            if ptype == self._RNS_PTYPE_DATA and dest_type == self._RNS_DTYPE_PLAIN:
                dest_id = bytes(data[2:12])
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
                            return
                        else:
                            # Cooldown expired -- this starts a fresh burst.
                            self._path_req_sent_times[dest_id] = (now, now)

        with self._pkt_id_lock:
            pkt_id       = self._pkt_id
            self._pkt_id = (self._pkt_id + 1) & 0xFFFFFFFF  # 32-bit bound integer tracking

        handler   = _PacketHandler(data, pkt_id, self._auto_payload_size())
        
        broadcast = self._is_broadcast_packet(data)
        
        #Log Fragmentation Performance Metrics
        RNS.log(
            f"[PERF {pkt_id}] OUT "
            f"size={len(data)} "
            f"ptype={ptype} "
            f"broadcast={broadcast}",
            RNS.LOG_INFO
        )
        
        
        
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
            route = [("channel", None)]
        else:
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
            route = [("direct", target_key)]
            
        # route always holds exactly one (mode, target) pair -- unpack once
        # rather than re-iterating a single-element list per fragment.
        mode, target = route[0]
        outq = self._direct_outqueue if mode == "direct" else self._channel_outqueue

        # LINK_REQUEST and PROOF jump ahead of ordinary DATA/ANNOUNCE already
        # waiting in the queue -- see _PRIORITY_HANDSHAKE for rationale.
        priority = (
            self._PRIORITY_HANDSHAKE
            if ptype in (self._RNS_PTYPE_LINK_REQ, self._RNS_PTYPE_PROOF)
            else self._PRIORITY_NORMAL
        )

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

        # Schedule extra passes for broadcast-only packet types. Path
        # RESPONSES aren't a distinct packet type in this system -- they're
        # just an ANNOUNCE that happened to be triggered by an inbound path
        # request (see _path_response_pending above) -- so they're already
        # covered by the announce_retransmit_extra branch below with no
        # separate handling needed.
        # ordinary_data_retransmit_extra only makes sense for non-broadcast
        # packets that ended up on the unacknowledged CHANNEL path (e.g. no
        # bound peer / no resolved route yet). A DIRECT send is already
        # ACK'd by the firmware (see _async_outgoing_worker), so blindly
        # retransmitting it too would just double-deliver a packet that's
        # already confirmed received.
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

        self.txb += len(data)

    async def _delayed_retransmits(self, fragments, route, count, broadcast, priority):
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

            # Age alone isn't a good enough reason to drop a fragment -- one
            # that's simply had bad luck on an otherwise quiet queue will get
            # sent in a moment regardless, and dropping it gains nothing.
            # Only sacrifice it when there's an ACTUAL backlog behind it too
            # (depth_behind, i.e. how many items are still waiting once this
            # one is removed): that's the case where continuing to send it
            # anyway is genuinely costing everything queued after it more
            # time, and where the RNS-level request/Link that created it
            # (RNS's own Link establishment timeout is typically a handful
            # of seconds per hop -- see stale_fragment_max_age in __init__)
            # has, in all likelihood, already given up. Broadcast packets
            # are exempt regardless -- they have no single requester to give
            # up, and a late announce/path-response is still useful
            # network-wide.
            if (
                not broadcast
                and self.stale_fragment_max_age_s > 0
                and queue_wait > self.stale_fragment_max_age_s
                and depth_behind >= self.stale_fragment_min_queue_depth
            ):
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
                outq.task_done()
                continue

            try:
                if mode == "direct":
                    # A lost delivery ACK on the return trip doesn't mean the
                    # forward frame was lost -- retrying the DIRECT send a
                    # couple of times is cheaper and far less airtime-hungry
                    # than immediately escalating to a broadcast CHANNEL
                    # resend. Mirrors send_msg_with_retry's multi-attempt
                    # behavior in the official meshcore client, instead of
                    # giving DIRECT exactly one shot before falling back.
                    max_direct_attempts = max(1, self.direct_send_attempts)
                    last_exc = RuntimeError("direct send failed")
                    ack_received = False

                    for attempt in range(1, max_direct_attempts + 1):
                        try:
                            result = await self._mc.commands.send_msg(target, frag_str)
                            if result is None or result.type != self._EventType.MSG_SENT:
                                reason = (
                                    result.payload.get("reason", "no path/unknown")
                                    if result is not None else "no response"
                                )
                                raise RuntimeError(f"direct send rejected: {reason}")

                            # CORRECTED UNDERSTANDING: MSG_SENT only confirms the
                            # local radio queued the frame for transmission -- it
                            # is NOT end-to-end delivery confirmation. The
                            # firmware hands back an "expected_ack" tag in the
                            # MSG_SENT payload; actual over-air delivery is
                            # confirmed later (if at all) by a separate ACK event
                            # carrying that same tag. Without waiting on it, a
                            # frame that never reaches the peer (out of range,
                            # collision, stale/broken path) is indistinguishable
                            # from one that was delivered.
                            exp_ack = result.payload.get("expected_ack")
                            if exp_ack is None:
                                ack_received = True
                                break

                            exp_ack_hex = (
                                exp_ack.hex() if isinstance(exp_ack, (bytes, bytearray))
                                else str(exp_ack)
                            )
                            suggested_ms = result.payload.get("suggested_timeout", 0) or 0
                            # NOTE: for a contact with out_path_len == -1 (no known
                            # route -- flood mode), the firmware's suggested_timeout
                            # can be very large, since it has to budget for a full
                            # flood-and-wait cycle. The DIRECT queue has its own
                            # worker task (separate from CHANNEL, see
                            # _async_outgoing_worker) -- an uncapped wait here can
                            # no longer stall channel broadcasts, but it would
                            # still stall every other queued DIRECT fragment
                            # (to this or any other peer) for however long the
                            # firmware suggests, which can be minutes. We
                            # deliberately cap it: our own CHANNEL fallback is
                            # cheap, so there's no reason to let one flood-mode
                            # contact block the rest of the direct queue for as
                            # long as the radio itself would wait.
                            raw_ack_timeout = max(
                                self.direct_ack_timeout_s, (suggested_ms / 1000.0) * 1.2
                            )
                            ack_timeout = min(raw_ack_timeout, self.direct_ack_timeout_max_s)
                            if raw_ack_timeout > ack_timeout:
                                RNS.log(
                                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                                    f"Firmware suggested {suggested_ms}ms ACK timeout for "
                                    f"peer key {target[:12] if target else '?'}... "
                                    f"(likely flood/no-path) -- capping wait at "
                                    f"{ack_timeout:.1f}s instead of {raw_ack_timeout:.1f}s "
                                    f"to avoid blocking the outgoing queue.",
                                    RNS.LOG_INFO
                                )
                            ack = await self._mc.dispatcher.wait_for_event(
                                self._EventType.ACK,
                                attribute_filters={"code": exp_ack_hex},
                                timeout=ack_timeout,
                            )
                            if ack is None:
                                raise RuntimeError(
                                    f"no delivery ACK within {ack_timeout:.1f}s "
                                    f"(expected_ack={exp_ack_hex}, "
                                    f"firmware suggested {suggested_ms}ms)"
                                )

                            #Ack received -- log success and continue to next fragment
                            RNS.log(
                                f"MeshCore_Dynamic_Interface [{self.name}]: "
                                f"Direct send to peer key {target[:12] if target else '?'}... "
                                f"ACK received (expected_ack={exp_ack_hex}).",
                                RNS.LOG_INFO
                            )
                            ack_received = True
                            break
                        except Exception as exc:
                            last_exc = exc
                            if attempt < max_direct_attempts:
                                RNS.log(
                                    f"MeshCore_Dynamic_Interface [{self.name}]: "
                                    f"DIRECT send attempt {attempt}/{max_direct_attempts} "
                                    f"to peer key {target[:12] if target else '?'}... "
                                    f"failed ({exc}) -- retrying.",
                                    RNS.LOG_INFO
                                )

                    if not ack_received:
                        raise last_exc

                    # A successful DIRECT delivery means whatever path is
                    # currently cached for this peer is working -- clear any
                    # accumulated failure count so a future blip doesn't
                    # inherit credit toward resetting a path that just proved
                    # itself fine.
                    with self._path_req_lock:
                        self._direct_path_failures.pop(target, None)
                else:
                    await self._mc.commands.send_chan_msg(self.channel_idx, frag_str)
            except Exception as exc:
                if mode == "direct":
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
                                    asyncio.run_coroutine_threadsafe(self.discover_path(contact), self._loop) # Request path

                            # Separate from discovery above: track repeated
                            # failures against this peer's CURRENTLY CACHED
                            # path specifically (0-hop or multi-hop -- any
                            # opl != -1). Verified empirically on a real
                            # link: a cached path can go stale (repeater
                            # repositioned, shorter route now exists) while
                            # remaining stuck in the contact table, and
                            # continuing to retry a stale path fails far more
                            # often than resetting it to flood mode and
                            # letting the firmware find whatever route
                            # currently works.
                            if opl != -1 and self.direct_path_reset_threshold > 0:
                                with self._path_req_lock:
                                    fail_count = self._direct_path_failures.get(target, 0) + 1
                                    self._direct_path_failures[target] = fail_count
                                if fail_count >= self.direct_path_reset_threshold:
                                    with self._path_req_lock:
                                        self._direct_path_failures[target] = 0
                                    RNS.log(
                                        f"MeshCore_Dynamic_Interface [{self.name}]: "
                                        f"Peer key {target[:12] if target else '?'}... has failed "
                                        f"{fail_count} consecutive DIRECT send(s) on its cached "
                                        f"path (out_path_len={opl}) -- resetting to flood mode "
                                        f"instead of continuing to retry what looks like a stale "
                                        f"route.",
                                        RNS.LOG_WARNING
                                    )
                                    if self._loop is not None:
                                        asyncio.run_coroutine_threadsafe(
                                            self._mc.commands.reset_path(contact), self._loop
                                        )
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
                outq.task_done()
                continue

            delay = (
                self.direct_frag_delay_s
                if mode == "direct"
                else self.fragment_delay_s
            )
            # NOTE: with independent DIRECT/CHANNEL workers, rate_limit_bps is
            # now enforced per-queue rather than as one combined interface-wide
            # cap -- each stream paces itself to the configured bps rather than
            # the two sharing a single budget.
            if self.rate_limit_bps > 0:
                bits  = (len(frag_str) * 3 // 4) * 8
                delay = max(delay, bits / self.rate_limit_bps)

            await asyncio.sleep(delay)
            outq.task_done()

    # -------------------------------------------------------------------------
    # Inbound delivery
    # -------------------------------------------------------------------------

    def processIncoming(self, data: bytes):
        if self.online and not self.detached:
            self.rxb += len(data)
            self.owner.inbound(data, self)

    def __str__(self):
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
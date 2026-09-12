# Reticulum (RNS) over MeshCore Interface

A packet aware [Reticulum Network Stack (RNS)](https://reticulum.network/) interface that tunnels RNS traffic over a [MeshCore](https://meshcore.co.uk/) LoRa mesh. It requires no static remote-node configuration — peers discover each other dynamically over the air — and uses a hybrid channel-broadcast / unicast-direct routing strategy to keep airtime usage on a shared, half-duplex LoRa channel as low as possible.

> [!NOTE]
> This is my fork of [comms-engineer's RNS_Over_Meshcore](https://github.com/comms-engineer/RNS_Over_Meshcore). Both this project, and the source project make use of AI.

> [!WARNING]
> This project is currently in an early experimental state and will not run reliably. Documentation may not be up to date

## Credits
[comms-engineer's RNS_Over_Meshcore](https://github.com/comms-engineer/RNS_Over_Meshcore) - project that this repo is forked from.

[Reticulum Network Stack (RNS)](https://reticulum.network/) - Self explanatory 

[meshcore_py](https://github.com/meshcore-dev/meshcore_py) - MeshCore API dependency

[Meshcore CLI](https://github.com/meshcore-dev/meshcore-cli) - Used this as reference for implementation of meshcore_py calls

## Project Goals
RNS ships interfaces for TCP, serial, I2P, packet radio, and a handful of others, but nothing that speaks directly to MeshCore firmware. This interface fills that gap: it fragments and re-assembles RNS binary packets into MeshCore channel/direct messages, and layers a lightweight peer-discovery and routing protocol on top so that Reticulum can run natively over a MeshCore LoRa network — including in mixed deployments where a MeshCore mesh acts as the "last mile" for an existing RNS transport backbone.
### Respect for MeshCore users
This project aims to use the existing Lora infrastructure built by MeshCore users in a way that is respectful. The interface should function well without flooding a MeshCore mesh by intelligently drop or delaying traffic and making the most out of any airtime used.

### Reliability & Ease of use
The current Reticulum landscape requires a certain level of tech literacy to setup and use. This project aims to remove the tinkering that may be required to get a similar solution working.
Ideally, no config options other than mode & radio settings should be required to setup.

## Features

- **Planned** - not yet being worked on
- **Unstable** - Feature implemented but unreliable in basic testing
- **Basic** - Only a basic version of this feature has been implemented.
- **Working** - Feature implemented and seemingly working but only lightly tested
- **Battle Tested** - Feature implemented, highly confident in feature after testing in real scenarios

| Feature    | State     | Description            |
|------------|-----------|------------------------|
| Hybrid routing (Channel & Direct) | Working | The interface caches which RNS links belong to which MeshCore contact. This allows traffic to be sent directly instead of flooding all traffic. |
| Automatic Peer Discovery | Battle Tested | The interface discovers any peers set to the same MeshCore channel and private key. |
| Z85 Encode/Decode | Battle Tested | Instead of Base64, Z85 encoding is used to parse reticulum packets through MeshCore messages. Z85 theoretically expands data by 25% compared to 33% when using base64|
| Zero static config peer discovery | Battle Tested | nodes find each other with a demand-driven `RNSBIND_REQ` / `RNSBIND` handshake instead of periodic broadcast, based on the RFC 2236 (IGMP) report-suppression pattern to avoid |
| Capability-aware discovery | Working | peers advertise whether they can carry transit traffic (`R` router / `E` edge) at discovery time, useful for distinguishing infrastructure nodes from battery-powered edge devices.|
| Automatic Packet Fragmentation | Basic | Fragments Reticulum packets into MeshCore sized messages. Determines the optimal size of fragments to be sent over MeshCore |
| Multiple transports | Working | connects to the MeshCore node over serial, TCP, or BLE. |
| Auto-reconnect | Working | automatically re-establishes the serial/BLE/TCP link if it drops, up to a configurable number of attempts, with connection-lifecycle logging. |
| Rate limiting | Basic | independent throttles for outgoing announces, path requests, and (optionally) a hard bitrate cap, to keep the interface well-behaved on congested or bandwidth-constrained channels. - Plans to deprecate rate limiting in favor of an automatic solution |
| Delivery aware sending | Working | waits on the MeshCore firmware's `expected_ack` / `ACK` event pair for unicast messages rather than trusting the immediate `MSG_SENT` result, with a bounded timeout so a single slow/flood-mode peer can't stall the shared outgoing queue. Retries a failed direct send a couple of times before falling back to `CHANNEL`. |
| Independent direct/channel queues | Working | direct and channel traffic are queued and processed independently, so a slow or retrying direct send can't stall channel broadcasts (or other direct sends), and vice versa. |
| Priority queueing | Working | `LINK_REQUEST`/`PROOF` packets jump ahead of ordinary `DATA`/`ANNOUNCE` traffic already waiting in the outgoing queue, so link establishment isn't stuck behind a bulk transfer. |
| Stale fragment dropping | Working | a fragment that's sat in the outgoing queue too long *and* has a real backlog behind it is dropped instead of sent, since the requester has very likely already given up (e.g. a NomadNet link attempt that already timed out). |
| Adaptive path-discovery backoff & retry | Working | path-discovery requests retry a few times back-to-back before giving up (a single lost broadcast is normal on LoRa), then back off exponentially per-peer so a persistently-broken hop isn't hammered forever. |
| Path-discovery persistence | Working | MeshCore's dedicated path-discovery command tells the requesting client a path but — by firmware design — never writes it into the device's own persistent contact table. The interface now explicitly persists a successfully-discovered path itself, so the path is also visible to the official MeshCore app on the next connection. |
| Stale path detection & reset | Working | a cached path can go stale (a repeater moves, a shorter route opens up) while remaining stuck in the contact table; repeated direct-send failures against a peer's cached path now trigger a reset to flood mode rather than continuing to retry a route that's proven itself dead. |
| Raw Binary Send | Planned | Right now this project uses Z85 encoding as a more size efficient alternative to Base64 encoding. I've made the decision to wait for SEND_RAW_DATA to be fully implemented in meshcore_py before implementing this feature |
| Compatibility other RNS over MeshCore interfaces | Planned | Support discovery & compatibility with other MeshCore interfaces where possible |
| Announce Priority By RNS Hop Count | Planned | On Transfer Nodes; cache and queue announces for Reticulum nodes and prioritise based on hop count. |

## Getting Started

### Requirements

- Python 3.9+
- [Reticulum (`rns`)](https://pypi.org/project/rns/)
- The [`meshcore`](https://pypi.org/project/meshcore/) Python library
- A MeshCore-flashed radio (or a MeshCore companion app reachable over TCP/BLE) reachable from the host running `rnsd`

```bash
pip install rns meshcore
```

### Installation

1. Copy `MeshCore_Dynamic_Interface.py` into your Reticulum config's `interfaces` directory (typically `~/.reticulum/interfaces/`).
2. Add an interface block to `~/.reticulum/config` (see [Configuration](#configuration) below).
3. Restart `rnsd`, or reload interfaces if your setup supports it.
4. If you leave `channel_idx`/`channel_name`/`channel_secret` unset, every node defaults to the same shared "RNSTunnel" channel, so nodes can find each other with zero coordination. Set all three explicitly (and match them across your own nodes) if you want a private channel instead.

### Configuration

Every node needs at minimum a transport block. Channel identity is optional — the defaults below already match across all nodes running this interface unmodified. A full infrastructure/transport-node example:

```ini
[reticulum]
  enable_transport = yes
  share_instance = yes

[logging]
  loglevel = 4    # increase to 7 for debug

[interfaces]

  [[MeshCore Dynamic Interface]]
    type = MeshCore_Dynamic_Interface
    interface_enabled = yes

    # Role
    mode = access_point
    can_route = yes

    # Transport — uncomment exactly one block
    # Serial (most common):
    transport = serial
    port = /dev/ttyUSB0
    baudrate = 115200
    #
    # TCP (MeshCore node reachable over IP):
    # transport = tcp
    # host = 127.0.0.1
    # tcp_port = 4403
    #
    # BLE:
    # transport = ble
    # ble_name =            # blank = connect to first found device

    # Channel — defaults join a shared public channel with zero coordination
    # needed. RNS already encrypts and authenticates your actual traffic
    # end-to-end, so a publicly-known default channel secret isn't a
    # security concern here — it only selects which MeshCore LoRa channel
    # this radio joins, like a WiFi SSID, not what's encrypted on top of it.
    # Uncomment and set your own values for a private channel instead (all
    # nodes on it must then match these three settings).
    # channel_idx = 0
    # channel_name = RNSTunnel
    # channel_secret = <32 hex chars>   # openssl rand -hex 16

    # Radio overrides — all four must be non-zero to take effect.
    # Leave commented to use the values already stored on the MeshCore node.
    # freq = 915.0        # MHz centre frequency
    # bw   = 250.0        # kHz bandwidth (125 / 250 / 500)
    # sf   = 10            # spreading factor (7-12)
    # cr   = 5             # coding rate denominator (5=4/5 ... 8=4/8)

    # Fragmentation
    payload_size = 64         # bytes/fragment - see "Payload size" below
    fragment_delay = 1.0      # seconds between channel-mode fragments
    direct_frag_delay = 0.5   # seconds between direct-message fragments
    fragment_timeout = 300    # 5-minute reassembly window for high-latency meshes

    # Outgoing rate limiting (set to 0 to disable)
    outgoing_announce_rate = 600     # min seconds between announces per dest
    outgoing_path_req_rate = 1800    # min seconds between path requests per dest

    # Optional hard bandwidth cap in bits per second (0 = disabled)
    # rate_limit = 1200

    # Peer discovery
    allow_direct = yes    # use unicast direct messages when a route is known
    peer_ttl = 86400      # seconds before a silent peer expires

    debug_level = info    # info | debug

  [[Backbone Interface]]
    type = BackboneInterface
    interface_enabled = yes
    mode = boundary
    target_host = <backbone-server-hostname-or-ip>
    target_port = 4242
    # Rate-limit announce re-propagation from the fast network
    announce_rate_target  = 3600
    announce_rate_grace   = 2
    announce_rate_penalty = 7200
```

### Interface mode

Mode selection has a real impact on announce traffic, path expiry, and channel load — get it wrong and a LoRa channel can be flooded indefinitely.

**`access_point`** (recommended for infrastructure/transport nodes with backbone connectivity)
Announces are not automatically re-broadcast on this interface, and paths to destinations behind it expire faster, matching the transient nature of battery-powered or intermittently-connected field devices. Path requests from clients are still forwarded and resolved on their behalf.

> **Note:** AP mode only suppresses `ANNOUNCE` re-broadcasting. `DATA`+`PLAIN` path requests from the wider mesh for a recently-offline node still pass through AP mode onto the LoRa channel. Use `outgoing_path_req_rate` to throttle these independently.

> ⚠️ **Never use `gateway` mode on a LoRa interface on a node that is also connected to a high-connectivity backbone.** Gateway mode proactively pushes *all* known announces to clients on that interface — with thousands of routes on the public Reticulum mesh, this will flood a shared LoRa channel indefinitely.

**`boundary`**
Applied to the backbone/TCP interface connecting the slow radio segment to a fast LAN or the internet. Marks the network edge so the transport node doesn't treat the backbone as a client-facing interface for proactive path distribution.

Add announce rate control to the backbone interface to throttle how quickly announces from the wider network are re-propagated onto the radio side:

```ini
announce_rate_target  = 3600   # min seconds between re-announces per dest
announce_rate_grace   = 2      # violations tolerated before enforcement
announce_rate_penalty = 7200   # extended quiet period after a violation
```

### Payload size

MeshCore firmware silently truncates channel messages beyond a hardware-dependent character limit (commonly ~128 chars). The firmware also prepends the sender's node name when relaying channel messages, so the usable character budget for the encoded fragment is:

```
budget = firmware_limit - len(node_name) - 2        # ": " separator
```

Encoded message length for a given payload size:

```
msg_len = ceil((payload_size + HEADER_SIZE) * 4/3) + len("RNS:")
```

With the default 6-byte header and `payload_size = 64`:

```
msg_len = ceil(70 * 4/3) + 4 = 98 chars   →  safe for node names up to ~28 characters at a 128-char firmware limit
```

To size `payload_size` for your own node name length:

```
budget      = firmware_limit - len(node_name) - 2
max_payload = floor((budget - 4) * 3/4) - HEADER_SIZE
```

## How it works

### Wire format

Each RNS binary packet is split into `payload_size`-byte chunks. Each chunk is encoded as a MeshCore channel (or direct) message:

```
"RNS:" + Z85 Encode( [frag_idx:1][pkt_id:4][frag_total:1] + payload )
```

### Peer discovery

Discovery is demand-driven rather than push/periodic, to minimize channel airtime:

1. A node with no known peers sends `RNSBIND_REQ:<pubkey>:<cap>` on the channel, advertising its own routing capability alongside its identity.
2. Overhearing nodes immediately record the requester (passive learning), wait a random backoff (`BIND_BACKOFF_MIN`–`BIND_BACKOFF_MAX` seconds), then reply with `RNSBIND:<pubkey>:<cap>`. The randomized backoff spreads responses out in time to avoid a simultaneous burst on the shared half-duplex channel.
3. Every node overhearing *any* `RNSBIND` response also records the responder, so a single discovery round passively populates every peer table on the channel.
4. Once peers are known, a quiet `RNSBIND` heartbeat goes out every `BIND_HEARTBEAT_S` (default: 1 hour) — no response is solicited.

The capability suffix (`R` = router, `E` = edge) tells peers at discovery time whether a node has upstream connectivity worth routing transit traffic through. It's recorded and logged but doesn't gate per-packet routing decisions — the interface's live route map is built from observed packet flow, and a path that has demonstrably worked (including through an edge node to reach a downstream client) is used regardless of the advertised capability.

### RNS header parsing

The interface inspects the RNS header byte to distinguish packet types (`DATA`, `ANNOUNCE`, `LINKREQUEST`, `PROOF`) and destination types (`SINGLE`, `GROUP`, `PLAIN`, `LINK`), and locally derives the destination hash for established Links (whose destination field becomes an ephemeral Link ID after handshake) so that direct-message routing continues to work for the life of the Link.

### Delivery confirmation

A MeshCore `MSG_SENT` result only confirms the local radio queued the frame — it isn't end-to-end delivery confirmation. For direct sends, the interface waits on the firmware's follow-up `ACK` event (matched via the `expected_ack` tag from `MSG_SENT`), bounded by `direct_ack_timeout` and a hard ceiling `direct_ack_timeout_max` so that a flood-mode peer with a long firmware-suggested timeout can't stall every other fragment behind it in the shared outgoing queue. A failed direct send is retried (`direct_send_attempts`) before falling back to a channel broadcast.

### Path staleness, discovery, and persistence

MeshCore's dedicated path-discovery command tells *the requesting client* a discovered path, but — by firmware design — it does not write that path into the device's own persistent contact table the way an ordinary message exchange does. Left alone, this means a path the interface believes it "discovered" can still show as unresolved to the official MeshCore app on its next connection. The interface now explicitly persists a successfully-discovered path back to the device itself, closing that gap.

Separately, a path that *is* persisted can still go stale over time — a repeater repositions, or a shorter route opens up — while remaining stuck in the contact table. Continuing to retry a stale path measured far worse in testing than simply resetting it: the interface tracks consecutive direct-send failures against a peer's cached path, and once `direct_path_reset_threshold` is reached, resets that peer to flood mode so the next attempt can find whatever route currently works instead of retrying a route that's already proven dead.

### Outgoing queue behavior

Direct and channel traffic are queued and processed independently (`_direct_outqueue` / `_channel_outqueue`), so a direct send that's slow or retrying can't stall channel broadcasts, and vice versa. Within each queue, `LINK_REQUEST`/`PROOF` packets are prioritized ahead of ordinary `DATA`/`ANNOUNCE` traffic, so a link-establishment attempt isn't stuck behind a bulk transfer. A fragment that's been queued longer than `stale_fragment_max_age` *and* still has a real backlog behind it (`stale_fragment_min_queue_depth`) is dropped rather than sent — age alone is never enough, since a fragment that simply had bad luck on an otherwise quiet queue will get sent momentarily regardless.

## Transports

| Transport | Config keys |
|---|---|
| Serial (default) | `port`, `baudrate` |
| TCP | `host`, `tcp_port` |
| BLE | `ble_name` (blank = connect to first device found) |

## Tuning reference

| Key | Default | Purpose |
|---|---|---|
| `channel_idx` | `0` | MeshCore channel index. Leave unset to use the shared default channel. |
| `channel_name` | `RNSTunnel` | MeshCore channel name. Leave unset to use the shared default channel. |
| `channel_secret` | *(shared default)* | MeshCore channel encryption key (32 hex chars). Sharing the default isn't an RNS security concern — see [Configuration](#configuration) — but set your own for a private channel. |
| `payload_size` | `64` | Fragment payload size in bytes; see [Payload size](#payload-size) |
| `fragment_delay` | `1.0` | Seconds between channel-mode fragments |
| `direct_frag_delay` | `0.5` | Seconds between direct-message fragments |
| `fragment_timeout` | `300` | Reassembly window for incomplete multi-fragment packets |
| `direct_ack_timeout` | `4.0` | Minimum wait for a direct-send delivery ACK |
| `direct_ack_timeout_max` | `8.0` | Hard ceiling on the ACK wait regardless of firmware suggestion |
| `direct_send_attempts` | `3` | Retries for a direct send (fresh ACK wait each time) before falling back to `CHANNEL` |
| `path_discovery_quick_attempts` | `3` | Back-to-back path-discovery retries before handing off to the exponential backoff below |
| `path_discovery_base_cooldown` | `15.0` | Cooldown after the first path-discovery failure round for a peer |
| `path_discovery_max_cooldown` | `900.0` | Ceiling on the path-discovery backoff, regardless of consecutive failures |
| `path_discovery_backoff_factor` | `2.0` | Multiplier applied to the cooldown per additional failure round |
| `direct_path_reset_threshold` | `2` | Consecutive fully-exhausted direct-send failures against a peer's *cached* path before resetting it to flood mode (`0` disables) |
| `stale_fragment_max_age` | `30.0` | Seconds a fragment may sit in the outgoing queue before it's eligible to be dropped (`0` disables) |
| `stale_fragment_min_queue_depth` | `10` | Fragments must also be backed up at least this many deep before dropping kicks in — age alone is never enough |
| `contact_refresh_interval` | `30.0` | Seconds between periodic re-fetches of MeshCore's contact list, so cached path info doesn't go stale between events (local query only, no mesh airtime cost) |
| `outgoing_announce_rate` | `600` | Minimum seconds between announces per destination (`0` disables) |
| `outgoing_path_req_rate` | `1800` | Minimum seconds between path requests per destination (`0` disables) |
| `rate_limit` | `0` | Optional hard bandwidth cap in bits/second (`0` disables) |
| `allow_direct` | `yes` | Use unicast direct messages when a route to the peer is known |
| `peer_ttl` | `86400` | Seconds before a silent peer is dropped from the peer table |
| `can_route` | `yes` | Whether this node can carry transit traffic |
| `auto_reconnect` | `yes` | Automatically try to re-establish the link if it drops |
| `max_reconnect_attempts` | `3` | Reconnect attempts before giving up (only relevant if `auto_reconnect` is enabled) |
| `debug_level` | `info` | `info` or `debug` — `debug` enables this interface's own verbose diagnostic logs independently of RNS core's global `loglevel`, so you get interface-level detail without RNS core's own debug firehose |

## Limitations

- MeshCore's channel-message character limit varies by firmware build and must be accounted for when choosing `payload_size` (see [Payload size](#payload-size)).
- `access_point` mode suppresses announce re-broadcasting but not `DATA`+`PLAIN` path requests; a node that flaps offline can still generate path-request traffic on the LoRa channel from remote nodes searching for it. Use `outgoing_path_req_rate` to bound this.


## Field Tests

Just so you have realistic expectations :)

Using 'RNS Hops' and 'slow' is a bit ambiguous, but until I come up with better testing methodology, this is what you get. Keep in mind, a connection over Meshcore only counts as one hop, regardless of the amount of repeaters.

|     | Direct     | 1x Repeater | 2x Repeater |
|-----|-----------|-----------------|------|
| MeshChat DM (3 Total RNS Hops) | Working | Working | Not tested |
| MeshChat DM (6 Total RNS Hops) | Working | Working | Not tested |
| NomadNet (3 Total RNS Hops)| Working | Working | Not tested |
| NomadNet (5 Total RNS Hops)| Working | Working | Not tested |

- The `1x Repeater` column was retested after adding independent direct/channel queues, priority queueing, stale-fragment dropping, path-discovery persistence, and stale-path reset-to-flood — all previously "slow"/"not working" cases over a single repeater are now working reliably. `2x Repeater` hasn't been retested against these fixes yet.
- This interface is built and tested against a specific `meshcore` library API surface; firmware/library version drift may require updates to event/attribute names.

Yes, I absolutely had help from Claude on this. I'm not a software person, I'm just stubborn enough to think I can beat my head against something until it works. PLEASE feel free to offer improvements and corrections.


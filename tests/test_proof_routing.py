"""
Tests for routing a PROOF packet DIRECT to the peer whose original packet
it's proving (Interface/MeshCore_Dynamic_Interface.py).

Background: a PROOF packet's destination-hash field is not a stable
identity -- per RNS.Packet.ProofDestination, it IS the truncated hash of
the original packet being proved, so it's different for every packet and
can never land in _rns_to_mc_map the way a Link's reused ephemeral ID
does (_extract_rns_token/_learn_rns_token_binding). Found in live field
testing: with force_direct_path correctly pinning a peer's MeshCore
out_path, real DIRECT sends over that path worked (97% success), but
PROVE_ALL delivery receipts for one-off packets still fell back to
CHANNEL every time ("No direct route bound for RNS token ..."), because
nothing correlated the outgoing PROOF back to the peer that sent the
packet it was proving.

The fix: _deliver_reassembled_packet records
truncated_packet_hash -> sender's MeshCore key in
_pending_proof_targets (a short-TTL dict); _resolve_outgoing_route
consults it as a fallback, exact-match, when a PROOF's token misses
_rns_to_mc_map.
"""
import hashlib
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import load_interface_module

mci = load_interface_module()
Interface = mci.MeshCore_Dynamic_Interface

DST_LEN = 16


def reference_truncated_hash(raw: bytes) -> bytes:
    """Independent reimplementation of RNS.Packet.truncated_packet_hash,
    from the same spec _compute_truncated_packet_hash follows -- used to
    check the interface's own computation against a second source, not
    just against itself."""
    header_type = (raw[0] & 0x40) >> 6
    hashable = bytes([raw[0] & 0b00001111])
    hashable += raw[2 + DST_LEN:] if header_type == 1 else raw[2:]
    return hashlib.sha256(hashable).digest()[:DST_LEN]


def make_packet(ptype: int, dest_type: int = 0, header_type: int = 0,
                 dest_hash: bytes = b"\xaa" * DST_LEN, payload: bytes = b"hello") -> bytes:
    flags = (ptype & 0x03) | ((dest_type & 0x03) << 2) | (header_type << 6)
    hop_count = 0x05
    if header_type == 1:
        transport_id = b"\xbb" * DST_LEN
        return bytes([flags, hop_count]) + transport_id + dest_hash + payload
    return bytes([flags, hop_count]) + dest_hash + payload


def make_stub(**overrides):
    stub = types.SimpleNamespace(
        name="t",
        _RNS_DST_LEN=DST_LEN,
        _RNS_PTYPE_PROOF=Interface._RNS_PTYPE_PROOF,
        _PENDING_PROOF_TARGETS_MAX_KEYS=Interface._PENDING_PROOF_TARGETS_MAX_KEYS,
        _pending_proof_targets={},
        _pending_proof_targets_lock=__import__("threading").Lock(),
    )
    for k, v in overrides.items():
        setattr(stub, k, v)
    stub._compute_truncated_packet_hash = types.MethodType(
        Interface._compute_truncated_packet_hash, stub
    )
    stub._cleanup_expired_proof_targets = types.MethodType(
        Interface._cleanup_expired_proof_targets, stub
    )
    return stub


class ComputeTruncatedPacketHashTests(unittest.TestCase):

    def test_header1_matches_reference(self):
        stub = make_stub()
        raw = make_packet(ptype=0, header_type=0)
        self.assertEqual(stub._compute_truncated_packet_hash(raw), reference_truncated_hash(raw))

    def test_header2_matches_reference_and_skips_transport_id(self):
        stub = make_stub()
        raw = make_packet(ptype=0, header_type=1)
        self.assertEqual(stub._compute_truncated_packet_hash(raw), reference_truncated_hash(raw))

    def test_hop_count_excluded_from_hash(self):
        # Two packets differing only in hop count (byte 1) must hash the
        # same -- a repeater incrementing hop count in transit must not
        # change the proof-correlation hash.
        stub = make_stub()
        raw1 = make_packet(ptype=0)
        raw2 = bytearray(raw1)
        raw2[1] = 0x09
        self.assertEqual(
            stub._compute_truncated_packet_hash(raw1),
            stub._compute_truncated_packet_hash(bytes(raw2)),
        )

    def test_returns_none_for_too_short_packet(self):
        stub = make_stub()
        self.assertIsNone(stub._compute_truncated_packet_hash(b"\x00"))

    def test_returns_none_for_truncated_header2(self):
        stub = make_stub()
        # header_type=1 but not enough bytes for the transport ID
        self.assertIsNone(stub._compute_truncated_packet_hash(bytes([0x40, 0x00, 0x01, 0x02])))


class DeliverReassembledPacketRecordsProofTargetTests(unittest.TestCase):

    def _make_delivery_stub(self, mc_key="7bd024b5d082747e"):
        stub = types.SimpleNamespace(
            name="t",
            _RNS_DST_LEN=DST_LEN,
            _RNS_PTYPE_DATA=Interface._RNS_PTYPE_DATA,
            _RNS_DTYPE_PLAIN=Interface._RNS_DTYPE_PLAIN,
            _PTYPE_NAMES=Interface._PTYPE_NAMES,
            _PENDING_PROOF_TARGETS_MAX_KEYS=Interface._PENDING_PROOF_TARGETS_MAX_KEYS,
            _PENDING_PROOF_TARGETS_TTL_S=Interface._PENDING_PROOF_TARGETS_TTL_S,
            r_stat_snr=None, r_stat_rssi=None,
            _peer_table={"a": mc_key} if mc_key else {},
            _peer_lock=__import__("threading").Lock(),
            _pending_proof_targets={},
            _pending_proof_targets_lock=__import__("threading").Lock(),
            _path_response_pending={},
            _path_response_pending_lock=__import__("threading").Lock(),
            _path_response_bypass_s=15.0,
            processIncoming=lambda data: None,
        )
        stub._compute_truncated_packet_hash = types.MethodType(
            Interface._compute_truncated_packet_hash, stub
        )
        stub._deliver_reassembled_packet = types.MethodType(
            Interface._deliver_reassembled_packet, stub
        )
        return stub

    def test_records_target_for_known_sender(self):
        stub = self._make_delivery_stub(mc_key="7bd024b5d082747e")
        raw = make_packet(ptype=0)
        stub._deliver_reassembled_packet(raw, sender="a", rx_mode="DIRECT")

        expected_hash = reference_truncated_hash(raw)
        self.assertIn(expected_hash, stub._pending_proof_targets)
        mc_key, expiry = stub._pending_proof_targets[expected_hash]
        self.assertEqual(mc_key, "7bd024b5d082747e")
        self.assertGreater(expiry, time.monotonic())

    def test_does_not_record_for_unknown_sender(self):
        stub = self._make_delivery_stub(mc_key=None)
        raw = make_packet(ptype=0)
        stub._deliver_reassembled_packet(raw, sender="stranger", rx_mode="CHANNEL")
        self.assertEqual(stub._pending_proof_targets, {})


class ResolveOutgoingRouteProofFallbackTests(unittest.TestCase):

    def _make_route_stub(self, pending_proof_targets=None, rns_to_mc_map=None):
        class FakeMC:
            def get_contact_by_key_prefix(self, key):
                return {"out_path_len": 1, "out_path": "19"}

        stub = types.SimpleNamespace(
            name="t",
            _RNS_DST_LEN=DST_LEN,
            _RNS_PTYPE_PROOF=Interface._RNS_PTYPE_PROOF,
            _has_direct_api=True,
            _mc=FakeMC(),
            _peer_lock=__import__("threading").Lock(),
            _rns_to_mc_map=rns_to_mc_map or {},
            _pending_proof_targets=pending_proof_targets or {},
            _pending_proof_targets_lock=__import__("threading").Lock(),
            _path_req_timestamps={},
            _path_discovery_cooldown_for=lambda key: 60.0,
            _loop=None,
        )
        stub._extract_rns_token = types.MethodType(Interface._extract_rns_token, stub)
        stub._resolve_outgoing_route = types.MethodType(Interface._resolve_outgoing_route, stub)
        return stub

    def test_proof_routes_direct_via_pending_target(self):
        proof_dest = b"\xcc" * DST_LEN  # stands in for the original packet's hash
        raw = make_packet(ptype=Interface._RNS_PTYPE_PROOF, dest_hash=proof_dest)
        stub = self._make_route_stub(
            pending_proof_targets={proof_dest: ("7bd024b5d082747e", time.monotonic() + 60)}
        )
        route = stub._resolve_outgoing_route(raw, broadcast=False)
        self.assertEqual(route, [("direct", "7bd024b5d082747e")])

    def test_proof_falls_back_to_channel_when_target_expired(self):
        proof_dest = b"\xcc" * DST_LEN
        raw = make_packet(ptype=Interface._RNS_PTYPE_PROOF, dest_hash=proof_dest)
        stub = self._make_route_stub(
            pending_proof_targets={proof_dest: ("7bd024b5d082747e", time.monotonic() - 1)}
        )
        route = stub._resolve_outgoing_route(raw, broadcast=False)
        self.assertEqual(route, [("channel", None)])

    def test_proof_falls_back_to_channel_when_no_target_recorded(self):
        proof_dest = b"\xcc" * DST_LEN
        raw = make_packet(ptype=Interface._RNS_PTYPE_PROOF, dest_hash=proof_dest)
        stub = self._make_route_stub(pending_proof_targets={})
        route = stub._resolve_outgoing_route(raw, broadcast=False)
        self.assertEqual(route, [("channel", None)])

    def test_rns_to_mc_map_takes_priority_over_pending_proof_target(self):
        proof_dest = b"\xcc" * DST_LEN
        raw = make_packet(ptype=Interface._RNS_PTYPE_PROOF, dest_hash=proof_dest)
        stub = self._make_route_stub(
            rns_to_mc_map={proof_dest: "primary_key"},
            pending_proof_targets={proof_dest: ("fallback_key", time.monotonic() + 60)},
        )
        route = stub._resolve_outgoing_route(raw, broadcast=False)
        self.assertEqual(route, [("direct", "primary_key")])

    def test_non_proof_packet_never_consults_pending_proof_targets(self):
        # Same "destination" bytes as a pending proof target, but this is
        # a DATA packet -- must not accidentally match.
        dest = b"\xcc" * DST_LEN
        raw = make_packet(ptype=Interface._RNS_PTYPE_DATA, dest_hash=dest)
        stub = self._make_route_stub(
            pending_proof_targets={dest: ("should_not_be_used", time.monotonic() + 60)}
        )
        route = stub._resolve_outgoing_route(raw, broadcast=False)
        self.assertEqual(route, [("channel", None)])


class CleanupExpiredProofTargetsTests(unittest.TestCase):

    def test_removes_only_expired_entries(self):
        stub = make_stub()
        now = time.monotonic()
        stub._pending_proof_targets = {
            b"\x01" * DST_LEN: ("key1", now - 5),   # expired
            b"\x02" * DST_LEN: ("key2", now + 60),  # still valid
        }
        stub._cleanup_expired_proof_targets(now)
        self.assertEqual(list(stub._pending_proof_targets.keys()), [b"\x02" * DST_LEN])


if __name__ == "__main__":
    unittest.main()

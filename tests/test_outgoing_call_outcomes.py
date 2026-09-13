"""
Tests for processOutgoing()'s per-call outcome accounting
(Interface/MeshCore_Dynamic_Interface.py).

Background: field testing turned up a "missing send" symptom -- RNS core
appeared to decide to send/rebroadcast a packet with no matching entry
anywhere in this interface's own logs, and it could not be root-caused
live with the logging that existed at the time. This adds a permanent,
cheap counter (stats.outgoing_call_outcome_counts, printed in the [STATS]
line by _stats_summary_loop) that tallies every processOutgoing() call
into exactly one bucket: dropped_offline, dropped_rate_limited, queued, or
exception, against a handed_to_interface count incremented before any of
those checks can run. The gap between handed_to_interface and the sum of
the other buckets ("unaccounted") is the actionable signal: nonzero means
a call fell through this accounting itself; handed_to_interface staying
flat while RNS core is known to be emitting packets instead points to a
gap upstream of this interface.
"""
import os
import sys
import threading
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import load_interface_module

mci = load_interface_module()
Interface = mci.MeshCore_Dynamic_Interface


def make_packet(ptype: int = 0, dest_type: int = 0, size: int = 20) -> bytes:
    flags = (ptype & 0x03) | ((dest_type & 0x03) << 2)
    hop_count = 0x00
    return bytes([flags, hop_count]) + (b"\xaa" * 16) + (b"x" * size)


def make_stub(**overrides):
    stub = types.SimpleNamespace(
        name="t",
        online=True,
        stats=mci._SessionStats(),
        _pkt_id_lock=threading.Lock(),
        _pkt_id=0,
        txb=0,
        _PTYPE_NAMES=Interface._PTYPE_NAMES,
        _RNS_PTYPE_LINK_REQ=Interface._RNS_PTYPE_LINK_REQ,
        _RNS_PTYPE_PROOF=Interface._RNS_PTYPE_PROOF,
        _PRIORITY_HANDSHAKE=Interface._PRIORITY_HANDSHAKE,
        _PRIORITY_NORMAL=Interface._PRIORITY_NORMAL,
        _rate_limit_announce=lambda data, ptype: (False, False),
        _rate_limit_path_request=lambda data, ptype, dest_type: False,
        _auto_payload_size=lambda: 64,
        _register_pkt_send=lambda pkt_id, n: None,
        _is_broadcast_packet=lambda data: False,
        _resolve_outgoing_route=lambda data, broadcast: [("DIRECT", "somepeer")],
        _enqueue_fragments=lambda *a, **k: None,
        _schedule_extra_retransmits=lambda *a, **k: None,
    )
    for k, v in overrides.items():
        setattr(stub, k, v)
    stub.processOutgoing = types.MethodType(Interface.processOutgoing, stub)
    return stub


class OutcomeAccountingTests(unittest.TestCase):

    def test_normal_send_is_queued(self):
        stub = make_stub()
        stub.processOutgoing(make_packet())
        counts = stub.stats.snapshot()["outgoing_call_outcome_counts"]
        self.assertEqual(counts["handed_to_interface"], 1)
        self.assertEqual(counts["queued"], 1)
        self.assertNotIn("dropped_offline", counts)
        self.assertNotIn("dropped_rate_limited", counts)
        self.assertNotIn("exception", counts)

    def test_offline_drop_is_counted_and_short_circuits(self):
        stub = make_stub(online=False)
        called = []
        stub._resolve_outgoing_route = lambda data, broadcast: called.append(1)
        stub.processOutgoing(make_packet())
        counts = stub.stats.snapshot()["outgoing_call_outcome_counts"]
        self.assertEqual(counts["handed_to_interface"], 1)
        self.assertEqual(counts["dropped_offline"], 1)
        self.assertNotIn("queued", counts)
        self.assertEqual(called, [])

    def test_announce_rate_limit_drop_is_counted(self):
        stub = make_stub(_rate_limit_announce=lambda data, ptype: (True, False))
        stub.processOutgoing(make_packet())
        counts = stub.stats.snapshot()["outgoing_call_outcome_counts"]
        self.assertEqual(counts["handed_to_interface"], 1)
        self.assertEqual(counts["dropped_rate_limited"], 1)
        self.assertNotIn("queued", counts)

    def test_path_request_rate_limit_drop_is_counted(self):
        stub = make_stub(_rate_limit_path_request=lambda data, ptype, dest_type: True)
        stub.processOutgoing(make_packet())
        counts = stub.stats.snapshot()["outgoing_call_outcome_counts"]
        self.assertEqual(counts["handed_to_interface"], 1)
        self.assertEqual(counts["dropped_rate_limited"], 1)
        self.assertNotIn("queued", counts)

    def test_exception_is_counted_and_reraised(self):
        stub = make_stub(
            _resolve_outgoing_route=lambda data, broadcast: (_ for _ in ()).throw(
                RuntimeError("boom")
            )
        )
        with self.assertRaises(RuntimeError):
            stub.processOutgoing(make_packet())
        counts = stub.stats.snapshot()["outgoing_call_outcome_counts"]
        self.assertEqual(counts["handed_to_interface"], 1)
        self.assertEqual(counts["exception"], 1)
        self.assertNotIn("queued", counts)

    def test_multiple_calls_accumulate_per_bucket(self):
        stub = make_stub()
        stub.processOutgoing(make_packet())
        stub.processOutgoing(make_packet())
        stub.online = False
        stub.processOutgoing(make_packet())
        counts = stub.stats.snapshot()["outgoing_call_outcome_counts"]
        self.assertEqual(counts["handed_to_interface"], 3)
        self.assertEqual(counts["queued"], 2)
        self.assertEqual(counts["dropped_offline"], 1)


class SessionStatsOutcomeMethodTests(unittest.TestCase):
    """Directly exercises _SessionStats.record_outgoing_call_outcome() /
    snapshot(), independent of processOutgoing()."""

    def test_unrecorded_outcome_is_absent_not_zero(self):
        stats = mci._SessionStats()
        counts = stats.snapshot()["outgoing_call_outcome_counts"]
        self.assertEqual(counts, {})

    def test_counts_are_independent_per_outcome(self):
        stats = mci._SessionStats()
        stats.record_outgoing_call_outcome("handed_to_interface")
        stats.record_outgoing_call_outcome("handed_to_interface")
        stats.record_outgoing_call_outcome("queued")
        counts = stats.snapshot()["outgoing_call_outcome_counts"]
        self.assertEqual(counts["handed_to_interface"], 2)
        self.assertEqual(counts["queued"], 1)


if __name__ == "__main__":
    unittest.main()

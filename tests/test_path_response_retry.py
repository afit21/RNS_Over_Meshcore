"""
Tests for the path-response ANNOUNCE retry budget added to
Interface/MeshCore_Dynamic_Interface.py.

Background: an ANNOUNCE sent in response to an inbound path request (see
_path_response_pending) is a one-shot, unacknowledged CHANNEL broadcast --
unlike a DIRECT send, there's no ACK and no automatic retry
(_send_direct_with_retry) if it's lost. Field testing over a multi-hop
repeater chain found exactly this: a lost path-response ANNOUNCE just
silently expires the requester's path-request timeout. Previously this
case shared announce_retransmit_extra (0 by default, deliberately, so a
spontaneous self-announce nobody's waiting on doesn't get retried) with
every other ANNOUNCE. It now has its own dedicated
path_response_retransmit_extra budget (default 1), which only ever
applies when _rate_limit_announce identifies the send as demand-driven.

Two things are exercised: _rate_limit_announce now returns
(suppress, is_path_response) instead of a bare bool, and
_schedule_extra_retransmits picks path_response_retransmit_extra instead
of announce_retransmit_extra exactly when that flag is set.
"""
import asyncio
import os
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import load_interface_module

mci = load_interface_module()
Interface = mci.MeshCore_Dynamic_Interface

DST_LEN = Interface._RNS_DST_LEN


def make_announce_packet(dest_hash: bytes = b"\xaa" * DST_LEN) -> bytes:
    flags = Interface._RNS_PTYPE_ANNOUNCE  # dest_type bits left 0
    return bytes([flags, 0x00]) + dest_hash + b"payload"


def make_rate_limit_stub(**overrides):
    stub = types.SimpleNamespace(
        name="t",
        _announce_rate_s=60.0,
        _RNS_DST_LEN=DST_LEN,
        _RNS_PTYPE_ANNOUNCE=Interface._RNS_PTYPE_ANNOUNCE,
        _path_response_pending={},
        _path_response_pending_lock=threading.Lock(),
        _announce_sent_times={},
        _announce_sent_lock=threading.Lock(),
    )
    for k, v in overrides.items():
        setattr(stub, k, v)
    stub._rate_limit_announce = types.MethodType(Interface._rate_limit_announce, stub)
    return stub


class RateLimitAnnounceReturnValueTests(unittest.TestCase):

    def test_non_announce_ptype_passes_through_untouched(self):
        stub = make_rate_limit_stub()
        suppress, is_response = stub._rate_limit_announce(make_announce_packet(), ptype=0x00)
        self.assertFalse(suppress)
        self.assertFalse(is_response)

    def test_first_announce_for_destination_is_allowed_not_a_response(self):
        stub = make_rate_limit_stub()
        suppress, is_response = stub._rate_limit_announce(
            make_announce_packet(), Interface._RNS_PTYPE_ANNOUNCE
        )
        self.assertFalse(suppress)
        self.assertFalse(is_response)

    def test_rate_limited_announce_is_suppressed_and_not_a_response(self):
        dest = b"\xaa" * DST_LEN
        stub = make_rate_limit_stub(_announce_sent_times={dest: time.monotonic()})
        suppress, is_response = stub._rate_limit_announce(
            make_announce_packet(dest), Interface._RNS_PTYPE_ANNOUNCE
        )
        self.assertTrue(suppress)
        self.assertFalse(is_response)

    def test_pending_path_response_bypasses_rate_limit_and_is_flagged(self):
        dest = b"\xaa" * DST_LEN
        stub = make_rate_limit_stub(
            _announce_sent_times={dest: time.monotonic()},  # would otherwise rate-limit
            _path_response_pending={dest: time.monotonic() + 30.0},
        )
        suppress, is_response = stub._rate_limit_announce(
            make_announce_packet(dest), Interface._RNS_PTYPE_ANNOUNCE
        )
        self.assertFalse(suppress)
        self.assertTrue(is_response)

    def test_expired_pending_path_response_is_not_flagged(self):
        dest = b"\xaa" * DST_LEN
        stub = make_rate_limit_stub(
            _path_response_pending={dest: time.monotonic() - 1.0},
        )
        suppress, is_response = stub._rate_limit_announce(
            make_announce_packet(dest), Interface._RNS_PTYPE_ANNOUNCE
        )
        self.assertFalse(is_response)

    def test_pending_entry_is_consumed_once(self):
        dest = b"\xaa" * DST_LEN
        stub = make_rate_limit_stub(
            _path_response_pending={dest: time.monotonic() + 30.0},
        )
        stub._rate_limit_announce(make_announce_packet(dest), Interface._RNS_PTYPE_ANNOUNCE)
        self.assertNotIn(dest, stub._path_response_pending)


def make_retransmit_stub(**overrides):
    calls = []
    stub = types.SimpleNamespace(
        name="t",
        _RNS_PTYPE_ANNOUNCE=Interface._RNS_PTYPE_ANNOUNCE,
        _RNS_PTYPE_DATA=Interface._RNS_PTYPE_DATA,
        _RNS_DTYPE_PLAIN=Interface._RNS_DTYPE_PLAIN,
        announce_retransmit_extra=0,
        path_req_retransmit_extra=0,
        ordinary_data_retransmit_extra=0,
        path_response_retransmit_extra=1,
        _loop=object(),
        _delayed_retransmits=lambda *a, **k: calls.append(a),
    )
    for k, v in overrides.items():
        setattr(stub, k, v)
    stub._schedule_extra_retransmits = types.MethodType(
        Interface._schedule_extra_retransmits, stub
    )
    return stub, calls


class ScheduleExtraRetransmitsTests(unittest.TestCase):
    """asyncio.run_coroutine_threadsafe is stubbed out for these -- only
    which retransmit_extra count reaches _delayed_retransmits is under
    test, not the actual scheduling/jitter/resend loop (covered
    separately by the pre-existing retransmit tests)."""

    def setUp(self):
        self._real_rct = asyncio.run_coroutine_threadsafe
        asyncio.run_coroutine_threadsafe = lambda coro, loop: coro

    def tearDown(self):
        asyncio.run_coroutine_threadsafe = self._real_rct

    def test_path_response_announce_uses_its_own_budget(self):
        stub, calls = make_retransmit_stub(
            announce_retransmit_extra=0, path_response_retransmit_extra=3
        )
        stub._schedule_extra_retransmits(
            handler=types.SimpleNamespace(fragments=["f1"]),
            route=[("channel", None)],
            ptype=Interface._RNS_PTYPE_ANNOUNCE,
            dest_type=0,
            broadcast=True,
            priority=0,
            is_path_response_announce=True,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], 3)  # (fragments, route, count, broadcast, priority)

    def test_ordinary_announce_uses_announce_budget_not_path_response(self):
        stub, calls = make_retransmit_stub(
            announce_retransmit_extra=2, path_response_retransmit_extra=5
        )
        stub._schedule_extra_retransmits(
            handler=types.SimpleNamespace(fragments=["f1"]),
            route=[("channel", None)],
            ptype=Interface._RNS_PTYPE_ANNOUNCE,
            dest_type=0,
            broadcast=True,
            priority=0,
            is_path_response_announce=False,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], 2)

    def test_default_flag_is_false_for_backward_compatible_callers(self):
        stub, calls = make_retransmit_stub(
            announce_retransmit_extra=4, path_response_retransmit_extra=9
        )
        stub._schedule_extra_retransmits(
            handler=types.SimpleNamespace(fragments=["f1"]),
            route=[("channel", None)],
            ptype=Interface._RNS_PTYPE_ANNOUNCE,
            dest_type=0,
            broadcast=True,
            priority=0,
        )
        self.assertEqual(calls[0][2], 4)

    def test_zero_path_response_budget_schedules_nothing(self):
        stub, calls = make_retransmit_stub(path_response_retransmit_extra=0)
        stub._schedule_extra_retransmits(
            handler=types.SimpleNamespace(fragments=["f1"]),
            route=[("channel", None)],
            ptype=Interface._RNS_PTYPE_ANNOUNCE,
            dest_type=0,
            broadcast=True,
            priority=0,
            is_path_response_announce=True,
        )
        self.assertEqual(calls, [])

    def test_path_response_flag_ignored_for_non_announce_ptype(self):
        stub, calls = make_retransmit_stub(
            path_req_retransmit_extra=2, path_response_retransmit_extra=9
        )
        stub._schedule_extra_retransmits(
            handler=types.SimpleNamespace(fragments=["f1"]),
            route=[("channel", None)],
            ptype=Interface._RNS_PTYPE_DATA,
            dest_type=Interface._RNS_DTYPE_PLAIN,
            broadcast=True,
            priority=0,
            is_path_response_announce=True,
        )
        self.assertEqual(calls[0][2], 2)


if __name__ == "__main__":
    unittest.main()

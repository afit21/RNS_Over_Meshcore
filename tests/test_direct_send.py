"""
Regression tests for _send_direct_with_retry / _maybe_reset_stale_path /
_check_event on the DIRECT send path (Interface/MeshCore_Dynamic_Interface.py).

Covers the bugs fixed in this area:
  - an ACK arriving after its own attempt's wait expired (during a retry)
    used to be discarded instead of counting as delivery
  - attempts truncated by our own ceiling used to count toward
    _maybe_reset_stale_path, tearing down working multi-hop routes
  - a non-MSG_SENT/non-ERROR reply must still be rejected via
    _check_event's expected_type check
"""
import asyncio
import os
import sys
import threading
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import FakeEvent, FakeEventType, FakeMC, FakeStats, load_interface_module

mci = load_interface_module()
Interface = mci.MeshCore_Dynamic_Interface


class FakeDirectCommands:
    """send_msg returns a fresh expected_ack per call; ACKs can be
    scheduled to arrive at a given delay after a given attempt, or before
    send_msg even returns (simulating a fast 0-hop round trip)."""

    def __init__(self, stub, sent_type=0, suggested_ms=1000, ack_delays=None,
                 ack_before_return=False, reply_type=None):
        self.stub = stub
        self.sent_type = sent_type
        self.suggested_ms = suggested_ms
        self.ack_delays = ack_delays or {}
        self.ack_before_return = ack_before_return
        self.reply_type = reply_type  # override MSG_SENT with something else
        self.calls = 0
        self.reset_calls = 0

    async def send_msg(self, target, text):
        self.calls += 1
        code = f"c0de{self.calls:04d}"
        if self.calls in self.ack_delays:
            delay = self.ack_delays[self.calls]
            asyncio.get_running_loop().call_later(
                delay,
                lambda: asyncio.ensure_future(
                    self.stub._on_msg_ack(FakeEvent(FakeEventType.ACK, {"code": code}, {"code": code}))
                ),
            )
        if self.ack_before_return:
            await self.stub._on_msg_ack(FakeEvent(FakeEventType.ACK, {"code": code}, {"code": code}))
        return FakeEvent(
            self.reply_type or FakeEventType.MSG_SENT,
            {"type": self.sent_type, "expected_ack": bytes.fromhex(code),
             "suggested_timeout": self.suggested_ms},
        )

    async def reset_path(self, contact):
        self.reset_calls += 1


def make_stub(**overrides):
    stub = types.SimpleNamespace(
        name="t",
        _EventType=FakeEventType,
        stats=FakeStats(),
        _pending_acks={},
        _recent_acks={},
        _path_req_lock=threading.Lock(),
        _direct_path_failures={},
        direct_send_attempts=3,
        direct_ack_timeout_s=0.05,
        direct_ack_timeout_max_s=0.3,
        direct_ack_timeout_routed_max_s=5.0,
        direct_path_reset_threshold=2,
        direct_path_reset_rssi_floor=-105.0,
        direct_path_reset_patience_multiplier=3.0,
        # No peer is pinned by default -- _maybe_reset_stale_path calls
        # self._is_forced_peer(target), which reads this.
        force_direct_path_peer=None,
        _debug=lambda msg: None,
    )
    for k, v in overrides.items():
        setattr(stub, k, v)
    stub._RECENT_ACKS_MAX = Interface._RECENT_ACKS_MAX
    stub._check_event = types.MethodType(Interface._check_event, stub)
    stub._on_msg_ack = types.MethodType(Interface._on_msg_ack, stub)
    stub._is_forced_peer = types.MethodType(Interface._is_forced_peer, stub)
    stub._maybe_reset_stale_path = types.MethodType(Interface._maybe_reset_stale_path, stub)
    stub._send_direct_with_retry = types.MethodType(Interface._send_direct_with_retry, stub)
    return stub


class DirectSendTests(unittest.IsolatedAsyncioTestCase):

    async def _run(self, sent_type, suggested_ms, ack_delays, expect_ok,
                    expect_calls, expect_resets, ack_before_return=False, reply_type=None):
        stub = make_stub()
        cmds = FakeDirectCommands(stub, sent_type, suggested_ms, ack_delays,
                                   ack_before_return, reply_type)
        stub._mc = FakeMC(cmds)
        try:
            await stub._send_direct_with_retry("aabbccddeeff00", "RNS:xyz")
            ok = True
        except Exception:
            ok = False
        self.assertEqual(ok, expect_ok)
        self.assertEqual(cmds.calls, expect_calls)
        self.assertEqual(cmds.reset_calls, expect_resets)
        self.assertEqual(stub._pending_acks, {})  # no leaked entries either way
        return stub

    async def test_routed_ack_inside_firmware_estimate_succeeds_first_try(self):
        await self._run(sent_type=0, suggested_ms=1000, ack_delays={1: 0.8},
                         expect_ok=True, expect_calls=1, expect_resets=0)

    async def test_flood_capped_send_never_resets_path_on_truncated_waits(self):
        # Firmware suggests 30s (flood/no-path) but our flood ceiling caps
        # the wait at 0.3s; no ACK ever arrives. All 3 attempts are cut
        # short by our OWN ceiling, not a genuine full-length failure, so
        # none of them may count toward resetting the cached path.
        await self._run(sent_type=1, suggested_ms=30000, ack_delays={},
                         expect_ok=False, expect_calls=3, expect_resets=0)

    async def test_late_ack_from_earlier_attempt_counts_as_delivery(self):
        # Attempt 1's ACK lands at 0.9s -- after its own ~0.6s wait already
        # expired -- while attempt 2 is in flight. It must still resolve
        # the fragment as delivered instead of being discarded.
        await self._run(sent_type=0, suggested_ms=500, ack_delays={1: 0.9},
                         expect_ok=True, expect_calls=2, expect_resets=0)

    async def test_two_full_wait_failures_reset_the_path(self):
        # Routed, full-length waits every time, no ACK ever -- these DO
        # count, and direct_path_reset_threshold=2 fires the reset before
        # the 3rd (last) attempt.
        await self._run(sent_type=0, suggested_ms=100, ack_delays={},
                         expect_ok=False, expect_calls=3, expect_resets=1)

    async def test_ack_dispatched_before_code_registration_still_matches(self):
        # Simulates a 0-hop link where MSG_SENT and its ACK are dispatched
        # back to back, faster than this coroutine can register the code.
        await self._run(sent_type=0, suggested_ms=1000, ack_delays={},
                         expect_ok=True, expect_calls=1, expect_resets=0,
                         ack_before_return=True)

    async def test_wrong_reply_type_is_rejected_by_check_event(self):
        # _check_event's expected_type=MSG_SENT guard: some other reply
        # type that isn't ERROR either must still be treated as a failure,
        # not silently accepted because it wasn't EventType.ERROR.
        await self._run(sent_type=0, suggested_ms=1000, ack_delays={},
                         expect_ok=False, expect_calls=3, expect_resets=0,
                         reply_type="SOME_OTHER_EVENT")

    async def test_no_response_is_rejected(self):
        stub = make_stub()

        class NoResponseCommands:
            calls = 0
            reset_calls = 0

            async def send_msg(self, target, text):
                NoResponseCommands.calls += 1
                return None

            async def reset_path(self, contact):
                NoResponseCommands.reset_calls += 1

        stub._mc = FakeMC(NoResponseCommands())
        with self.assertRaises(RuntimeError):
            await stub._send_direct_with_retry("aabbccddeeff00", "RNS:xyz")


if __name__ == "__main__":
    unittest.main()

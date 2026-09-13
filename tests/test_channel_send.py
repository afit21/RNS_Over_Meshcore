"""
Regression tests for _send_fragment's CHANNEL branch and _check_event
(Interface/MeshCore_Dynamic_Interface.py).

Covers the bug fixed in changelog.md: send_chan_msg's result was never
checked, so a firmware-rejected send (bad channel_idx, the device's own
send queue full, or no reply within the library's timeout -- all of
which come back as EventType.ERROR or None, never a raised exception)
was treated exactly like a successful broadcast: stats incremented, the
fragment marked permanently done, and the failure handler that exists
for this case never reached. These tests assert send_chan_msg's result
is actually inspected and a rejection raises, is never counted as sent,
and IS logged via _handle_send_failure's CHANNEL branch.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import FakeEvent, FakeEventType, FakeStats, load_interface_module

mci = load_interface_module()
Interface = mci.MeshCore_Dynamic_Interface


class FakeChannelCommands:
    def __init__(self, reply):
        self.reply = reply  # an Event, None, or an exception instance to raise
        self.calls = 0

    async def send_chan_msg(self, channel_idx, text):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def make_stub(commands):
    stub = types.SimpleNamespace(
        name="t",
        _EventType=FakeEventType,
        stats=FakeStats(),
        channel_idx=35,
        _mc=types.SimpleNamespace(commands=commands),
    )
    stub._check_event = types.MethodType(Interface._check_event, stub)
    stub._send_fragment = types.MethodType(Interface._send_fragment, stub)
    # _record_sent_channel_fragment does header-decode bookkeeping unrelated
    # to what these tests check; stub it out rather than dragging in the
    # decode/lock machinery it depends on.
    stub._record_sent_channel_fragment = lambda frag_str: None
    stub._mark_pkt_fragment_done = lambda pkt_id: None
    return stub


class ChannelSendTests(unittest.IsolatedAsyncioTestCase):

    async def test_ok_reply_is_recorded_as_sent(self):
        commands = FakeChannelCommands(FakeEvent(FakeEventType.OK, {}))
        stub = make_stub(commands)
        await stub._send_fragment("channel", None, "RNS:xyz", pkt_id=1)
        self.assertEqual(commands.calls, 1)
        self.assertEqual(stub.stats.tx, 1)
        self.assertEqual(stub.stats.flood_tx, 1)

    async def test_error_reply_raises_and_is_not_counted_as_sent(self):
        commands = FakeChannelCommands(
            FakeEvent(FakeEventType.ERROR, {"reason": "queue full"})
        )
        stub = make_stub(commands)
        with self.assertRaises(RuntimeError):
            await stub._send_fragment("channel", None, "RNS:xyz", pkt_id=1)
        self.assertEqual(commands.calls, 1)
        # The exact bug: these must NOT have been incremented for a
        # rejected send.
        self.assertEqual(stub.stats.tx, 0)
        self.assertEqual(stub.stats.flood_tx, 0)

    async def test_no_response_raises_and_is_not_counted_as_sent(self):
        commands = FakeChannelCommands(None)
        stub = make_stub(commands)
        with self.assertRaises(RuntimeError):
            await stub._send_fragment("channel", None, "RNS:xyz", pkt_id=1)
        self.assertEqual(stub.stats.tx, 0)
        self.assertEqual(stub.stats.flood_tx, 0)

    async def test_rejected_channel_send_reaches_the_failure_log_path(self):
        # End-to-end through _handle_send_failure, exactly as the real
        # outgoing worker calls it: a CHANNEL rejection must be logged
        # (see changelog.md -- it used to be silently swallowed) and must
        # still mark the fragment done since there's no CHANNEL fallback.
        commands = FakeChannelCommands(
            FakeEvent(FakeEventType.ERROR, {"reason": "queue full"})
        )
        stub = make_stub(commands)
        logged = []
        stub._handle_send_failure = types.MethodType(Interface._handle_send_failure, stub)
        stub._mark_pkt_fragment_done = lambda pkt_id: logged.append(("done", pkt_id))
        import RNS
        real_log = RNS.log
        RNS.log = lambda msg, level=None: logged.append(("log", msg))
        try:
            try:
                await stub._send_fragment("channel", None, "RNS:xyz", pkt_id=42)
                self.fail("expected RuntimeError")
            except RuntimeError as exc:
                stub._handle_send_failure(
                    "channel", None, "RNS:xyz", priority=1,
                    queued_at=0.0, pkt_id=42, broadcast=True, exc=exc,
                )
        finally:
            RNS.log = real_log
        self.assertIn(("done", 42), logged)
        self.assertTrue(
            any(kind == "log" and "CHANNEL send failed" in msg for kind, msg in logged),
            f"expected a logged CHANNEL failure, got: {logged}",
        )


if __name__ == "__main__":
    unittest.main()

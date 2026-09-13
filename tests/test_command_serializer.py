"""
Regression tests for _install_command_serializer
(Interface/MeshCore_Dynamic_Interface.py).

Covers the bug fixed in changelog.md: the meshcore library's
CommandHandler.send() has no locking and matches a reply by event TYPE
alone, so two commands in flight at once could be handed each other's
replies (a DIRECT send adopting a path-discovery request's tag as its
own expected_ack, and vice versa). The fix wraps commands.send() in a
single lock so only one command's full subscribe/write/wait round trip
is ever in flight at a time.
"""
import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import load_interface_module

mci = load_interface_module()
Interface = mci.MeshCore_Dynamic_Interface


class ConcurrencyProbeCommands:
    """A send() that records how many calls are simultaneously inside its
    critical section, so overlap is directly observable rather than
    inferred from timing."""

    def __init__(self, delay=0.02):
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0

    async def send(self, tag):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.calls += 1
        await asyncio.sleep(self.delay)
        self.in_flight -= 1
        return tag


class CommandSerializerTests(unittest.IsolatedAsyncioTestCase):

    def _make_stub(self, commands):
        stub = types.SimpleNamespace(_mc=types.SimpleNamespace(commands=commands))
        stub._install_command_serializer = types.MethodType(
            Interface._install_command_serializer, stub
        )
        return stub

    async def test_concurrent_sends_never_overlap(self):
        commands = ConcurrencyProbeCommands()
        stub = self._make_stub(commands)
        installed = stub._install_command_serializer()
        self.assertTrue(installed)

        results = await asyncio.gather(*[commands.send(i) for i in range(6)])
        self.assertEqual(results, list(range(6)))
        self.assertEqual(commands.calls, 6)
        self.assertEqual(
            commands.max_in_flight, 1,
            "two commands' send() bodies were in flight at once -- the "
            "exact race that let one command's reply be handed to a "
            "different in-flight command",
        )

    async def test_install_is_idempotent(self):
        commands = ConcurrencyProbeCommands()
        stub = self._make_stub(commands)
        self.assertTrue(stub._install_command_serializer())
        wrapped_once = commands.send
        self.assertTrue(stub._install_command_serializer())
        self.assertIs(
            commands.send, wrapped_once,
            "installing a second time must not wrap an already-wrapped "
            "send() again (that would silently double the lock nesting)",
        )

    async def test_missing_commands_object_fails_closed(self):
        stub = types.SimpleNamespace(_mc=types.SimpleNamespace())  # no .commands
        stub._install_command_serializer = types.MethodType(
            Interface._install_command_serializer, stub
        )
        self.assertFalse(
            stub._install_command_serializer(),
            "a malformed/future-library _mc.commands shape must be "
            "reported as a failed install, not silently accepted -- the "
            "caller in _async_setup refuses to start the interface "
            "unprotected when this returns False",
        )

    async def test_no_mc_fails_closed(self):
        stub = types.SimpleNamespace(_mc=None)
        stub._install_command_serializer = types.MethodType(
            Interface._install_command_serializer, stub
        )
        self.assertFalse(stub._install_command_serializer())


if __name__ == "__main__":
    unittest.main()

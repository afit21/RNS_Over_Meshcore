"""
Tests for the two testing-only overrides added to
Interface/MeshCore_Dynamic_Interface.py (_configure_testing_overrides):

  - force_direct_path_peer / force_direct_path: pins one peer's MeshCore
    out_path to a manually-specified repeater route, and disables
    discover_path()/_maybe_reset_stale_path() for that peer for the rest
    of the session.
  - channel_relay_only: silently drops any received CHANNEL message that
    shows no evidence of having been relayed by a repeater (path_len 0 or
    255).
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import FakeEvent, FakeEventType, load_interface_module

mci = load_interface_module()
Interface = mci.MeshCore_Dynamic_Interface


def make_config_stub():
    stub = types.SimpleNamespace(name="t")
    stub._configure_testing_overrides = types.MethodType(
        Interface._configure_testing_overrides, stub
    )
    return stub


class ConfigParsingTests(unittest.TestCase):
    """_configure_testing_overrides: config validation is silent-failure-
    safe -- a bad value disables the feature with a logged reason rather
    than raising or half-applying."""

    def _configure(self, cfg_dict):
        stub = make_config_stub()
        import RNS
        real_log = RNS.log
        RNS.log = lambda msg, level=None: None
        try:
            stub._configure_testing_overrides(cfg_dict)
        finally:
            RNS.log = real_log
        return stub

    def test_defaults_are_disabled(self):
        stub = self._configure({})
        self.assertIsNone(stub.force_direct_path_peer)
        self.assertIsNone(stub.force_direct_path_hex)
        self.assertFalse(stub.channel_relay_only)

    def test_valid_path_default_mode(self):
        stub = self._configure({
            "force_direct_path_peer": "AABBCC",
            "force_direct_path": "7A3F01",
        })
        self.assertEqual(stub.force_direct_path_peer, "aabbcc")
        self.assertEqual(stub.force_direct_path_hex, "7a3f01")
        self.assertEqual(stub.force_direct_path_hash_mode, 0)

    def test_valid_path_explicit_mode(self):
        stub = self._configure({
            "force_direct_path_peer": "aabbcc",
            "force_direct_path": "7a3f01be:1",  # 4 bytes / 2-byte hops = 2 hops
        })
        self.assertEqual(stub.force_direct_path_hex, "7a3f01be")
        self.assertEqual(stub.force_direct_path_hash_mode, 1)

    def test_only_peer_set_disables_both(self):
        stub = self._configure({"force_direct_path_peer": "aabbcc"})
        self.assertIsNone(stub.force_direct_path_peer)
        self.assertIsNone(stub.force_direct_path_hex)

    def test_only_path_set_disables_both(self):
        stub = self._configure({"force_direct_path": "aabbcc"})
        self.assertIsNone(stub.force_direct_path_peer)
        self.assertIsNone(stub.force_direct_path_hex)

    def test_invalid_hex_disables(self):
        stub = self._configure({
            "force_direct_path_peer": "aabbcc",
            "force_direct_path": "zzzz",
        })
        self.assertIsNone(stub.force_direct_path_peer)

    def test_odd_length_hex_disables(self):
        stub = self._configure({
            "force_direct_path_peer": "aabbcc",
            "force_direct_path": "abc",
        })
        self.assertIsNone(stub.force_direct_path_peer)

    def test_unsupported_hash_mode_disables(self):
        # hash_mode 3 (4-byte hops) is rejected by the firmware itself
        # (Packet::isValidPathLen) -- see commands/base.py's
        # encode_reply_path in the meshcore library for the same rule.
        stub = self._configure({
            "force_direct_path_peer": "aabbcc",
            "force_direct_path": "aabbccdd:3",
        })
        self.assertIsNone(stub.force_direct_path_peer)

    def test_too_many_hops_disables(self):
        # 64 single-byte hops exceeds the 63-hop cap (plen's low 6 bits).
        stub = self._configure({
            "force_direct_path_peer": "aabbcc",
            "force_direct_path": "aa" * 64,
        })
        self.assertIsNone(stub.force_direct_path_peer)

    def test_channel_relay_only_parses_bool(self):
        self.assertTrue(self._configure({"channel_relay_only": "yes"}).channel_relay_only)
        self.assertFalse(self._configure({"channel_relay_only": "no"}).channel_relay_only)
        self.assertFalse(self._configure({}).channel_relay_only)


class IsForcedPeerTests(unittest.TestCase):

    def _stub(self, forced_peer):
        stub = types.SimpleNamespace(force_direct_path_peer=forced_peer)
        stub._is_forced_peer = types.MethodType(Interface._is_forced_peer, stub)
        return stub

    def test_no_override_configured(self):
        stub = self._stub(None)
        self.assertFalse(stub._is_forced_peer("aabbccddeeff"))

    def test_exact_and_prefix_matches(self):
        stub = self._stub("aabbcc")
        self.assertTrue(stub._is_forced_peer("aabbcc"))
        self.assertTrue(stub._is_forced_peer("aabbccddeeff00"))  # full key, forced is a prefix
        self.assertTrue(stub._is_forced_peer("aa"))              # short prefix of the forced peer
        self.assertFalse(stub._is_forced_peer("112233"))

    def test_case_insensitive(self):
        stub = self._stub("aabbcc")
        self.assertTrue(stub._is_forced_peer("AABBCCDDEEFF"))

    def test_empty_key(self):
        stub = self._stub("aabbcc")
        self.assertFalse(stub._is_forced_peer(""))


class DiscoverPathSkipsForcedPeerTests(unittest.IsolatedAsyncioTestCase):

    async def test_forced_peer_never_calls_send_path_discovery_sync(self):
        calls = []

        class Commands:
            async def send_path_discovery_sync(self, contact, timeout):
                calls.append(contact)
                return None

        class MC:
            def __init__(self):
                self.commands = Commands()

            async def ensure_contacts(self):
                pass

        stub = types.SimpleNamespace(
            name="t",
            force_direct_path_peer="aabbcc",
            _mc=MC(),
            _debug=lambda msg: None,
        )
        stub._is_forced_peer = types.MethodType(Interface._is_forced_peer, stub)
        stub.discover_path = types.MethodType(Interface.discover_path, stub)

        result = await stub.discover_path({"public_key": "aabbccddeeff00"})
        self.assertIsNone(result)
        self.assertEqual(calls, [], "forced peer must never trigger path discovery")


class MaybeResetStalePathSkipsForcedPeerTests(unittest.IsolatedAsyncioTestCase):

    async def test_forced_peer_never_gets_reset(self):
        class Commands:
            def __init__(self):
                self.reset_calls = 0

            async def reset_path(self, contact):
                self.reset_calls += 1

        class MC:
            def __init__(self, commands):
                self.commands = commands

            def get_contact_by_key_prefix(self, key):
                return {"out_path_len": 5}

        commands = Commands()
        stub = types.SimpleNamespace(
            name="t",
            force_direct_path_peer="aabbcc",
            direct_path_reset_threshold=1,  # would fire immediately if not skipped
            direct_path_reset_rssi_floor=-105.0,
            direct_path_reset_patience_multiplier=3.0,
            stats=types.SimpleNamespace(mesh_utilization={}),
            _path_req_lock=__import__("threading").Lock(),
            _direct_path_failures={},
            _mc=MC(commands),
            _debug=lambda msg: None,
        )
        stub._is_forced_peer = types.MethodType(Interface._is_forced_peer, stub)
        stub._maybe_reset_stale_path = types.MethodType(Interface._maybe_reset_stale_path, stub)

        await stub._maybe_reset_stale_path("aabbccddeeff00", consecutive_failures=99)
        self.assertEqual(commands.reset_calls, 0)


class SetupAppliesForcedPathFromKnownContactsTests(unittest.IsolatedAsyncioTestCase):
    """Regression test for a real bug found in field testing: a peer
    restored from _load_peer_cache (or otherwise already known at
    startup) never passes through _bind_meshcore_contact, so the reactive
    hook there never gets a chance to apply force_direct_path unless a
    LIVE contact-table event happens to fire for that peer later.
    _setup_apply_forced_direct_path must apply it directly from the
    already-fetched contact list instead of waiting for one."""

    async def test_applies_immediately_when_contact_already_known(self):
        applied = []

        class MC:
            def get_contact_by_key_prefix(self, key):
                return {"public_key": "aabbccddeeff00"}

        stub = types.SimpleNamespace(
            force_direct_path_peer="aabbcc",
            _forced_path_applied=False,
            _mc=MC(),
            _debug=lambda msg: None,
        )

        async def fake_apply(contact):
            applied.append(contact)

        stub._apply_forced_direct_path = fake_apply
        stub._setup_apply_forced_direct_path = types.MethodType(
            Interface._setup_apply_forced_direct_path, stub
        )

        await stub._setup_apply_forced_direct_path()
        self.assertEqual(len(applied), 1)
        self.assertTrue(stub._forced_path_applied)

    async def test_noop_when_no_override_configured(self):
        stub = types.SimpleNamespace(
            force_direct_path_peer=None,
            _forced_path_applied=False,
            _mc=types.SimpleNamespace(),
            _debug=lambda msg: None,
        )
        stub._setup_apply_forced_direct_path = types.MethodType(
            Interface._setup_apply_forced_direct_path, stub
        )
        await stub._setup_apply_forced_direct_path()  # must not raise
        self.assertFalse(stub._forced_path_applied)

    async def test_leaves_it_for_the_reactive_hook_when_contact_not_yet_known(self):
        class MC:
            def get_contact_by_key_prefix(self, key):
                return None

        stub = types.SimpleNamespace(
            force_direct_path_peer="aabbcc",
            _forced_path_applied=False,
            _mc=MC(),
            _debug=lambda msg: None,
        )
        stub._setup_apply_forced_direct_path = types.MethodType(
            Interface._setup_apply_forced_direct_path, stub
        )
        await stub._setup_apply_forced_direct_path()
        self.assertFalse(
            stub._forced_path_applied,
            "must leave _forced_path_applied False so _bind_meshcore_contact's "
            "reactive hook still gets a chance once the contact appears",
        )

    async def test_skips_if_already_applied(self):
        class MC:
            def get_contact_by_key_prefix(self, key):
                raise AssertionError("must not even look up the contact again")

        stub = types.SimpleNamespace(
            force_direct_path_peer="aabbcc",
            _forced_path_applied=True,
            _mc=MC(),
            _debug=lambda msg: None,
        )
        stub._setup_apply_forced_direct_path = types.MethodType(
            Interface._setup_apply_forced_direct_path, stub
        )
        await stub._setup_apply_forced_direct_path()  # must not raise


def make_channel_msg_stub(channel_relay_only):
    calls = {"bind": [], "tunnel": []}

    async def fake_handle_bind(text, bind_idx, req_idx=-1):
        calls["bind"].append(text)

    async def fake_process_tunnel_text(text, sender="", rx_mode="UNKNOWN", snr=None, rssi=None):
        calls["tunnel"].append(text)

    stub = types.SimpleNamespace(
        channel_relay_only=channel_relay_only,
        MSG_PREFIX=Interface.MSG_PREFIX,
        BIND_PREFIX=Interface.BIND_PREFIX,
        BIND_REQ_PREFIX=Interface.BIND_REQ_PREFIX,
        _handle_bind=fake_handle_bind,
        _process_tunnel_text=fake_process_tunnel_text,
        _debug=lambda msg: None,
    )
    stub._on_channel_msg = types.MethodType(Interface._on_channel_msg, stub)
    return stub, calls


class ChannelRelayOnlyTests(unittest.IsolatedAsyncioTestCase):

    async def test_unrelayed_message_is_dropped_when_enabled(self):
        stub, calls = make_channel_msg_stub(channel_relay_only=True)
        for path_len in (0, 255, None):
            payload = {"text": "RNS:xyz", "path_len": path_len}
            await stub._on_channel_msg(FakeEvent(FakeEventType.OK, payload))
        self.assertEqual(calls["tunnel"], [])
        self.assertEqual(calls["bind"], [])

    async def test_bind_traffic_is_also_dropped_when_unrelayed(self):
        stub, calls = make_channel_msg_stub(channel_relay_only=True)
        payload = {"text": "RNSBIND_REQ:aabbcc:R", "path_len": 0}
        await stub._on_channel_msg(FakeEvent(FakeEventType.OK, payload))
        self.assertEqual(calls["bind"], [])

    async def test_relayed_message_passes_through_when_enabled(self):
        stub, calls = make_channel_msg_stub(channel_relay_only=True)
        payload = {"text": "RNS:xyz", "path_len": 2}
        await stub._on_channel_msg(FakeEvent(FakeEventType.OK, payload))
        self.assertEqual(calls["tunnel"], ["RNS:xyz"])

    async def test_unrelayed_message_passes_through_when_disabled(self):
        stub, calls = make_channel_msg_stub(channel_relay_only=False)
        payload = {"text": "RNS:xyz", "path_len": 0}
        await stub._on_channel_msg(FakeEvent(FakeEventType.OK, payload))
        self.assertEqual(calls["tunnel"], ["RNS:xyz"])


if __name__ == "__main__":
    unittest.main()

"""
Shared fake MeshCore transport/command harness for the test suite.

No real radio hardware is touched anywhere in this suite. These fakes
simulate just enough of the `meshcore` library's Event/EventType/
CommandHandler shape -- as reverse-engineered from the installed library
during the investigations recorded in changelog.md -- for the interface
code under test to run against, including the failure modes (ERROR
replies, no-response timeouts, reply cross-talk) that caused the real
bugs this suite guards against.

load_interface_module() loads Interface/MeshCore_Dynamic_Interface.py by
path rather than via a package import, since this repo has no installed
package/__init__.py structure -- it's the same approach used to verify
the fixes interactively during development.
"""
import importlib.util
import os

INTERFACE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "Interface", "MeshCore_Dynamic_Interface.py"
)


def load_interface_module():
    spec = importlib.util.spec_from_file_location(
        "mci_under_test", INTERFACE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeEvent:
    def __init__(self, type, payload, attributes=None):
        self.type = type
        self.payload = payload
        self.attributes = attributes or {}


class FakeEventType:
    """Enough of meshcore.EventType for the code paths under test."""
    MSG_SENT = "MSG_SENT"
    ERROR = "ERROR"
    ACK = "ACK"
    OK = "OK"


class FakeStats:
    """Records just what the send paths under test touch."""
    mesh_utilization = {}

    def __init__(self):
        self.tx = 0
        self.flood_tx = 0
        self.latencies = []
        self.direct_results = []

    def record_tx(self):
        self.tx += 1

    def record_flood_tx(self):
        self.flood_tx += 1

    def record_meshcore_latency(self, seconds, peer_key=None):
        self.latencies.append(seconds)

    def record_direct_result(self, success, peer_key=None):
        self.direct_results.append(success)


class FakeMC:
    """Stand-in for the meshcore.MeshCore instance (self._mc)."""

    def __init__(self, commands, out_path_len=2):
        self.commands = commands
        self._out_path_len = out_path_len

    def get_contact_by_key_prefix(self, key):
        return {"out_path_len": self._out_path_len}

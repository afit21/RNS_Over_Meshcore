"""
Static check enforcing design invariant #3 in the module docstring of
Interface/MeshCore_Dynamic_Interface.py: every `self.X = ... cfg.get("X",
...)` set in a _configure_* method must be read (as `self.X`, in Load
context) somewhere else in the file.

This is the automated version of the bug in changelog.md: `self.
firmware_text_limit` was parsed from config, documented in README.md as
a user-adjustable safety knob, and then never read anywhere --
_auto_payload_size used a hardcoded placeholder instead. A user setting
firmware_text_limit in their config got no error and no effect. This
test would have caught that the day it was introduced.

Uses Python's own `ast` module rather than a regex scan, so it can't be
fooled by the attribute name appearing inside a comment or a log-message
string (both of which legitimately mention several of these config names
for humans reading the logs).
"""
import ast
import os
import unittest

INTERFACE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "Interface", "MeshCore_Dynamic_Interface.py"
)


def _is_cfg_get_call(node) -> bool:
    """True if `node` is (anywhere in its subtree) a call to cfg.get(...)."""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "get"
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == "cfg"
        ):
            return True
    return False


def find_dead_cfg_attrs(source: str) -> dict:
    """Returns {attr_name: first_assignment_lineno} for every self.X
    assigned from a cfg.get(...)-derived expression that is never read
    (as self.X, ast.Load) anywhere else in the same source."""
    tree = ast.parse(source)

    assigned = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            continue
        if _is_cfg_get_call(node.value):
            assigned.setdefault(target.attr, node.lineno)

    read_attrs = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
    }

    return {
        attr: lineno for attr, lineno in assigned.items() if attr not in read_attrs
    }


class ConfigUsageTests(unittest.TestCase):
    # This file's own AST is all the scan can see -- it's blind to a
    # value read by code outside this file. RNS.Interfaces.Interface (the
    # base class) reads several of its subclasses' attributes directly by
    # name via ordinary Python attribute access, e.g.
    # RNS/Interfaces/Interface.py's HW_MTU sizing reads `self.bitrate`.
    # That's correct, working, and by design -- not a bug -- so these
    # are excluded on that basis alone, never as a stand-in for "unread
    # and that's fine."
    EXTERNALLY_CONSUMED_BY_RNS_CORE = {
        "bitrate": (
            "RNS.Transport / RNS.Interfaces.Interface reads "
            "interface.bitrate directly (HW_MTU sizing, announce pacing, "
            "Link-establishment timeout estimates) -- see the comment "
            "above its assignment in _configure_routing_and_debug."
        ),
    }

    # Deliberate, temporary exceptions to invariant #3: attributes that
    # really are unread anywhere right now. Each entry must name why and
    # what removes the exception -- see design invariant #3 in the module
    # docstring. Unlike EXTERNALLY_CONSUMED_BY_RNS_CORE above, an entry
    # here IS the exact bug class this test exists to catch, just
    # tolerated on a deadline.
    ALLOWED_DEAD = {
        "firmware_text_limit": (
            "_auto_payload_size is deliberately hardcoded to a fixed "
            "value while a specific payload size is being tested (see "
            "its TODO comment). Restore "
            "`firmware_limit = self.firmware_text_limit` there and "
            "remove this entry once that test is done."
        ),
    }

    @classmethod
    def setUpClass(cls):
        with open(INTERFACE_PATH, encoding="utf-8") as f:
            cls.source = f.read()
        cls.dead = find_dead_cfg_attrs(cls.source)

    def test_every_configured_attribute_is_read_somewhere(self):
        excused = {**self.EXTERNALLY_CONSUMED_BY_RNS_CORE, **self.ALLOWED_DEAD}
        unexpected = {
            attr: lineno for attr, lineno in self.dead.items() if attr not in excused
        }
        self.assertEqual(
            unexpected, {},
            f"Config attribute(s) parsed via cfg.get() but never read "
            f"anywhere else in the file (design invariant #3): "
            f"{unexpected}. A user setting this in their config gets no "
            f"error and no effect. If it's genuinely read only by RNS "
            f"core via the Interface base-class contract, add it to "
            f"EXTERNALLY_CONSUMED_BY_RNS_CORE; if it's a real, temporary "
            f"gap, add it to ALLOWED_DEAD with a reason and a removal "
            f"condition -- don't silence this failure any other way.",
        )

    def test_allowlists_have_no_stale_entries(self):
        # If either excuse stops applying -- the attribute becomes
        # genuinely read from within this file -- its entry is stale and
        # must be removed, or this test could never catch that SAME
        # attribute going dead again later for a different, unnoticed
        # reason.
        for attr in {**self.EXTERNALLY_CONSUMED_BY_RNS_CORE, **self.ALLOWED_DEAD}:
            self.assertIn(
                attr, self.dead,
                f"'{attr}' is listed as unread-within-this-file but is "
                f"no longer dead by that measure -- remove the "
                f"(now-stale) entry.",
            )


if __name__ == "__main__":
    unittest.main()

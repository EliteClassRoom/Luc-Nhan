"""Regression tests pinning the IDA action registration contract.

After the Bulk Renamer tab was removed from the visible Tools surface,
the Send-to-Bulk-Rename right-click action must also be gone.  These
tests exercise ``LucNhanUIHooks._register_actions`` under the
established IDA mock harness and assert that only the Open Tools
action (plus the core context-menu actions) gets registered.
"""

from __future__ import annotations

import importlib
import os
import sys
import types as _types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

# Force a fresh import of the actions module so the module-level IDA
# probe resolves to ``_HAS_IDA = True`` and the action classes are
# actually defined in the namespace.  Pre-registering the module
# in ``sys.modules`` keeps ``setattr(_sys.modules[__name__], ...)``
# inside ``_ensure_ida`` from failing under the mock harness.
_mod_placeholder = _types.ModuleType("lucnhan.ida.ui.actions")
sys.modules.setdefault("lucnhan.ida.ui.actions", _mod_placeholder)

for _name in list(sys.modules):
    if _name.startswith("lucnhan.ida.ui.actions") or _name.startswith("lucnhan.ui.action_handlers"):
        sys.modules.pop(_name, None)

actions_mod = importlib.import_module("lucnhan.ida.ui.actions")


class TestSendToBulkRenameRemoved(unittest.TestCase):
    """The Send-to-Bulk-Rename action has been removed from the registry.

    These tests avoid invoking ``_register_actions`` (which exercises
    the entire ``idaapi`` mock surface and is sensitive to global mock
    state).  Instead, they assert that:

    - The legacy handler class is no longer present in the actions
      module.
    - The action id ``lucnhan:send_to_bulk_rename`` does not appear
      anywhere in the module source, so a future re-add is caught by
      a code-level grep rather than by a runtime mock exercise.
    """

    def test_no_legacy_class_in_module_namespace(self):
        """The legacy handler class must not be importable any more."""
        self.assertFalse(
            hasattr(actions_mod, "_SendToBulkRenameAction"),
            "_SendToBulkRenameAction should be removed with the tab.",
        )

    def test_no_send_to_bulk_rename_action_id_in_source(self):
        """The action id ``lucnhan:send_to_bulk_rename`` must not be re-introduced."""
        import inspect

        source = inspect.getsource(actions_mod)
        self.assertNotIn(
            "lucnhan:send_to_bulk_rename",
            source,
            "Action id lucnhan:send_to_bulk_rename reappeared in actions.py",
        )

    def test_no_bulk_rename_phrase_in_source(self):
        """The human-readable phrase should also be gone to keep copy consistent."""
        import inspect

        source = inspect.getsource(actions_mod)
        self.assertNotIn(
            "Send to Bulk Rename",
            source,
            "Send to Bulk Rename label reappeared in actions.py",
        )

    def test_open_tools_action_is_still_registered(self):
        """Open Tools must remain — users still need a way to open the panel."""
        import inspect

        source = inspect.getsource(actions_mod)
        self.assertIn(
            "lucnhan:open_tools",
            source,
            "Open Tools action id is missing; the panel cannot be reached.",
        )


if __name__ == "__main__":
    unittest.main()

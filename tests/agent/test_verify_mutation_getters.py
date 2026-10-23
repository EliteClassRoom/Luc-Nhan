"""Contract test: post-state-verification getters return RAW value.

``AgentLoop._verify_mutation`` (lucnhan/agent/loop.py) compares a getter tool's
return value *exactly* against the expected mutation result. For example it
asserts ``str(get_function_name(...)) == str(new_name)``. That comparison only
holds if every getter used for verification returns the raw underlying value
(function name / address name / comment string) with NO prefix, header,
truncation, or formatting.

CLAUDE.md enshrines this: "Getter tool used by ``capture_pre_state`` returns raw
data, not a formatted string." This test pins that contract so a future
refactor of ``lucnhan/ida/tools/annotations.py`` / ``functions.py`` that adds
wrapping to a getter breaks loudly instead of silently making ``_verify_mutation``
report spurious failures.

Each test patches the tool module's *bound* reference (``ida_name`` / ``idc`` /
``ida_funcs`` as held by ``lucnhan.ida.tools.{annotations,functions}``) rather
than ``sys.modules``, so it is immune to IDA-mock re-installation that can
replace the per-module objects during full-suite collection.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.ida.tools import annotations as _ann
from lucnhan.ida.tools import functions as _funcs
from lucnhan.ida.tools.registry import (
    create_default_registry,
    register_advanced_tools,
)


def _build_registry():
    """Build the real registry the agent uses (boot + advanced tools).

    ``dispatch_wrapper=None`` so execution is direct and synchronous; the
    getters here need no IDA main-thread marshalling in the test process.
    """
    registry = create_default_registry(dispatch_wrapper=None)
    registry.set_capabilities({"hexrays": True, "ida_ui": True})
    register_advanced_tools(registry)
    return registry


class TestVerifyMutationGettersRaw(unittest.TestCase):
    """Each getter used by ``_verify_mutation`` must return the raw value."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = _build_registry()

    def test_get_comment_returns_raw_comment(self) -> None:
        raw = "a raw comment; with <markers> & special chars"
        with mock.patch.object(_ann.idc, "get_cmt", return_value=raw):
            result = self.registry.execute("get_comment", {"address": "0x1000", "repeatable": False})
        self.assertEqual(result, raw)

    def test_get_function_comment_returns_raw(self) -> None:
        raw = "func-level comment"
        fakes_func = mock.MagicMock()
        with (
            mock.patch.object(_ann.ida_funcs, "get_func", return_value=fakes_func),
            mock.patch.object(_ann.ida_funcs, "get_func_cmt", return_value=raw),
        ):
            result = self.registry.execute("get_function_comment", {"address": "0x1000", "repeatable": False})
        self.assertEqual(result, raw)

    def test_get_function_name_returns_raw(self) -> None:
        fakes_func = mock.MagicMock()
        with (
            mock.patch.object(_funcs.ida_funcs, "get_func", return_value=fakes_func),
            mock.patch.object(_funcs.ida_name, "get_name", return_value="sub_401000"),
        ):
            result = self.registry.execute("get_function_name", {"address": "0x401000"})
        self.assertEqual(result, "sub_401000")
        self.assertNotIn(":", result)

    def test_get_address_name_returns_raw(self) -> None:
        with mock.patch.object(_ann.ida_name, "get_name", return_value="data_500000"):
            result = self.registry.execute("get_address_name", {"address": "0x500000"})
        self.assertEqual(result, "data_500000")


if __name__ == "__main__":
    unittest.main()

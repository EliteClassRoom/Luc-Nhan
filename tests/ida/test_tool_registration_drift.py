"""Every module that defines a ``@tool`` must be registered somewhere.

A new tool file that nobody adds to ``_BOOT_TOOL_MODULES`` or
``_ADVANCED_MODULE_NAMES`` still imports fine and passes its own tests while
being invisible to the agent — nothing else catches that.
"""

from __future__ import annotations

import os
import unittest

from lucnhan.ida.tools import registry


def _tool_modules() -> set[str]:
    # ``base`` only documents the decorator; it defines no tool of its own.
    skip = {"lucnhan.tools.base"}
    found: set[str] = set()
    for package in ("lucnhan/ida/tools", "lucnhan/tools"):
        for name in os.listdir(package):
            if not name.endswith(".py") or name == "__init__.py":
                continue
            path = os.path.join(package, name)
            with open(path, encoding="utf-8") as f:
                if "@tool(" not in f.read():
                    continue
            found.add(f"{package.replace('/', '.')}.{name[:-3]}")
    return found - skip


class TestToolRegistrationDrift(unittest.TestCase):
    def test_helper_finds_known_tool_modules(self) -> None:
        found = _tool_modules()
        self.assertIn("lucnhan.ida.tools.types_tools", found)
        self.assertIn("lucnhan.tools.web_fetch", found)
        self.assertNotIn("lucnhan.tools.cache", found)

    def test_every_tool_module_is_registered(self) -> None:
        boot = {f"{mod.__name__}" for mod in registry._BOOT_TOOL_MODULES}
        registered = boot | set(registry._ADVANCED_MODULE_NAMES)
        missing = sorted(_tool_modules() - registered)
        self.assertEqual(missing, [], f"tool module(s) never registered: {missing}")

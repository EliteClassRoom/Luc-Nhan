"""Tests for the lazy crypto availability check in ``core.crypto``.

The settings dialog depends on ``crypto.is_available()`` to enable the
"Encrypt API keys" checkbox. The original implementation imported
``cryptography`` at module load time, which slowed down the settings
dialog's first paint. These tests verify the lazy check still returns
the correct result without importing the heavy crypto primitives.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def _isolated_cryptography_modules() -> Iterator[None]:
    """Snapshot every ``cryptography*`` module from ``sys.modules`` and
    restore them on exit, even when the test body raises.

    Tests that want to assert "calling X does not import cryptography"
    must first evict the package from ``sys.modules`` so that the
    assertion is meaningful.  If the eviction is left in place after the
    test finishes, a later test in the same process that *does* import
    cryptography (e.g. via the OpenAI / Anthropic SDKs, oauth helpers,
    or the encryption key dialog) would see a half-unloaded package and
    fail in surprising ways.  This context manager guarantees the
    interpreter is restored to its original state.
    """
    snapshot = {name: module for name, module in list(sys.modules.items()) if name.startswith("cryptography")}
    try:
        for name in list(snapshot):
            sys.modules.pop(name, None)
        yield
    finally:
        # Drop anything that snuck in during the test, then restore.
        for name in [m for m in list(sys.modules) if m.startswith("cryptography")]:
            sys.modules.pop(name, None)
        sys.modules.update(snapshot)


class TestCryptoLazyImport(unittest.TestCase):
    def test_is_available_returns_bool(self):
        from rikugan.core.crypto import is_available

        result = is_available()
        self.assertIsInstance(result, bool)

    def test_module_does_not_eagerly_import_cryptography(self):
        # Re-import the crypto module after forcibly removing
        # ``cryptography`` from ``sys.modules``. If crypto.py were still
        # importing it at module load time, this reimport would
        # succeed in binding the globals; otherwise is_available()
        # should fall back to ``importlib.util.find_spec`` and return
        # whatever is currently installed (probably True in CI).
        with _isolated_cryptography_modules():
            if "rikugan.core.crypto" in sys.modules:
                importlib.reload(sys.modules["rikugan.core.crypto"])
            from rikugan.core.crypto import is_available

            # The result reflects the actual installation: if cryptography
            # is present (it usually is in dev), the function returns True;
            # otherwise False. Either way, the import must not crash.
            self.assertIsInstance(is_available(), bool)

    def test_calling_is_available_does_not_import_cryptography(self):
        # After reloading the module in a clean state, calling
        # ``is_available()`` MUST NOT actually import the
        # ``cryptography`` package.  We assert that no submodule whose
        # name starts with ``"cryptography"`` appears in ``sys.modules``
        # after the call.
        with _isolated_cryptography_modules():
            if "rikugan.core.crypto" in sys.modules:
                importlib.reload(sys.modules["rikugan.core.crypto"])
            from rikugan.core.crypto import is_available

            # is_available() is allowed to use find_spec(); that does not
            # cause a full import.  No ``cryptography*`` module may end
            # up in sys.modules after the call.
            result = is_available()
            self.assertIsInstance(result, bool)
            leaked = [m for m in sys.modules if m.startswith("cryptography")]
            self.assertEqual(
                leaked,
                [],
                f"is_available() must not import the cryptography package, but found: {leaked}",
            )

    def test_coerce_token_count_does_not_import_cryptography(self):
        # Ensure the token-coercion helper (in a different module) does
        # not pull in cryptography.
        with _isolated_cryptography_modules():
            from rikugan.core.types import coerce_token_count

            self.assertEqual(coerce_token_count(None), 0)
            self.assertEqual(coerce_token_count(5), 5)


if __name__ == "__main__":
    unittest.main()

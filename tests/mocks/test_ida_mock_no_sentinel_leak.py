"""Guard against ``ida_mock`` sentinel leaks and the ``ida_domain`` crash.

Two regressions guarded here:

* **P1 (Windows interpreter crash)** — ``lucnhan.ida.tools.scripting``
  ``_get_base_namespace()`` imports every symbol in ``_IDA_MODULE_NAMES``,
  including the *real* ``ida_domain`` PyPI package.  On a host with IDA
  Python installed that package's ``_load_dependencies()`` ``import idapro``
  and constructs a QWidget without a QApplication, tripping a Windows native
  fast-fail (``0xC0000409``).  ``install_ida_mocks`` must shadow
  ``ida_domain`` so the real package is never imported in tests.

* **Sentinel leak hygiene** — several mock attributes still return a bare
  ``MagicMock``; any code that treats such a value as a real ``str``/``int``
  misbehaves.  This guards the high-signal functions so a future change to
  ``tests/mocks/ida_mock.py`` cannot silently regress to sentinel output.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

import lucnhan.core.host as host
from tests.mocks.ida_mock import install_ida_mocks


@pytest.fixture(scope="module", autouse=True)
def _install():
    install_ida_mocks()
    yield
    # Nothing to restore: install_ida_mocks() is idempotent per call.


def test_ida_domain_is_shadowed():
    """``ida_domain`` must be mocked so scripting never imports the real package."""
    mod = sys.modules.get("ida_domain")
    assert mod is not None, "ida_domain should be stubbed by install_ida_mocks"
    assert isinstance(mod, MagicMock), "ida_domain must be a MagicMock, not the real package"


def test_ida_kernwin_is_shadowed():
    """``ida_kernwin`` must be mocked (real module loads IDA/Qt)."""
    mod = sys.modules.get("ida_kernwin")
    assert isinstance(mod, MagicMock)


def test_host_database_path_is_real_str():
    """``core.host.get_database_path`` must yield a real str, never a sentinel."""
    val = host.get_database_path()
    assert isinstance(val, str)
    assert not isinstance(val, MagicMock)


def test_host_database_instance_id_is_real_str():
    """``core.host.get_database_instance_id`` must yield a real str, never a sentinel."""
    val = host.get_database_instance_id()
    assert isinstance(val, str)
    assert not isinstance(val, MagicMock)


def test_resolve_active_cache_key_yields_strings():
    """The string-cache key derivation must not receive a MagicMock idb path."""
    from lucnhan.tools.string_cache import resolve_active_cache_key

    cache_key, idb_path, db_instance_id = resolve_active_cache_key()
    for value in (cache_key, idb_path, db_instance_id):
        assert isinstance(value, str)
        assert not isinstance(value, MagicMock)


def test_idaapi_get_path_is_real_str():
    """``idaapi.get_path`` must return a real path string, not a sentinel."""
    import sys as _sys

    idaapi = _sys.modules["idaapi"]
    assert isinstance(idaapi.get_path(0), str)
    assert not isinstance(idaapi.get_path(0), MagicMock)

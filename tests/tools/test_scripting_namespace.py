"""Tests for ``lucnhan/ida/tools/scripting._get_base_namespace``.

Findings 6 & 7: pre-imported IDA modules must be wrapped in ``SafeModule``
so the global deny-set applies uniformly (raw modules would otherwise expose
``idaapi.os`` / ``idc.sys`` transitively), and every call must return *fresh*
wrappers so a later execution never shares a mutable accessor with an earlier
one.

These tests are self-contained: they inject minimal fake IDA modules into
``sys.modules`` per-test rather than using the repository-wide IDA mock
machinery (``install_ida_mocks``), so this module can safely be collected in
the same pytest process as other real-module test files.
"""

from __future__ import annotations

import sys
import types

import pytest

from lucnhan.ida.tools import scripting
from lucnhan.tools.script_guard import SafeModule

#: Fake IDA modules injected into ``sys.modules`` so
#: ``_get_base_namespace``'s ``importlib.import_module`` succeeds and
#: ``_cached_namespace`` is populated deterministically.
_FAKE_MODULES = {
    "idaapi": ({"idc": "stub"}, []),
    "idc": ({"get_name": "stub"}, []),
}


@pytest.fixture(autouse=True)
def _fake_ida_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject fake IDA modules and reset the module cache for clean state."""
    for name, (_attrs, _subs) in _FAKE_MODULES.items():
        mod = types.ModuleType(name)
        mod.fake_attr = "value"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, name, mod)
    # Reset the lazily-populated cache so each test observes fresh behavior.
    monkeypatch.setattr(scripting, "_cached_namespace", None)
    yield
    # Restore added modules and cache.
    monkeypatch.undo()


def test_namespace_modules_are_safe_module_wrapped() -> None:
    """Finding 6: every pre-imported module in the returned namespace must be
    a ``SafeModule`` proxy (not the raw module), applying the deny-set."""
    ns = scripting._get_base_namespace()
    wrapped_ida = {k: v for k, v in ns.items() if k in _FAKE_MODULES}
    assert wrapped_ida, "expected fake IDA modules to be pre-imported"
    for name in _FAKE_MODULES:
        assert isinstance(wrapped_ida[name], SafeModule), f"{name} not wrapped"


def test_fresh_wrappers_per_call() -> None:
    """Finding 7: two calls must return non-identical SafeModule wrappers, so
    user code never shares (and cannot mutate) a module accessor."""
    ns1 = scripting._get_base_namespace()
    ns2 = scripting._get_base_namespace()
    for name in _FAKE_MODULES:
        assert isinstance(ns1[name], SafeModule)
        assert ns1[name] is not ns2[name], f"{name} wrapper leaked across calls"


def test_wrapped_module_denies_os_attribute() -> None:
    """Finding 6: accessing a deny-set attribute through a wrapped module must
    raise AttributeError, proving the deny-set closes transitively."""
    ns = scripting._get_base_namespace()
    sample = ns["idc"]
    assert isinstance(sample, SafeModule)
    with pytest.raises(AttributeError, match="Blocked"):
        _ = sample.os  # type: ignore[attr-defined]
    with pytest.raises(AttributeError, match="Blocked"):
        _ = sample.sys  # type: ignore[attr-defined]

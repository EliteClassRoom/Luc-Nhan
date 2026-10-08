"""Tests for lucnhan.core.host standalone-testable functions."""

from __future__ import annotations

import lucnhan.core.host as host_mod
from lucnhan.core.host import (
    HOST_IDA,
    HOST_STANDALONE,
    host_display_name,
    host_kind,
    is_ida,
)


def _set_host(kind: str):
    """Force _HOST to a given value for testing."""
    host_mod._HOST = kind


class TestHostKind:
    def test_returns_string(self):
        result = host_kind()
        assert isinstance(result, str)

    def test_valid_value(self):
        assert host_kind() in (HOST_IDA, HOST_STANDALONE)


class TestIsIda:
    def setup_method(self):
        self._orig = host_mod._HOST

    def teardown_method(self):
        host_mod._HOST = self._orig

    def test_true_when_ida(self):
        _set_host(HOST_IDA)
        assert is_ida() is True

    def test_false_when_standalone(self):
        _set_host(HOST_STANDALONE)
        assert is_ida() is False


class TestHostDisplayName:
    def setup_method(self):
        self._orig = host_mod._HOST

    def teardown_method(self):
        host_mod._HOST = self._orig

    def test_ida_display_name(self):
        _set_host(HOST_IDA)
        assert host_display_name() == "IDA Pro"

    def test_standalone_display_name(self):
        _set_host(HOST_STANDALONE)
        assert host_display_name() == "Standalone Python"

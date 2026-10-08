"""Tests for lucnhan.core.crypto — API key encryption."""

from __future__ import annotations

import pytest

from lucnhan.core import crypto
from lucnhan.core.crypto import decrypt_keys, encrypt_keys, is_available


@pytest.mark.skipif(not is_available(), reason="cryptography not installed")
class TestCrypto:
    def test_round_trip(self):
        data = {"provider_api_key": "sk-ant-123", "providers": {"openrouter": "sk-or-456"}}
        enc = encrypt_keys("password", data)
        assert decrypt_keys("password", enc) == data

    def test_wrong_password(self):
        enc = encrypt_keys("correct", {"provider_api_key": "secret"})
        with pytest.raises(ValueError, match="Wrong password"):
            decrypt_keys("wrong", enc)

    def test_empty_keys(self):
        data = {"provider_api_key": "", "providers": {}}
        enc = encrypt_keys("pw", data)
        assert decrypt_keys("pw", enc) == data

    def test_malformed_block(self):
        with pytest.raises(ValueError, match="Malformed"):
            decrypt_keys("pw", {"bad": "data"})

    def test_decrypts_pre_rename_block(self, monkeypatch):
        """Blocks written before the rename stay readable.

        The marker is sealed *inside* the ciphertext, so it cannot be rewritten
        on read — the legacy spelling has to keep verifying or stored API keys
        become unreachable.
        """
        data = {"provider_api_key": "sk-ant-legacy"}
        monkeypatch.setattr(crypto, "_VERIFY_SENTINEL", next(iter(crypto._LEGACY_VERIFY_SENTINELS)))
        enc = encrypt_keys("pw", data)
        monkeypatch.undo()
        assert decrypt_keys("pw", enc) == data

    def test_fresh_salt_per_call(self):
        data = {"provider_api_key": "key"}
        a = encrypt_keys("pw", data)
        b = encrypt_keys("pw", data)
        assert a["salt"] != b["salt"]
        assert a["ciphertext"] != b["ciphertext"]

    def test_tampered_ciphertext(self):
        enc = encrypt_keys("pw", {"provider_api_key": "key"})
        enc["ciphertext"] = enc["ciphertext"][:-4] + "AAAA"
        with pytest.raises(ValueError):
            decrypt_keys("pw", enc)


def test_is_available():
    assert isinstance(is_available(), bool)

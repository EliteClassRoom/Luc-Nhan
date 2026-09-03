"""Tests for RikuganConfig encryption semantics around save()/load().

Pins the password-less save() contract: when ``encrypt_api_keys`` is on, a
``save()`` without a password must never write plaintext API keys to disk nor
flip ``encryption.enabled`` to False — the stored blob is re-emitted verbatim
so a later session can still decrypt (the panel_core.py knowledge-toggle
path). Independent of Qt, IDA, or any keychain.
"""

from __future__ import annotations

import json
from pathlib import Path

import rikugan.core.config as config_module
from rikugan.core.config import RikuganConfig

CONFIG_FILE = "config.json"


def _config(tmp_path: Path) -> RikuganConfig:
    cfg = RikuganConfig()
    cfg._config_dir = str(tmp_path)
    return cfg


def _encrypted_config(tmp_path: Path, password: str = "pw123") -> RikuganConfig:
    """Create a config with one encrypted key on disk and return it."""
    cfg = _config(tmp_path)
    cfg.provider.api_key = "sk-secret"
    cfg.encrypt_api_keys = True
    cfg.save(password=password)
    return cfg


def _decrypt_like_session_start(cfg: RikuganConfig, password: str = "pw123") -> None:
    """Mimic panel_core.py:239 → _prompt_decryption_password()."""
    assert cfg.has_encrypted_keys() is True
    assert cfg.decrypt_stored_keys(password) is True


def test_save_without_password_preserves_encryption(tmp_path: Path) -> None:
    """Password-less save must re-emit the blob, never downgrade to plaintext."""
    cfg = _encrypted_config(tmp_path)
    assert cfg.has_encrypted_keys() is False  # just encrypted — nothing pending
    blob = json.loads((tmp_path / CONFIG_FILE).read_text(encoding="utf-8"))["encryption"]
    assert blob["enabled"] is True

    cfg2 = _config(tmp_path)
    cfg2.load()
    _decrypt_like_session_start(cfg2)
    cfg2.knowledge_show_retrieved_in_chat = True  # the panel_core.py:3655 path
    cfg2.save()  # no password

    raw = (tmp_path / CONFIG_FILE).read_text(encoding="utf-8")
    assert "sk-secret" not in raw, "plaintext downgrade on password-less save"
    on_disk = json.loads(raw)
    assert on_disk["encryption"]["enabled"] is True
    # Blob re-emitted verbatim — salt/nonce/ciphertext untouched.
    assert on_disk["encryption"]["salt"] == blob["salt"]
    assert on_disk["encryption"]["nonce"] == blob["nonce"]
    assert on_disk["encryption"]["ciphertext"] == blob["ciphertext"]

    # A fresh session must still recover the keys from the preserved blob.
    cfg3 = _config(tmp_path)
    cfg3.load()
    assert cfg3.has_encrypted_keys() is True
    assert cfg3.decrypt_stored_keys("pw123") is True
    assert cfg3.provider.api_key == "sk-secret"


def test_save_without_password_keeps_plaintext_keys_in_memory(tmp_path: Path) -> None:
    """The dump is zeroed, not the live config: the session keeps its keys."""
    _encrypted_config(tmp_path)
    cfg2 = _config(tmp_path)
    cfg2.load()
    _decrypt_like_session_start(cfg2)

    cfg2.save()

    assert cfg2.provider.api_key == "sk-secret"
    assert cfg2.providers["anthropic"]["api_key"] == "sk-secret"
    assert cfg2.encrypt_api_keys is True


def test_has_encrypted_keys_false_after_decrypt(tmp_path: Path) -> None:
    """Decryption resolves the pending-prompt state without dropping the blob."""
    _encrypted_config(tmp_path)
    cfg = _config(tmp_path)
    cfg.load()
    assert cfg.has_encrypted_keys() is True

    assert cfg.decrypt_stored_keys("pw123") is True
    assert cfg.has_encrypted_keys() is False


def test_save_without_password_and_without_blob_refuses_plaintext_keys(tmp_path: Path, monkeypatch) -> None:
    """Encrypt flag on, but no blob and no password: keys must not hit disk."""
    warnings: list[str] = []
    monkeypatch.setattr(config_module, "log_warning", lambda msg: warnings.append(msg))

    cfg = _config(tmp_path)
    cfg.provider.api_key = "sk-orphan"  # never saved with a password
    cfg.encrypt_api_keys = True
    cfg.save()

    raw = (tmp_path / CONFIG_FILE).read_text(encoding="utf-8")
    assert "sk-orphan" not in raw, "plaintext keys written without a password"
    on_disk = json.loads(raw)
    assert on_disk["encryption"]["enabled"] is False
    # Coherent on-disk degrade state: the flag must not point at disabled
    # encryption, otherwise the next session reloads a contradictory config.
    assert on_disk["encrypt_api_keys"] is False
    assert warnings, "user must be warned that keys were not persisted"
    # The live session is untouched — only the file degraded.
    assert cfg.provider.api_key == "sk-orphan"


def test_save_without_password_no_keys_disables_cleanly(tmp_path: Path) -> None:
    """Encrypt flag on, no blob, and no keys anywhere: the file must stay coherent.

    Nothing can leak, but the persisted state must not leave
    ``encrypt_api_keys=true`` pointing at ``encryption.enabled=false`` —
    a reload would restore the degenerate in-memory state.
    """
    cfg = _config(tmp_path)
    cfg.encrypt_api_keys = True
    cfg.save()

    on_disk = json.loads((tmp_path / CONFIG_FILE).read_text(encoding="utf-8"))
    assert on_disk["encryption"] == {"enabled": False}
    assert on_disk["encrypt_api_keys"] is False

    reloaded = _config(tmp_path)
    reloaded.load()
    assert reloaded.encrypt_api_keys is False
    assert reloaded.has_encrypted_keys() is False


def test_save_without_password_keeps_plaintext_when_unencrypted(tmp_path: Path) -> None:
    """Baseline guard: encryption off + no password behaves as before."""
    cfg = _config(tmp_path)
    cfg.provider.api_key = "sk-plain"
    cfg.save()

    raw = (tmp_path / CONFIG_FILE).read_text(encoding="utf-8")
    assert "sk-plain" in raw
    assert json.loads(raw)["encryption"] == {"enabled": False}


def test_blob_round_trips_through_load_and_save(tmp_path: Path) -> None:
    """The full encrypt → save → load → password-less save → decrypt loop."""
    _encrypted_config(tmp_path, password="hunter2")

    cfg = _config(tmp_path)
    cfg.load()
    cfg.save()  # password-less, mid-session

    cfg2 = _config(tmp_path)
    cfg2.load()
    assert cfg2.decrypt_stored_keys("hunter2") is True
    assert cfg2.provider.api_key == "sk-secret"

"""Tests for LucNhanConfig encryption semantics around save()/load().

Pins the password-less save() contract: when ``encrypt_api_keys`` is on, a
``save()`` without a password must never write plaintext API keys to disk nor
flip ``encryption.enabled`` to False — the stored blob is re-emitted verbatim
so a later session can still decrypt (the panel_core.py knowledge-toggle
path). Independent of Qt, IDA, or any keychain.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import pytest

import lucnhan.core.config as config_module
from lucnhan.core.config import LucNhanConfig

CONFIG_FILE = "config.json"


def _config(tmp_path: Path) -> LucNhanConfig:
    cfg = LucNhanConfig()
    cfg._config_dir = str(tmp_path)
    return cfg


def _encrypted_config(tmp_path: Path, password: str = "pw123") -> LucNhanConfig:
    """Create a config with one encrypted key on disk and return it."""
    cfg = _config(tmp_path)
    cfg.provider.api_key = "sk-secret"
    cfg.encrypt_api_keys = True
    cfg.save(password=password)
    return cfg


def _decrypt_like_session_start(cfg: LucNhanConfig, password: str = "pw123") -> None:
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


# --------------------------------------------------------------------
# Config hardening: strict boolean guard + numeric coercion
# --------------------------------------------------------------------
# Hand-edited config files must not silently flip security-relevant
# booleans ("yes"/"1"/1 → True is a hardening regression) and must not
# crash validate()/save() when a numeric field arrives as a string.
# These tests pin both behaviors via _apply_loaded_config, which is the
# single source of truth for loading saved JSON.


def test_truthy_string_does_not_enable_oauth_consent() -> None:
    cfg = LucNhanConfig()
    cfg._apply_loaded_config({"oauth_consent_accepted": "yes"})
    assert cfg.oauth_consent_accepted is False


def test_truthy_string_does_not_enable_preserve_context() -> None:
    cfg = LucNhanConfig()
    cfg._apply_loaded_config({"preserve_context": "true"})
    assert cfg.preserve_context is False


def test_truthy_string_does_not_disable_a2a_auto_discover() -> None:
    cfg = LucNhanConfig()
    cfg._apply_loaded_config({"a2a_auto_discover": "0"})
    assert cfg.a2a_auto_discover is True  # default preserved


def test_truthy_string_does_not_enable_encrypt_api_keys() -> None:
    # Security-critical gate: must never silently flip from a truthy string.
    cfg = LucNhanConfig()
    cfg._apply_loaded_config({"encrypt_api_keys": "yes"})
    assert cfg.encrypt_api_keys is False


def test_real_bool_oauth_consent_is_accepted() -> None:
    cfg = LucNhanConfig()
    cfg._apply_loaded_config({"oauth_consent_accepted": True})
    assert cfg.oauth_consent_accepted is True


def test_string_temperature_loads_and_validate_returns_errors_not_raises() -> None:
    # Hand-edited config: temperature arrived as a JSON string.
    # _apply_loaded_config must coerce-or-skip so validate() never sees a
    # non-numeric temperature (which would raise TypeError on "<=").
    cfg = LucNhanConfig()
    cfg._apply_loaded_config(
        {
            "provider": {
                "name": "anthropic",
                "temperature": "0.3",
                "max_tokens": 4096,
                "context_window": 100000,
            }
        }
    )
    # No TypeError — validate() returns a list (possibly with errors).
    errors = cfg.validate()
    assert isinstance(errors, list)


def test_garbage_temperature_is_rejected_not_loaded() -> None:
    cfg = LucNhanConfig()
    cfg._apply_loaded_config({"provider": {"temperature": "not-a-number"}})
    # Bad value skipped; default temperature is preserved.
    from lucnhan.constants import DEFAULT_TEMPERATURE

    assert cfg.provider.temperature == DEFAULT_TEMPERATURE


def test_garbage_max_tokens_is_rejected_not_loaded() -> None:
    cfg = LucNhanConfig()
    cfg._apply_loaded_config({"provider": {"max_tokens": "lots"}})
    from lucnhan.constants import DEFAULT_MAX_TOKENS

    assert cfg.provider.max_tokens == DEFAULT_MAX_TOKENS


def test_garbage_context_window_is_rejected_not_loaded() -> None:
    cfg = LucNhanConfig()
    cfg._apply_loaded_config({"provider": {"context_window": "huge"}})
    from lucnhan.constants import DEFAULT_CONTEXT_WINDOW

    assert cfg.provider.context_window == DEFAULT_CONTEXT_WINDOW


# --------------------------------------------------------------------
# Atomic save boundary — a partial write must never destroy the previous
# config now that every footer model/thinking commit persists.
# --------------------------------------------------------------------


def test_save_writes_through_atomic_replace(tmp_path: Path, monkeypatch) -> None:
    """save() swaps a temp file from the same directory into place."""
    cfg = _config(tmp_path)
    calls: list[tuple[str, str]] = []

    from lucnhan.core import atomic_io

    real_replace = atomic_io.atomic_replace

    def _spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_io, "atomic_replace", _spy)

    cfg.save()

    assert len(calls) == 1
    src, dst = calls[0]
    assert Path(dst) == Path(cfg.config_path)
    # Same directory as the target, otherwise os.replace is cross-device.
    assert Path(src).parent == Path(cfg.config_path).parent
    # The temp file is consumed by the replace.
    assert not Path(src).exists()


def test_save_failure_keeps_previous_file_and_removes_temp(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    cfg.save()
    before = (tmp_path / CONFIG_FILE).read_bytes()

    from lucnhan.core import atomic_io

    def _boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(atomic_io, "atomic_replace", _boom)

    with pytest.raises(OSError):
        cfg.provider.model = "gpt-4o-mini"
        cfg.save()

    assert (tmp_path / CONFIG_FILE).read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == [CONFIG_FILE]


def test_save_failure_on_first_write_leaves_no_file_or_temp(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)

    from lucnhan.core import atomic_io

    def _boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(atomic_io, "atomic_replace", _boom)

    with pytest.raises(OSError):
        cfg.save()

    assert list(tmp_path.iterdir()) == []


class TestLoadHardening:
    """A hand-edited config must not crash the panel or poison the agent loop."""

    def test_string_numbers_are_coerced(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        (tmp_path / CONFIG_FILE).write_text(
            json.dumps({"max_retries": "5", "exploration_turn_limit": "42"}), encoding="utf-8"
        )
        cfg.load()
        assert cfg.max_retries == 5
        assert cfg.exploration_turn_limit == 42

    def test_out_of_range_values_are_clamped(self, tmp_path: Path) -> None:
        """A 0 retry made range() empty and the retry loop raise None."""
        cfg = _config(tmp_path)
        (tmp_path / CONFIG_FILE).write_text(
            json.dumps({"max_retries": 0, "font_size_override": 400, "knowledge_max_context_chars": 10}),
            encoding="utf-8",
        )
        cfg.load()
        assert cfg.max_retries == 1
        assert cfg.font_size_override == 72
        assert cfg.knowledge_max_context_chars == 1_000

    def test_unparsable_value_keeps_the_default(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        (tmp_path / CONFIG_FILE).write_text(json.dumps({"max_retries": "abc"}), encoding="utf-8")
        cfg.load()
        assert cfg.max_retries == LucNhanConfig().max_retries

    def test_boolean_is_not_accepted_as_a_number(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        (tmp_path / CONFIG_FILE).write_text(json.dumps({"max_retries": True}), encoding="utf-8")
        cfg.load()
        assert cfg.max_retries == LucNhanConfig().max_retries

    def test_corrupt_json_falls_back_to_defaults_and_keeps_a_backup(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        path = tmp_path / CONFIG_FILE
        path.write_text("{ broken ,,", encoding="utf-8")
        cfg.load()
        assert cfg.max_retries == LucNhanConfig().max_retries
        assert not path.exists()
        assert (tmp_path / f"{CONFIG_FILE}.bak").read_text(encoding="utf-8") == "{ broken ,,"

    def test_bom_is_tolerated(self, tmp_path: Path) -> None:
        """PowerShell 5.1 / legacy Notepad write a BOM; it must not wipe keys."""
        cfg = _config(tmp_path)
        (tmp_path / CONFIG_FILE).write_bytes(
            b"\xef\xbb\xbf" + json.dumps({"provider": {"api_key": "sk-1"}, "max_retries": 4}).encode()
        )
        cfg.load()
        assert cfg.provider.api_key == "sk-1"
        assert cfg.max_retries == 4
        assert not (tmp_path / f"{CONFIG_FILE}.bak").exists()

    def test_non_object_json_is_quarantined(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        path = tmp_path / CONFIG_FILE
        path.write_text("[1, 2]", encoding="utf-8")
        cfg.load()
        assert cfg.max_retries == LucNhanConfig().max_retries
        assert (tmp_path / f"{CONFIG_FILE}.bak").exists()


class TestApiBaseValidation(unittest.TestCase):
    """A base URL that cannot work must be refused before the SDK sees it."""

    def test_problem_is_none_for_a_usable_base(self):
        from lucnhan.core.config import api_base_problem

        self.assertIsNone(api_base_problem("https://api.example.com/v1"))
        self.assertIsNone(api_base_problem("http://127.0.0.1:8080/v1"))
        self.assertIsNone(api_base_problem(""))

    def test_problem_names_the_reason(self):
        from lucnhan.core.config import api_base_problem

        self.assertIn("http://", api_base_problem("ftp://host/v1") or "")
        self.assertIn("no host", api_base_problem("https:///v1") or "")
        self.assertIn("user:pass", api_base_problem("https://u:p@host/v1") or "")
        self.assertIn("whitespace", api_base_problem("https://a.test/v 1") or "")
        self.assertIn("not a string", api_base_problem(7) or "")

    def test_validate_reports_and_trims(self):
        config = LucNhanConfig()
        config.provider.api_base = "  https://api.example.com/v1  "
        self.assertEqual(config.validate(), [])
        self.assertEqual(config.provider.api_base, "https://api.example.com/v1")

    def test_validate_rejects_a_bad_base(self):
        config = LucNhanConfig()
        config.provider.api_base = "api.example.com"
        self.assertTrue(any("api_base" in err for err in config.validate()))

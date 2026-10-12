"""Regression tests for the model/thinking selection controller path.

``SessionControllerBase.select_model`` is the single commit point for the
footer picker: it validates the connection, model ID and thinking level,
stages the provider/extra changes, persists the config atomically, and
only then publishes the new selection onto the live config object.

Contract pinned here (plan Task 4 / GUI contract G0, G6, G9, G11):

* a busy agent **in any tab** rejects the selection (``RuntimeError``);
  the guard runs at commit time, so an agent that starts while the popup
  is open still wins the race;
* invalid connection / empty model ID / level outside the model's
  registry raise ``ValueError`` — a custom/manual model ID does not need
  to be in a fetched catalog;
* two connections offering the same model ID keep distinct endpoint and
  credential identity;
* unrelated ``extra`` fields, credentials, transcript, draft and MCP
  processes survive a switch;
* a failed write keeps the previous file, the previous selection and
  leaves no temp file behind;
* an encrypted config saved without a password keeps its blob verbatim.

No runtime/network init happens here: the controller is built with
``__new__`` and only the attributes ``select_model`` touches.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.core.config import LucNhanConfig
from lucnhan.core.thinking import get_thinking_levels
from lucnhan.state.session import SessionState
from lucnhan.ui.session_controller_base import SessionControllerBase

CONFIG_FILE = "config.json"


class _FakeLoop:
    def __init__(self, running: bool = False) -> None:
        self.is_running = running


class _FakeRunner:
    def __init__(self, running: bool = True) -> None:
        self.agent_loop = _FakeLoop(running)
        self.cancelled = False

    def cancel(self) -> None:  # pragma: no cover — shutdown safety net
        self.cancelled = True


def _make_controller(cfg: LucNhanConfig) -> SessionControllerBase:
    """Build a controller without the runtime/MCP/SDK import chain."""
    ctrl = SessionControllerBase.__new__(SessionControllerBase)
    ctrl.config = cfg
    ctrl._sessions = {
        "tab_a": SessionState(provider_name=cfg.provider.name, model_name=cfg.provider.model),
    }
    ctrl._active_tab_id = "tab_a"
    ctrl._runners = {}
    return ctrl


@pytest.fixture
def cfg(tmp_path: Path) -> LucNhanConfig:
    config = LucNhanConfig()
    config._config_dir = str(tmp_path)
    config.provider.name = "openai"
    config.provider.model = "gpt-4o"
    config.provider.api_key = "sk-primary"
    config.provider.api_base = "https://primary.example/v1"
    config.provider.extra = {"thinking": {"enabled": False, "reasoning_effort": "none", "preserve": True}}
    # Two connections that offer the SAME model ID but different
    # endpoint/credential identity.
    config.providers = {
        "openai": {
            "model": "gpt-4o",
            "api_key": "sk-primary",
            "api_base": "https://primary.example/v1",
            "temperature": 0.2,
            "max_tokens": 4096,
            "context_window": 128000,
            "extra": {"thinking": {"enabled": False, "reasoning_effort": "none", "preserve": True}},
        },
        "openai_compat": {
            "model": "gpt-4o",
            "api_key": "sk-secondary",
            "api_base": "https://secondary.example/v1",
            "temperature": 0.9,
            "max_tokens": 1024,
            "context_window": 64000,
            "extra": {"custom_flag": "keep-me"},
        },
    }
    config.custom_providers = {"openai_compat": {}}
    return config


@pytest.fixture
def ctrl(cfg: LucNhanConfig) -> SessionControllerBase:
    return _make_controller(cfg)


# ---------------------------------------------------------------------------
# Busy guard — evaluated at commit time, across every tab
# ---------------------------------------------------------------------------


def test_busy_agent_in_other_tab_rejects_selection(ctrl: SessionControllerBase, cfg: LucNhanConfig) -> None:
    tab_b = "tab_b"
    ctrl._sessions[tab_b] = SessionState(provider_name="openai", model_name="gpt-4o")
    ctrl._runners[tab_b] = _FakeRunner(running=True)  # type: ignore[assignment]

    with pytest.raises(RuntimeError):
        ctrl.select_model("openai_compat", "gpt-4o", "none")

    # Selection untouched: neither config nor footer/session metadata.
    assert cfg.provider.name == "openai"
    assert cfg.provider.model == "gpt-4o"
    assert cfg.provider.api_key == "sk-primary"


def test_busy_state_change_before_commit_aborts(
    ctrl: SessionControllerBase, cfg: LucNhanConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that starts while the popup is open still wins the race.

    The guard is evaluated at commit time — after validation/staging and
    before anything is written — so flipping the busy state just before
    the commit rejects the selection and leaves nothing behind.
    """
    tab_b = "tab_b"
    ctrl._sessions[tab_b] = SessionState(provider_name="openai", model_name="gpt-4o")
    runner = _FakeRunner(running=False)
    ctrl._runners[tab_b] = runner  # type: ignore[assignment]

    original = SessionControllerBase.is_agent_running
    guard_reads = {"n": 0}

    def _armed(self) -> bool:
        # Arm the runner on the very first read the commit path makes.
        guard_reads["n"] += 1
        runner.agent_loop.is_running = True
        return bool(original.fget(self))  # type: ignore[attr-defined]

    monkeypatch.setattr(SessionControllerBase, "is_agent_running", property(_armed), raising=True)

    with pytest.raises(RuntimeError):
        ctrl.select_model("openai_compat", "gpt-4o", "none")

    assert guard_reads["n"] == 1
    assert cfg.provider.name == "openai"
    assert cfg.provider.model == "gpt-4o"
    assert cfg.provider.api_key == "sk-primary"
    # Nothing reached disk either.
    assert list(Path(cfg._config_dir).iterdir()) == []


def test_idle_state_allows_selection(ctrl: SessionControllerBase) -> None:
    tab_b = "tab_b"
    ctrl._sessions[tab_b] = SessionState(provider_name="openai", model_name="gpt-4o")
    ctrl._runners[tab_b] = _FakeRunner(running=False)  # type: ignore[assignment]

    ctrl.select_model("openai_compat", "gpt-4o", "none")

    assert ctrl.config.provider.name == "openai_compat"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_unknown_connection_raises_value_error(ctrl: SessionControllerBase) -> None:
    with pytest.raises(ValueError):
        ctrl.select_model("not-a-connection", "gpt-4o", "none")


def test_empty_connection_name_raises_value_error(ctrl: SessionControllerBase) -> None:
    with pytest.raises(ValueError):
        ctrl.select_model("", "gpt-4o", "none")


def test_empty_model_id_raises_value_error(ctrl: SessionControllerBase) -> None:
    with pytest.raises(ValueError):
        ctrl.select_model("openai_compat", "   ", "none")


def test_level_outside_model_registry_raises_value_error(ctrl: SessionControllerBase) -> None:
    # glm-5.2 accepts ("none", "minimal", "low", "medium", "high",
    # "xhigh", "max") — "ultra" belongs to the default list only.
    with pytest.raises(ValueError):
        ctrl.select_model("openai_compat", "glm-5.2", "ultra")


def test_manual_model_id_outside_catalog_is_accepted(ctrl: SessionControllerBase) -> None:
    """Custom/manual IDs are valid even when never fetched from a catalog."""
    ctrl.select_model("openai_compat", "local-custom-model-x9", "high")

    assert ctrl.config.provider.model == "local-custom-model-x9"


def test_invalid_input_leaves_previous_selection(ctrl: SessionControllerBase, cfg: LucNhanConfig) -> None:
    before = (cfg.provider.name, cfg.provider.model, cfg.provider.api_key, dict(cfg.provider.extra))
    with pytest.raises(ValueError):
        ctrl.select_model("nope", "gpt-4o", "none")
    assert (cfg.provider.name, cfg.provider.model, cfg.provider.api_key, cfg.provider.extra) == before


# ---------------------------------------------------------------------------
# Connection identity: same model ID, different endpoint/credentials
# ---------------------------------------------------------------------------


def test_same_model_id_keeps_distinct_connection_identity(ctrl: SessionControllerBase, cfg: LucNhanConfig) -> None:
    ctrl.select_model("openai_compat", "gpt-4o", "none")

    assert cfg.provider.name == "openai_compat"
    assert cfg.provider.model == "gpt-4o"
    assert cfg.provider.api_key == "sk-secondary"
    assert cfg.provider.api_base == "https://secondary.example/v1"
    # The original connection keeps its own identity in the snapshot.
    assert cfg.providers["openai"]["api_key"] == "sk-primary"
    assert cfg.providers["openai"]["api_base"] == "https://primary.example/v1"
    assert cfg.providers["openai"]["model"] == "gpt-4o"

    # …and switching back restores the primary identity, not the secondary.
    ctrl.select_model("openai", "gpt-4o", "none")
    assert cfg.provider.api_key == "sk-primary"
    assert cfg.provider.api_base == "https://primary.example/v1"


def test_unrelated_extra_and_generation_fields_survive_switches(
    ctrl: SessionControllerBase, cfg: LucNhanConfig
) -> None:
    ctrl.select_model("openai_compat", "gpt-4o", "none")
    assert cfg.provider.extra["custom_flag"] == "keep-me"
    assert cfg.provider.temperature == 0.9
    assert cfg.provider.max_tokens == 1024
    assert cfg.provider.context_window == 64000

    ctrl.select_model("openai", "gpt-4o", "high")
    # The primary connection's own thinking extra survives and only the
    # thinking block is rewritten.
    assert cfg.provider.extra["thinking"]["reasoning_effort"] == "high"
    assert cfg.provider.temperature == 0.2


def test_session_metadata_follows_global_semantics(ctrl: SessionControllerBase, cfg: LucNhanConfig) -> None:
    ctrl.select_model("openai_compat", "gpt-4o", "none")

    for session in ctrl._sessions.values():
        assert session.provider_name == "openai_compat"
        assert session.model_name == "gpt-4o"


def test_transcript_and_draft_are_untouched(ctrl: SessionControllerBase, cfg: LucNhanConfig) -> None:
    session = ctrl._sessions["tab_a"]
    session.metadata["draft"] = "half typed message"
    session.messages.append(object())  # opaque stand-in for a transcript entry
    before = list(session.messages)

    ctrl.select_model("openai_compat", "gpt-4o", "none")

    assert session.messages == before
    assert session.metadata["draft"] == "half typed message"
    assert cfg.provider.model == "gpt-4o"


def test_no_mcp_or_settings_side_effects(ctrl: SessionControllerBase, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(SessionControllerBase, "update_settings", lambda self: called.append("update_settings"))
    monkeypatch.setattr(SessionControllerBase, "reload_mcp", lambda self: called.append("reload_mcp"))

    ctrl.select_model("openai_compat", "gpt-4o", "none")

    assert called == []


# ---------------------------------------------------------------------------
# Thinking normalization
# ---------------------------------------------------------------------------


def test_valid_thinking_level_is_kept(ctrl: SessionControllerBase, cfg: LucNhanConfig) -> None:
    ctrl.select_model("openai_compat", "local-custom-model-x9", "high")

    assert cfg.provider.extra["thinking"] == {
        "enabled": True,
        "reasoning_effort": "high",
        "preserve": True,
    }


def test_none_level_disables_thinking(ctrl: SessionControllerBase, cfg: LucNhanConfig) -> None:
    ctrl.select_model("openai_compat", "local-custom-model-x9", "none")

    assert cfg.provider.extra["thinking"]["enabled"] is False
    assert cfg.provider.extra["thinking"]["reasoning_effort"] == "none"


def test_preserve_flag_survives_thinking_change(ctrl: SessionControllerBase, cfg: LucNhanConfig) -> None:
    cfg.providers["openai_compat"]["extra"] = {
        "custom_flag": "keep-me",
        "thinking": {"enabled": True, "reasoning_effort": "low", "preserve": False},
    }

    ctrl.select_model("openai_compat", "gpt-4o", "medium")

    assert cfg.provider.extra["thinking"]["preserve"] is False
    assert cfg.provider.extra["thinking"]["reasoning_effort"] == "medium"


def test_non_boolean_preserve_falls_back_to_true(ctrl: SessionControllerBase, cfg: LucNhanConfig) -> None:
    cfg.providers["openai_compat"]["extra"] = {
        "thinking": {"enabled": True, "reasoning_effort": "low", "preserve": "yes"},
    }

    ctrl.select_model("openai_compat", "gpt-4o", "medium")

    assert cfg.provider.extra["thinking"]["preserve"] is True


def test_non_dict_thinking_defaults_preserve_true(ctrl: SessionControllerBase, cfg: LucNhanConfig) -> None:
    cfg.providers["openai_compat"]["extra"] = {"thinking": "garbage"}

    ctrl.select_model("openai_compat", "gpt-4o", "medium")

    assert cfg.provider.extra["thinking"] == {
        "enabled": True,
        "reasoning_effort": "medium",
        "preserve": True,
    }


def test_registry_default_used_for_invalid_level_of_unknown_model(
    ctrl: SessionControllerBase, cfg: LucNhanConfig
) -> None:
    """Unknown models accept any registry level; the stored value matches."""
    levels = get_thinking_levels("totally-unknown-model")
    level = levels[-1]
    ctrl.select_model("openai_compat", "totally-unknown-model", level)
    assert cfg.provider.extra["thinking"]["reasoning_effort"] == level


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_successful_selection_persists_to_disk(ctrl: SessionControllerBase, cfg: LucNhanConfig) -> None:
    ctrl.select_model("openai_compat", "gpt-4o", "high")

    on_disk = json.loads(Path(cfg.config_path).read_text(encoding="utf-8"))
    assert on_disk["provider"]["name"] == "openai_compat"
    assert on_disk["provider"]["model"] == "gpt-4o"
    assert on_disk["provider"]["extra"]["thinking"]["reasoning_effort"] == "high"


def test_settings_sees_the_same_selection_afterwards(ctrl: SessionControllerBase, cfg: LucNhanConfig) -> None:
    ctrl.select_model("openai_compat", "gpt-4o", "high")

    # Same object identity — no swap that would leave other owners stale.
    assert ctrl.config is cfg
    reloaded = LucNhanConfig()
    reloaded._config_dir = cfg._config_dir
    reloaded.load()
    assert reloaded.provider.name == "openai_compat"
    assert reloaded.provider.model == "gpt-4o"
    assert reloaded.provider.extra["thinking"]["reasoning_effort"] == "high"
    assert reloaded.provider.api_key == "sk-secondary"


def test_write_failure_keeps_previous_file_and_selection(
    ctrl: SessionControllerBase, cfg: LucNhanConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg.save()  # establish the previous file
    before_bytes = Path(cfg.config_path).read_bytes()
    before_providers = copy.deepcopy(cfg.providers)

    from lucnhan.core import atomic_io

    def _boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(atomic_io, "atomic_replace", _boom)

    with pytest.raises(OSError):
        ctrl.select_model("openai_compat", "gpt-4o", "high")

    assert Path(cfg.config_path).read_bytes() == before_bytes
    # Previous selection is intact in memory (footer source of truth).
    assert cfg.provider.name == "openai"
    assert cfg.provider.model == "gpt-4o"
    assert cfg.provider.api_key == "sk-primary"
    assert cfg.provider.extra["thinking"]["reasoning_effort"] == "none"
    # Per-connection snapshots rolled back to their pre-call values too.
    assert cfg.providers == before_providers
    # No temp file left behind.
    leftovers = [p for p in Path(cfg._config_dir).iterdir() if p.name != CONFIG_FILE]
    assert leftovers == []


def test_write_failure_before_persist_leaves_no_temp_file(
    ctrl: SessionControllerBase, cfg: LucNhanConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(self, password: str = "") -> None:
        raise OSError("disk full")

    # Patch the class of the live instance, not the module attribute: the
    # suite purges stub modules between tests, so a re-imported
    # ``lucnhan.core.config`` would be a different class object.
    monkeypatch.setattr(type(cfg), "save", _boom)

    with pytest.raises(OSError):
        ctrl.select_model("openai_compat", "gpt-4o", "high")

    assert cfg.provider.name == "openai"
    assert cfg.provider.model == "gpt-4o"
    assert cfg.provider.api_key == "sk-primary"
    assert list(Path(cfg._config_dir).iterdir()) == []


def test_encrypted_passwordless_save_keeps_blob_verbatim(ctrl: SessionControllerBase, cfg: LucNhanConfig) -> None:
    cfg.encrypt_api_keys = True
    cfg.save(password="pw-sel-test")
    blob = json.loads(Path(cfg.config_path).read_text(encoding="utf-8"))["encryption"]

    ctrl.select_model("openai_compat", "gpt-4o", "high")

    on_disk = json.loads(Path(cfg.config_path).read_text(encoding="utf-8"))
    assert on_disk["encryption"]["enabled"] is True
    assert on_disk["encryption"]["salt"] == blob["salt"]
    assert on_disk["encryption"]["nonce"] == blob["nonce"]
    assert on_disk["encryption"]["ciphertext"] == blob["ciphertext"]
    assert on_disk["provider"]["api_key"] == ""
    assert on_disk["provider"]["model"] == "gpt-4o"

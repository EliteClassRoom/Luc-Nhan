"""Compatibility tests for legacy config keys removed by the chat-history-on-demand work.

These tests pin the contract that legacy JSON keys removed from ``LucNhanConfig``
must remain silently ignored on load and must never be re-serialized on save.
Unknown keys are dropped by the explicit allow-list in
``_apply_loaded_config``; removal of a field must therefore result in the key
disappearing from the saved file on the next round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path

from lucnhan.core.config import LucNhanConfig


def test_legacy_startup_restore_key_is_ignored_and_not_resaved(tmp_path: Path) -> None:
    """The legacy ``startup_restore_sessions`` JSON key must be dropped on round-trip.

    A config file written by an older Luc Nhan release may contain
    ``"startup_restore_sessions": "all"``. After ``load()`` + ``save()`` the
    key must be gone, the field must no longer exist on the dataclass, and
    ``validate()`` must still report no errors.
    """
    config = LucNhanConfig()
    config._config_dir = str(tmp_path)
    Path(config.config_path).write_text(
        json.dumps({"startup_restore_sessions": "all"}),
        encoding="utf-8",
    )

    config.load()
    config.save()

    saved = json.loads(Path(config.config_path).read_text(encoding="utf-8"))
    # Field is gone from the dataclass entirely.
    assert not hasattr(config, "startup_restore_sessions")
    # Legacy key is not re-serialized.
    assert "startup_restore_sessions" not in saved
    # Validation surface area no longer references this removed field.
    assert config.validate() == []

"""Skills settings tab: enable/disable Luc Nhan, Claude Code, and Codex skills."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...core.config import LucNhanConfig
from ...core.logging import log_debug
from ...skills.loader import SkillDefinition
from ..qt_compat import (
    QCheckBox,
    QGroupBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from ..settings_service import SettingsService


class SkillsTab(QWidget):
    """Tab for managing skills: Luc Nhan built-in/user skills + external skills."""

    def __init__(self, config: LucNhanConfig, service: SettingsService, parent: QWidget | None = None):
        super().__init__(parent)
        self._config = config
        self._service = service
        self._lucnhan_checks: dict[str, QCheckBox] = {}
        self._external_checks: dict[str, QCheckBox] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)

        # Luc Nhan skills (pre-loaded by service)
        lucnhan_group = self._build_lucnhan_group(self._service.skills.lucnhan)
        layout.addWidget(lucnhan_group)

        # External skills (pre-loaded by service)
        for source_key, skills in sorted(self._service.skills.external.items()):
            group = self._build_external_group(source_key, skills)
            layout.addWidget(group)

        layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)

    def _build_lucnhan_group(self, skills: list[SkillDefinition]) -> QGroupBox:
        """Build the Luc Nhan skills group box."""
        group = QGroupBox("Luc Nhan Skills")
        layout = QVBoxLayout(group)

        disabled_set = set(self._config.disabled_skills)

        if not skills:
            layout.addWidget(QLabel("No skills found"))
            return group

        for skill in sorted(skills, key=lambda s: s.slug):
            cb = QCheckBox(f"{skill.slug}  —  {skill.description or '(no description)'}")
            cb.setChecked(skill.slug not in disabled_set)
            self._lucnhan_checks[skill.slug] = cb
            layout.addWidget(cb)

        return group

    def _build_external_group(self, source_key: str, skills: list[SkillDefinition]) -> QGroupBox:
        """Build a group box for external skills from one source."""
        if source_key == "claude":
            title = "Claude Code Skills (~/.claude/skills/)"
        elif source_key == "codex":
            title = "Codex Skills (~/.codex/skills/)"
        else:
            title = f"{source_key} Skills"

        group = QGroupBox(title)
        layout = QVBoxLayout(group)

        if not skills:
            layout.addWidget(QLabel("No skills found"))
            return group

        enabled_set = set(self._config.enabled_external_skills)

        for skill in sorted(skills, key=lambda s: s.slug):
            ext_id = f"{source_key}:{skill.slug}"
            cb = QCheckBox(f"{skill.slug}  —  {skill.description or '(no description)'}")
            cb.setChecked(ext_id in enabled_set)
            self._external_checks[ext_id] = cb
            layout.addWidget(cb)

        return group

    def apply_to_config(self, config: LucNhanConfig) -> None:
        """Write checkbox state back to config fields."""
        # Disabled Luc Nhan skills (unchecked = disabled)
        config.disabled_skills = [slug for slug, cb in self._lucnhan_checks.items() if not cb.isChecked()]

        # Enabled external skills (checked = enabled)
        config.enabled_external_skills = [ext_id for ext_id, cb in self._external_checks.items() if cb.isChecked()]

        log_debug(
            f"Skills config: {len(config.disabled_skills)} disabled, "
            f"{len(config.enabled_external_skills)} external enabled"
        )

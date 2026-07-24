"""Global constants for Rikugan.

This module is data-only — no runtime detection or host probing.
For host capability flags see ``rikugan.core.host``.
"""

from __future__ import annotations

PLUGIN_NAME = "Luc Nhan"
PLUGIN_VERSION = "1.14.0"
PLUGIN_HOTKEY = "Ctrl+Shift+I"
PLUGIN_COMMENT = "Intelligent Reverse-engineering Integrated System"

# Tool name constants — used wherever the security gate (always-approve
# behavior) and the dispatch tables must agree on the literal string.
# A typo at any of those call sites would silently disable the gate.
EXECUTE_PYTHON_TOOL_NAME = "execute_python"

CONFIG_DIR_NAME = "rikugan"
CONFIG_FILE_NAME = "config.json"
CHECKPOINTS_DIR_NAME = "checkpoints"
CACHE_DIR_NAME = "cache"
STRING_CACHE_SCHEMA_VERSION = 1
STRING_CACHE_BUILD_TIMEOUT = 300.0  # seconds — first-build budget for huge binaries

DEFAULT_MAX_TOKENS = 16384
DEFAULT_TEMPERATURE = 0.2
DEFAULT_CONTEXT_WINDOW = 200000

TOOL_RESULT_TRUNCATE_LEN = 8000

SYSTEM_PROMPT_VERSION = 1
CONFIG_SCHEMA_VERSION = 2
SESSION_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Built-in GLM provider (Task 4 of GLM reasoning resilience plan)
# ---------------------------------------------------------------------------
#: Default model for the built-in ``glm`` provider.  The user can override
#: via Settings; this is only the fallback when the saved model is empty.
GLM_DEFAULT_MODEL = "glm-5.2"

SKILLS_DIR_NAME = "skills"
MCP_CONFIG_FILE = "mcp.json"
MCP_TOOL_PREFIX = "mcp_"
MCP_DEFAULT_TIMEOUT = 30.0

# ---------------------------------------------------------------------------
# Central memory workspaces
# ---------------------------------------------------------------------------
MEMORY_DIR_NAME = "memory"
MEMORY_REGISTRY_SCHEMA_VERSION = 2
MEMORY_WORKSPACE_SCHEMA_VERSION = 1
MEMORY_LOCK_TIMEOUT_SECONDS = 5.0
MEMORY_MARKDOWN_MAX_BYTES = 16 * 1024 * 1024

# ---------------------------------------------------------------------------
# On-demand chat history
# ---------------------------------------------------------------------------
# Maximum length (characters) of a derived history title. Titles originate
# from untrusted user/LLM message content, are truncated by the shared
# ``derive_history_title`` helper (spec section 9.1), and rendered as plain
# text in the History panel and as a shorter tab label.
HISTORY_TITLE_MAX_CHARS = 80

# Threshold (seconds) at which the delete-history UI surfaces a "still
# working" notice for slow deletes (large manifests, slow disks). The
# background worker performs one terminal ``HistoryDeleteResult`` per
# request, so the notice is purely cosmetic — it must NEVER cancel the
# delete itself. Spec section 11.5 (chat history deletion) pins this to
# 30s; later tasks may share this value with delete-history UX.
HISTORY_DELETE_SLOW_NOTICE_SECONDS = 30.0

"""Static JSON schemas for Rikugan's pseudo-tools.

These schemas are passed to LLM providers to describe pseudo-tool calls
(``exploration_report``, ``phase_transition``, ``save_memory``,
``spawn_subagent``, ``research_note``, ``ask_user``,
``delegate_external_task``) that the agent loop handles internally
rather than forwarding to the user-supplied tool registry.

All schemas are module-level constants because they do not depend on
runtime state — they are pure data describing the contract between the
LLM and the agent loop.  Putting them in their own module keeps
``loop.py`` focused on control flow and makes it trivial for new agent
modes to reuse the exact same tool surface.

This is the single source of truth for pseudo-tool descriptions: the
``function.description`` strings here are part of the LLM prompt, so
changing them changes model behaviour.  ``loop.py`` imports these
constants rather than redefining them inline.
"""

from __future__ import annotations

# Pseudo-tool: structured finding during binary exploration.
EXPLORATION_REPORT_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "exploration_report",
        "description": (
            "Log a structured finding during binary exploration. "
            "Call this whenever you discover something relevant to "
            "the user's goal: a function's purpose, a key constant, "
            "a data structure, or a hypothesis about what to change."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Type of finding.",
                    "enum": [
                        "function_purpose",
                        "data_structure",
                        "constant",
                        "hypothesis",
                        "string_ref",
                        "import_usage",
                        "patch_result",
                        "general",
                    ],
                },
                "address": {
                    "type": "integer",
                    "description": "Address related to this finding (hex or decimal).",
                },
                "function_name": {
                    "type": "string",
                    "description": "Name of the function (for function_purpose findings).",
                },
                "summary": {
                    "type": "string",
                    "description": "Brief summary of the finding.",
                },
                "evidence": {
                    "type": "string",
                    "description": "Supporting evidence (e.g. decompiled code snippet).",
                },
                "relevance": {
                    "type": "string",
                    "description": "How relevant to the user's goal.",
                    "enum": ["low", "medium", "high"],
                },
                "original_hex": {
                    "type": "string",
                    "description": "Original bytes as hex string (for patch_result category). E.g. '74 05'.",
                },
                "new_hex": {
                    "type": "string",
                    "description": "New patched bytes as hex string (for patch_result category). E.g. '75 05'.",
                },
            },
            "required": ["category", "summary"],
        },
    },
}

# Pseudo-tool: declare an explicit phase transition during exploration.
PHASE_TRANSITION_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "phase_transition",
        "description": (
            "Request to move to the next exploration phase. "
            "Call with to_phase='plan' when you have identified "
            "all locations that need to change and have formed "
            "concrete hypotheses. Requires at least 1 relevant "
            "function and 1 hypothesis logged via exploration_report."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to_phase": {
                    "type": "string",
                    "description": "Target phase to transition to.",
                    "enum": ["plan"],
                },
                "reason": {
                    "type": "string",
                    "description": "Why you're ready to transition.",
                },
            },
            "required": ["to_phase", "reason"],
        },
    },
}

# Pseudo-tool: persist a fact to long-term memory.
SAVE_MEMORY_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "save_memory",
        "description": (
            "Save a fact to persistent memory. "
            "Use this to remember important findings across sessions: "
            "function purposes, naming conventions, architecture notes, "
            "or analysis results that would be useful in future sessions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "The fact or finding to remember.",
                },
                "category": {
                    "type": "string",
                    "description": "Category of the memory.",
                    "enum": [
                        "function_purpose",
                        "architecture",
                        "naming_convention",
                        "prior_analysis",
                        "data_structure",
                        "general",
                    ],
                },
            },
            "required": ["fact", "category"],
        },
    },
}

# Pseudo-tool: spawn a subagent to handle a sub-task in parallel.
SPAWN_SUBAGENT_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "spawn_subagent",
        "description": (
            "Spawn an isolated subagent to handle a complex subtask. "
            "The subagent has its own context window and can use all "
            "available tools. It returns a concise summary of its "
            "findings. Use this to delegate research-heavy tasks "
            "(e.g. 'analyze all functions referencing the score string') "
            "without filling your own context with raw tool output."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the subagent to perform.",
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Maximum turns for the subagent (default: 20).",
                },
            },
            "required": ["task"],
        },
    },
}

# Pseudo-tool: save a research-mode note into the session notebook.
RESEARCH_NOTE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "research_note",
        "description": (
            "Write an Obsidian-compatible markdown research note to the notes/ folder. "
            "Use this to document findings during research mode. Notes should include "
            "[[wiki-links]] to cross-reference other notes, mermaid diagrams for call "
            "flows, and tables for function/address listings. Write notes progressively "
            "as you discover things — don't wait until the end."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "genre": {
                    "type": "string",
                    "description": (
                        "Folder category for the note: networking, crypto, "
                        "initialization, data-structures, persistence, "
                        "anti-analysis, command-and-control, general, etc."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Note title (becomes the filename slug).",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Full markdown body with Obsidian conventions: "
                        "[[wiki-links]], #tags, mermaid diagrams, tables. "
                        "Include addresses, decompiled snippets, and evidence."
                    ),
                },
                "related_notes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Titles of other notes to cross-link via [[wiki-links]].",
                },
            },
            "required": ["genre", "title", "content"],
        },
    },
}

# Pseudo-tool: ask the user a clarifying question.
ASK_USER_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "Ask the user a question and wait for their answer. "
            "Use this when you need clarification, confirmation, "
            "or a choice from the user before proceeding."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the user.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of non-empty, distinct choices "
                        "(e.g. ['Yes', 'No', 'Cancel']). Omit this field "
                        "entirely for open-ended questions where the user "
                        "should type a free-text answer. Never send empty "
                        "strings — each option must be a meaningful choice."
                    ),
                },
            },
            "required": ["question"],
        },
    },
}

# Pseudo-tool: delegate a task to an external agent (Claude Code,
# Codex CLI, or an A2A-compatible HTTP endpoint).
#
# The agent_name must match an entry from
# ``A2ADispatcher.discover()``. The ``context`` field is optional;
# the dispatcher prepends it to the task so the external agent has
# binary context if ``include_context`` is set.
DELEGATE_EXTERNAL_TASK_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "delegate_external_task",
        "description": (
            "Delegate a sub-task to an external agent (Claude Code "
            "CLI, Codex CLI, or an A2A-compatible HTTP endpoint). "
            "Use this when the user's request is better suited to a "
            "separate agent session — e.g. a long code-generation "
            "task that benefits from a fresh context window, or a "
            "research task that another agent can run in parallel. "
            "The external agent's response is returned as the tool "
            "result and forwarded back to the user. "
            "Set ``include_context`` to true to send the current "
            "binary's metadata along with the task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": (
                        "Name of the external agent to delegate to. "
                        "Must be in the discovered agent list "
                        "(use the A2A panel or /a2a slash command to "
                        "list available agents). Common values: "
                        "'claude', 'codex'."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "The task description to send to the external "
                        "agent. Be specific — the external agent has "
                        "no Rikugan/IDA tool access, so include any "
                        "binary details (addresses, decompiled "
                        "snippets, function names) inline."
                    ),
                },
                "include_context": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "If true, prepend the current binary's "
                        "metadata (name, arch, entry point) and the "
                        "current cursor's function context to the "
                        "task before sending."
                    ),
                },
            },
            "required": ["agent", "task"],
        },
    },
}


#: Aggregate list of all pseudo-tool schemas in the order they should be
#: presented to the LLM.  Note: ``DELEGATE_EXTERNAL_TASK_SCHEMA`` is not
#: included here because the agent loop appends it to every run's tool
#: list unconditionally (see ``loop.py``).
ALL_PSEUDO_TOOL_SCHEMAS: tuple[dict, ...] = (
    EXPLORATION_REPORT_SCHEMA,
    PHASE_TRANSITION_SCHEMA,
    SAVE_MEMORY_SCHEMA,
    SPAWN_SUBAGENT_SCHEMA,
    RESEARCH_NOTE_SCHEMA,
    ASK_USER_SCHEMA,
)


__all__ = [
    "ALL_PSEUDO_TOOL_SCHEMAS",
    "ASK_USER_SCHEMA",
    "DELEGATE_EXTERNAL_TASK_SCHEMA",
    "EXPLORATION_REPORT_SCHEMA",
    "PHASE_TRANSITION_SCHEMA",
    "RESEARCH_NOTE_SCHEMA",
    "SAVE_MEMORY_SCHEMA",
    "SPAWN_SUBAGENT_SCHEMA",
]

"""complete orchestration tool — mark task complete with answer (GAIA-style)."""

from __future__ import annotations

COMPLETE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "complete",
        "description": (
            "Mark the task as complete with the given answer. "
            "Use this for GAIA-style tasks where a definitive answer is expected. "
            "This signals that orchestration is done and provides the final answer directly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": "The final answer to the user's question.",
                },
            },
            "required": ["answer"],
        },
    },
}

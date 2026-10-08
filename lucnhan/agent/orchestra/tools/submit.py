"""submit orchestration tool — submit the final result."""

from __future__ import annotations

SUBMIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit",
        "description": (
            "Submit the final result of the orchestration task. "
            "Use this when you have synthesized all sub-agent results and are ready "
            "to provide the final answer to the user. Include a summary of what "
            "was accomplished and the key findings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reasoning": {
                    "type": "string",
                    "description": "Final reasoning or synthesis of the sub-agent results.",
                },
                "result": {
                    "type": "string",
                    "description": "The final answer or result to present to the user.",
                },
            },
            "required": ["reasoning"],
        },
    },
}

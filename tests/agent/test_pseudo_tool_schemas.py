"""Tests for lucnhan.agent.pseudo_tool_schemas."""

from __future__ import annotations

from lucnhan.agent.pseudo_tool_schemas import (
    ALL_PSEUDO_TOOL_SCHEMAS,
    ASK_USER_SCHEMA,
    DELEGATE_EXTERNAL_TASK_SCHEMA,
    EXPLORATION_REPORT_SCHEMA,
    PHASE_TRANSITION_SCHEMA,
    RESEARCH_NOTE_SCHEMA,
    SAVE_MEMORY_SCHEMA,
    SPAWN_SUBAGENT_SCHEMA,
    __all__,
)


def test_all_exports_are_listed() -> None:
    """`__all__` must include every public schema (and the aggregate)."""
    assert set(__all__) == {
        "ALL_PSEUDO_TOOL_SCHEMAS",
        "ASK_USER_SCHEMA",
        "DELEGATE_EXTERNAL_TASK_SCHEMA",
        "EXPLORATION_REPORT_SCHEMA",
        "PHASE_TRANSITION_SCHEMA",
        "RESEARCH_NOTE_SCHEMA",
        "SAVE_MEMORY_SCHEMA",
        "SPAWN_SUBAGENT_SCHEMA",
    }


def test_aggregate_contains_every_individual_schema() -> None:
    """ALL_PSEUDO_TOOL_SCHEMAS must reference every individual schema exactly once."""
    expected = [
        EXPLORATION_REPORT_SCHEMA,
        PHASE_TRANSITION_SCHEMA,
        SAVE_MEMORY_SCHEMA,
        SPAWN_SUBAGENT_SCHEMA,
        RESEARCH_NOTE_SCHEMA,
        ASK_USER_SCHEMA,
    ]
    assert list(ALL_PSEUDO_TOOL_SCHEMAS) == expected
    assert len(ALL_PSEUDO_TOOL_SCHEMAS) == 6


def test_every_schema_is_openai_function_format() -> None:
    """Every schema must follow the {type: function, function: {name, ...}} shape."""
    for schema in ALL_PSEUDO_TOOL_SCHEMAS:
        assert schema["type"] == "function", schema["function"]["name"]
        fn = schema["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict) and params["properties"]
        # `required` is optional but if present must be a list
        assert "required" not in params or isinstance(params["required"], list)


def _required_fields(schema: dict) -> list[str]:
    return list(schema["function"]["parameters"].get("required", []))


def test_exploration_report_requires_category_and_summary() -> None:
    assert _required_fields(EXPLORATION_REPORT_SCHEMA) == ["category", "summary"]


def test_phase_transition_requires_to_phase_and_reason() -> None:
    assert _required_fields(PHASE_TRANSITION_SCHEMA) == ["to_phase", "reason"]


def test_save_memory_requires_fact_and_category() -> None:
    """MAIN requires an explicit category so memories are classified on write."""
    assert _required_fields(SAVE_MEMORY_SCHEMA) == ["fact", "category"]


def test_spawn_subagent_requires_task_only() -> None:
    assert _required_fields(SPAWN_SUBAGENT_SCHEMA) == ["task"]


def test_research_note_requires_genre_title_and_content() -> None:
    """MAIN requires a genre so notes are filed into the right notebook folder."""
    assert _required_fields(RESEARCH_NOTE_SCHEMA) == ["genre", "title", "content"]


def test_ask_user_requires_question_only() -> None:
    assert _required_fields(ASK_USER_SCHEMA) == ["question"]


def test_ask_user_options_description_forbids_empty_strings() -> None:
    """The options description must tell the LLM that empty strings are invalid.

    Without this guidance, weaker models send ``options: [""]`` for open-ended
    questions, which renders a single empty button in the UI and locks the
    text input (the panel treats ``bool([""])`` as "has options → button-only").
    """
    options_desc = ASK_USER_SCHEMA["function"]["parameters"]["properties"]["options"]["description"]
    assert "non-empty" in options_desc.lower() or "empty" in options_desc.lower()
    # Must instruct the LLM to omit the field for open-ended questions
    assert "omit" in options_desc.lower() or "open-ended" in options_desc.lower()


def test_tool_names_are_unique_across_aggregate() -> None:
    """Anthropic rejects requests with duplicate tool names."""
    names = [s["function"]["name"] for s in ALL_PSEUDO_TOOL_SCHEMAS]
    assert len(names) == len(set(names)), f"Duplicate tool names: {names}"


def test_save_memory_category_is_enum() -> None:
    """MAIN classifies memories with a fixed category enum (no default; required)."""
    category_prop = SAVE_MEMORY_SCHEMA["function"]["parameters"]["properties"]["category"]
    assert category_prop["type"] == "string"
    assert "enum" in category_prop
    assert "general" in category_prop["enum"]
    # category is required, so it has no default
    assert "default" not in category_prop


def test_spawn_subagent_has_optional_max_turns() -> None:
    """MAIN exposes max_turns (optional) instead of an agent_type selector."""
    props = SPAWN_SUBAGENT_SCHEMA["function"]["parameters"]["properties"]
    assert "max_turns" in props
    assert props["max_turns"]["type"] == "integer"
    assert "agent_type" not in props
    assert "max_turns" not in SPAWN_SUBAGENT_SCHEMA["function"]["parameters"].get("required", [])


def test_delegate_external_task_requires_agent_and_task() -> None:
    """The A2A delegation tool must require both agent name and task text."""
    required = _required_fields(DELEGATE_EXTERNAL_TASK_SCHEMA)
    assert "agent" in required
    assert "task" in required


def test_delegate_external_task_include_context_defaults_false() -> None:
    """include_context should default to false so the LLM opts in explicitly."""
    props = DELEGATE_EXTERNAL_TASK_SCHEMA["function"]["parameters"]["properties"]
    assert props["include_context"]["default"] is False

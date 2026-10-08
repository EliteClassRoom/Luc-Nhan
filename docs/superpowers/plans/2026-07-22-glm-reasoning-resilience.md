# GLM Reasoning Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a GLM-specific provider dialect that preserves reasoning correctly, aborts degenerated reasoning before the normal output cap, and performs exactly one non-thinking recovery without changing non-GLM behavior.

**Architecture:** Extend the provider-neutral message, stream, request-context, outcome, and event contracts first. Build GLM configuration and protocol handling on top of those contracts, then add a bounded detector and one-shot transaction in `execute_single_turn()`. Keep live reasoning transient in the UI, persist only accepted outcomes, and use an allowlisted structured telemetry record with explicit authoritative/estimated usage provenance.

**Tech Stack:** Python 3.11+, dataclasses/enums, OpenAI Python SDK 2.x, pytest, unittest/Qt stubs, Ruff, mypy, uv.

## Global Constraints

- Work on an isolated worktree created through `superpowers:using-git-worktrees` before implementation.
- Follow TDD: add one focused failing test, run it red, make the smallest implementation, run it green, then commit.
- New function signatures and all modified function signatures require type annotations.
- GLM strict malformed/truncated tool handling is GLM-only; non-GLM provider payloads and behavior remain unchanged.
- Automatic degeneration recovery is at most two logical attempts: normal attempt 1 plus recovery attempt 2; existing bounded transport retries remain inside each logical attempt.
- Recovery uses `thinking.type="disabled"`, `reasoning_effort="none"`, and `min(16_384, selected_model_max_output_tokens)` total output tokens.
- Guard formula is exactly `T AND (H OR (R AND M AND V))` with the constants in the design spec.
- Never persist discarded reasoning or include it in future provider context.
- Never log reasoning, visible text, tool arguments/results, messages, request/response bodies, errors, authorization values, or API keys in structured attempt telemetry.
- Do not touch or stage `docs/superpowers/plans/2026-07-22-memory-durability-and-orchestra-gate.md` or `docs/superpowers/specs/2026-07-22-memory-durability-and-orchestra-gate-design.md`.
- Use `uv run pytest`, `uv run ruff`, and `uv run mypy`; do not invoke bare Python/pip tooling.

## File and Consumer Map

### Files to create

- `lucnhan/core/glm_config.py` — typed GLM config, model metadata, validation.
- `lucnhan/providers/glm_provider.py` — GLM request/message/client/model dialect over OpenAI transport.
- `lucnhan/agent/glm_guard.py` — bounded streaming degeneration detector.
- `tests/core/test_glm_config.py`
- `tests/providers/test_glm_provider.py`
- `tests/agent/test_glm_guard.py`
- `tests/agent/test_glm_recovery.py`
- `tests/ui/test_chat_view_glm.py`

### Existing files to modify

- `lucnhan/constants.py` — add built-in GLM default model.
- `lucnhan/core/types.py` — reasoning, request context, usage provenance, capabilities, serialization.
- `lucnhan/core/config.py` — GLM validation and deep-copy provider `extra` persistence/migration.
- `lucnhan/core/log_sinks.py`, `lucnhan/core/logging.py` — allowlisted structured attempt logging.
- `lucnhan/providers/base.py` — request-context plumbing.
- `lucnhan/providers/openai_provider.py` — capability-gated reasoning delta while preserving OpenAI inline thinking.
- `lucnhan/providers/anthropic_provider.py`, `gemini_provider.py`, `codex_provider.py`, `minimax_provider.py` — accept the request-context keyword without changing payloads.
- `lucnhan/providers/registry.py` — built-in GLM and custom dialect routing.
- `lucnhan/agent/turn.py` — reasoning/recovery/discard events and JSON serialization.
- `lucnhan/agent/loop.py` — typed attempt outcomes, guard integration, queue coalescing.
- `lucnhan/agent/modes/turn_helpers.py` — durable commit boundary and recovery transaction.
- `lucnhan/agent/modes/plan.py` — direct typed-outcome consumer.
- `lucnhan/agent/prompts/base.py` — protocol-oriented parallel tool wording.
- `lucnhan/state/session.py` — usage recording without a persisted message.
- `lucnhan/agent/context_window.py` — explicit reasoning omission during old-message compaction.
- `lucnhan/ui/session_controller_base.py` — pass active `extra` into provider creation.
- `lucnhan/ui/settings_dialog.py` — GLM controls and opt-in Z.AI migration.
- `lucnhan/ui/chat_view.py`, `lucnhan/ui/tool_widgets.py` — transient reasoning/recovery/discard rendering and restore.
- `lucnhan/headless/runner.py`, `lucnhan/control/server.py` — explicit pass-through semantics.

### Consumers that must be migrated or regression-tested

- `_stream_llm_turn()` direct consumers:
  - `lucnhan/agent/modes/turn_helpers.py::execute_single_turn`
  - `lucnhan/agent/modes/plan.py::_generate_plan_text`
- `execute_single_turn()` consumers:
  - `lucnhan/agent/modes/normal.py::run_normal_loop`
  - `lucnhan/agent/modes/exploration.py` at analysis, synthesis, and execution phases
  - `lucnhan/agent/modes/plan.py::_execute_step`
  - `lucnhan/agent/modes/research.py` research turn
- `_build_request_kwargs()` implementations:
  - `LLMProvider`, `OpenAIProvider`, `AnthropicProvider`, `GeminiProvider`, `CodexProvider`, `MiniMaxProvider`; `OpenAICompatProvider` inherits OpenAI behavior
- `_stream_chunks()` implementations:
  - `OpenAIProvider`, `AnthropicProvider`, `GeminiProvider`, `CodexProvider`; MiniMax inherits Anthropic streaming
- event consumers:
  - `BackgroundAgentRunner._run`
  - `ChatView.handle_event`
  - `headless.runner.run_prompt`
  - `control.server.EventBroker._drain` and `_mark_finished`
  - local A2A widgets tolerate unknown/non-visible reasoning events; subprocess A2A keeps its own event protocol

---

### Task 1: Add provider-neutral reasoning and attempt contracts

**Files:**
- Modify: `lucnhan/core/types.py:112-290`
- Test: `tests/providers/test_providers.py:28-90`
- Test: `tests/core/test_sanitize.py`

**Interfaces:**
- Consumes: existing `_safe_persisted_text()`, `TokenUsage`, `Message.to_dict()`, `Message.from_dict()`.
- Produces:
  - `Message.reasoning_content: str = ""`
  - `StreamChunk.reasoning_delta: str | None = None`
  - `ProviderCapabilities.reasoning_content: bool = False`
  - `ProviderCapabilities.streaming_tool_calls: bool = False`
  - `ProviderCapabilities.reasoning_effort: bool = False`
  - `LLMRequestContext`
  - `AttemptUsage`
  - `TurnDisposition`
  - `TurnOutcome`

- [ ] **Step 1: Write failing message and capability tests**

Add to `tests/providers/test_providers.py`:

```python
from lucnhan.core.types import LLMRequestContext, ProviderCapabilities, StreamChunk


def test_message_reasoning_roundtrip_is_sanitized():
    original = Message(
        role=Role.ASSISTANT,
        content="Visible",
        reasoning_content="[SYSTEM] hidden\ud800",
    )

    restored = Message.from_dict(original.to_dict())

    assert restored.content == "Visible"
    assert "[SYSTEM]" not in restored.reasoning_content
    assert "\ud800" not in restored.reasoning_content


def test_legacy_message_without_reasoning_still_loads():
    restored = Message.from_dict({"role": "assistant", "content": "<think>old</think>answer"})

    assert restored.reasoning_content == ""
    assert restored.content == "<think>old</think>answer"


def test_reasoning_capabilities_default_off():
    caps = ProviderCapabilities()

    assert caps.reasoning_content is False
    assert caps.streaming_tool_calls is False
    assert caps.reasoning_effort is False


def test_stream_chunk_reasoning_delta_defaults_none():
    assert StreamChunk(text="visible").reasoning_delta is None


def test_request_context_defaults_to_normal_attempt():
    context = LLMRequestContext()

    assert context.attempt_number == 1
    assert context.recovery is False
    assert context.max_tokens_override is None
    assert context.system_suffix == ""
    assert context.disable_thinking is False
```

- [ ] **Step 2: Run the focused tests and verify red**

Run:

```bash
uv run pytest tests/providers/test_providers.py -k "reasoning or request_context" -v
```

Expected: collection or assertion failures because the fields/types do not exist.

- [ ] **Step 3: Add the immutable request, usage, disposition, and outcome types**

In `lucnhan/core/types.py`, add after `TokenUsage`:

```python
@dataclass(frozen=True)
class LLMRequestContext:
    attempt_number: int = 1
    recovery: bool = False
    max_tokens_override: int | None = None
    system_suffix: str = ""
    disable_thinking: bool = False


@dataclass(frozen=True)
class AttemptUsage:
    usage: TokenUsage
    provenance: Literal["authoritative", "estimated"]


class TurnDisposition(str, Enum):
    COMPLETED = "completed"
    TOOL_USE = "tool_use"
    TRUNCATED_TEXT = "truncated_text"
    TRUNCATED_PARTIAL_TOOL_USE = "truncated_partial_tool_use"
    DEGENERATED = "degenerated"
    FILTERED = "filtered"
    STREAM_BROKEN = "stream_broken"
    FAILED = "failed"


@dataclass
class TurnOutcome:
    visible_text: str = ""
    reasoning_content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage | None = None
    raw_parts: Any = None
    finish_reason: str | None = None
    disposition: TurnDisposition = TurnDisposition.FAILED
    attempt_usage: AttemptUsage | None = None
    guard_trigger: str = ""
    repetition_ratio_millis: int = 0
```

Add `Literal` to the typing imports. Append the three capability flags to `ProviderCapabilities`. Add `reasoning_delta` to `StreamChunk`.

- [ ] **Step 4: Add message persistence without legacy tag migration**

Append `reasoning_content: str = ""` to `Message`. In `Message.to_dict()`, serialize it only when non-empty:

```python
if self.reasoning_content:
    d["reasoning_content"] = self.reasoning_content
```

In `Message.from_dict()`, pass:

```python
reasoning_content=_safe_persisted_text(d.get("reasoning_content")),
```

Do not parse `<think>` tags from `content`.

- [ ] **Step 5: Run type-focused tests green**

Run:

```bash
uv run pytest tests/providers/test_providers.py tests/core/test_sanitize.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/core/types.py tests/providers/test_providers.py tests/core/test_sanitize.py
git commit -m "feat(types): add reasoning attempt contracts"
```

---

### Task 2: Add reasoning, recovery, and discarded-tool events

**Files:**
- Modify: `lucnhan/agent/turn.py:12-70,442-484`
- Test: `tests/agent/test_turn_events.py`

**Interfaces:**
- Consumes: existing `TurnEvent.metadata`, tool ID/name fields.
- Produces:
  - `TurnEventType.REASONING_DELTA`
  - `TurnEventType.RECOVERY_START`
  - `TurnEventType.TOOL_CALL_DISCARDED`
  - `TurnEvent.reasoning_event()`
  - `TurnEvent.recovery_start()`
  - `TurnEvent.tool_call_discarded()`

- [ ] **Step 1: Write failing event tests**

Add to `tests/agent/test_turn_events.py`:

```python
def test_reasoning_event_serializes_distinct_payload():
    event = TurnEvent.reasoning_event("private chain")

    assert event.type == TurnEventType.REASONING_DELTA
    assert event.reasoning == "private chain"
    assert event.text == ""
    assert event.to_dict()["reasoning"] == "private chain"


def test_recovery_start_serializes_boundary_metadata():
    event = TurnEvent.recovery_start(
        attempt=2,
        reason="reasoning_degenerated",
        discard_transient_reasoning=True,
    )

    assert event.type == TurnEventType.RECOVERY_START
    assert event.metadata == {
        "attempt": 2,
        "reason": "reasoning_degenerated",
        "discard_transient_reasoning": True,
    }


def test_tool_call_discarded_closes_started_call():
    event = TurnEvent.tool_call_discarded("call_1", "read_bytes", "truncated_arguments")

    assert event.type == TurnEventType.TOOL_CALL_DISCARDED
    assert event.tool_call_id == "call_1"
    assert event.tool_name == "read_bytes"
    assert event.metadata["reason"] == "truncated_arguments"
```

- [ ] **Step 2: Verify red**

```bash
uv run pytest tests/agent/test_turn_events.py -k "reasoning or recovery or discarded" -v
```

Expected: FAIL because event types/factories do not exist.

- [ ] **Step 3: Add event types, payload, and factories**

In `TurnEventType` add:

```python
REASONING_DELTA = "reasoning_delta"
RECOVERY_START = "recovery_start"
TOOL_CALL_DISCARDED = "tool_call_discarded"
```

Add `reasoning: str = ""` to `TurnEvent`. Add factories:

```python
@staticmethod
def reasoning_event(text: str) -> TurnEvent:
    return TurnEvent(type=TurnEventType.REASONING_DELTA, reasoning=text)


@staticmethod
def recovery_start(
    *,
    attempt: int,
    reason: str,
    discard_transient_reasoning: bool,
) -> TurnEvent:
    return TurnEvent(
        type=TurnEventType.RECOVERY_START,
        metadata={
            "attempt": attempt,
            "reason": reason,
            "discard_transient_reasoning": discard_transient_reasoning,
        },
    )


@staticmethod
def tool_call_discarded(tool_call_id: str, tool_name: str, reason: str) -> TurnEvent:
    return TurnEvent(
        type=TurnEventType.TOOL_CALL_DISCARDED,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        metadata={"reason": reason},
    )
```

In `to_dict()` add:

```python
if self.reasoning:
    d["reasoning"] = self.reasoning
```

- [ ] **Step 4: Run the event suite green**

```bash
uv run pytest tests/agent/test_turn_events.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lucnhan/agent/turn.py tests/agent/test_turn_events.py
git commit -m "feat(events): add GLM reasoning recovery events"
```

---

### Task 3: Add content-free structured attempt logging

**Files:**
- Modify: `lucnhan/core/log_sinks.py:195-212`
- Modify: `lucnhan/core/logging.py:1-109`
- Test: `tests/core/test_logging.py`

**Interfaces:**
- Consumes: `strip_injection_markers()`, `strip_lone_surrogates()`.
- Produces: `log_structured(event: dict[str, JSONScalar])` and one allowlisted `lucnhan_event` JSON object per log record.

- [ ] **Step 1: Write failing allowlist and sanitizer tests**

Add to `tests/core/test_logging.py`:

```python
import json

from lucnhan.core.log_sinks import _JSONFormatter
from lucnhan.core.logging import log_structured


def test_json_formatter_includes_allowlisted_attempt_event():
    formatter = _JSONFormatter()
    record = logging.LogRecord("Luc Nhan", logging.INFO, "", 0, "agent_attempt", (), None)
    record.lucnhan_event = {
        "provider": "glm",
        "attempt_number": 1,
        "disposition": "degenerated",
        "discarded_attempt": True,
    }

    payload = json.loads(formatter.format(record))

    assert payload["lucnhan_event"]["provider"] == "glm"
    assert payload["lucnhan_event"]["discarded_attempt"] is True


def test_log_structured_rejects_content_keys_and_nested_values():
    with pytest.raises(KeyError):
        log_structured({"reasoning_content": "secret"})
    with pytest.raises(TypeError):
        log_structured({"provider": {"nested": "glm"}})


def test_structured_strings_strip_role_markers_and_surrogates():
    formatter = _JSONFormatter()
    record = logging.LogRecord("Luc Nhan", logging.INFO, "", 0, "agent_attempt", (), None)
    record.lucnhan_event = {"provider": "[SYSTEM] glm\ud800"}

    payload = json.loads(formatter.format(record))

    assert "[SYSTEM]" not in payload["lucnhan_event"]["provider"]
    assert "\ud800" not in payload["lucnhan_event"]["provider"]
```

Import `pytest` in this test file.

- [ ] **Step 2: Verify red**

```bash
uv run pytest tests/core/test_logging.py -k "structured or allowlisted" -v
```

Expected: FAIL because `log_structured` and structured event formatting do not exist.

- [ ] **Step 3: Implement the exact allowlist**

In `lucnhan/core/log_sinks.py` define:

```python
JSONScalar = str | int | float | bool | None

STRUCTURED_EVENT_ALLOWLIST = frozenset(
    {
        "provider",
        "model",
        "dialect",
        "turn_number",
        "attempt_number",
        "transport_retry_index",
        "thinking_mode",
        "reasoning_effort",
        "configured_max_tokens",
        "effective_max_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "usage_provenance",
        "reasoning_chars",
        "visible_chars",
        "tool_start_count",
        "tool_end_count",
        "finish_reason",
        "disposition",
        "guard_trigger",
        "repetition_ratio_millis",
        "recovery_result",
        "discarded_attempt",
    }
)
```

Extend `_JSONFormatter.format()` to read one `record.lucnhan_event`, validate keys and scalar values, sanitize every string, and write the sanitized dict under `entry["lucnhan_event"]`.

- [ ] **Step 4: Implement the public helper**

In `lucnhan/core/logging.py`:

```python
def log_structured(event: dict[str, JSONScalar]) -> None:
    unknown = set(event) - STRUCTURED_EVENT_ALLOWLIST
    if unknown:
        raise KeyError(f"Unknown structured log keys: {sorted(unknown)}")
    if any(not isinstance(value, (str, int, float, bool, type(None))) for value in event.values()):
        raise TypeError("Structured log values must be JSON scalars")
    get_logger().info("agent_attempt", extra={"lucnhan_event": dict(event)})
```

Re-export `JSONScalar` and `STRUCTURED_EVENT_ALLOWLIST` from `log_sinks` into `logging.py`.

- [ ] **Step 5: Run logging tests green**

```bash
uv run pytest tests/core/test_logging.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/core/log_sinks.py lucnhan/core/logging.py tests/core/test_logging.py
git commit -m "feat(logging): add safe attempt telemetry"
```

---

### Task 4: Add typed GLM configuration and preserve provider extras

**Files:**
- Create: `lucnhan/core/glm_config.py`
- Modify: `lucnhan/constants.py:33-50`
- Modify: `lucnhan/core/config.py:17-56,181-241,283-303,403-439`
- Test: `tests/core/test_glm_config.py`
- Test: `tests/headless/test_provider_config.py:270-405`

**Interfaces:**
- Consumes: `ProviderConfig.extra`, provider model limits.
- Produces:
  - `GLMModelMetadata`
  - `GLMThinkingConfig`
  - `GLMGuardConfig`
  - `GLMConfig`
  - `parse_glm_extra(extra, model_id)`
  - deep-copy save/switch behavior

- [ ] **Step 1: Write failing config parser tests**

Create `tests/core/test_glm_config.py`:

```python
import pytest

from lucnhan.core.glm_config import get_glm_model_metadata, parse_glm_extra


def test_default_glm_config_is_guarded_and_preserved():
    parsed = parse_glm_extra({"dialect": "glm"}, "glm-5.2")

    assert parsed.thinking.enabled is True
    assert parsed.thinking.reasoning_effort == "max"
    assert parsed.thinking.preserve is True
    assert parsed.guard.enabled is True
    assert parsed.guard.reasoning_token_ceiling == 16_384
    assert parsed.guard.recovery_max_tokens == 16_384


def test_glm_config_rejects_unknown_nested_key_with_field_path():
    with pytest.raises(ValueError, match=r"provider.extra.thinking.unknown"):
        parse_glm_extra(
            {"dialect": "glm", "thinking": {"unknown": True}},
            "glm-5.2",
        )


def test_glm_config_validates_ranges():
    with pytest.raises(ValueError, match=r"reasoning_token_ceiling"):
        parse_glm_extra(
            {
                "dialect": "glm",
                "degeneration_guard": {"reasoning_token_ceiling": 1023},
            },
            "glm-5.2",
        )


def test_unknown_glm_model_disables_tool_stream_and_effort():
    metadata = get_glm_model_metadata("glm-experimental")

    assert metadata.reasoning_content is True
    assert metadata.streaming_tool_calls is False
    assert metadata.reasoning_effort is False
```

- [ ] **Step 2: Write failing provider-extra deep-copy tests**

Add to `tests/headless/test_provider_config.py`:

```python
def test_switch_provider_preserves_nested_extra_without_aliasing():
    cfg = LucNhanConfig()
    cfg.provider.name = "glm"
    cfg.provider.extra = {"dialect": "glm", "thinking": {"enabled": True}}

    cfg.switch_provider("openai")
    cfg.switch_provider("glm")

    assert cfg.provider.extra["thinking"]["enabled"] is True
    cfg.provider.extra["thinking"]["enabled"] = False
    assert cfg.providers["glm"]["extra"]["thinking"]["enabled"] is True
```

- [ ] **Step 3: Verify red**

```bash
uv run pytest tests/core/test_glm_config.py tests/headless/test_provider_config.py -k "glm or extra" -v
```

Expected: FAIL because parser/metadata and `extra` snapshot/restore are absent.

- [ ] **Step 4: Implement GLM dataclasses and exact model metadata**

Create `lucnhan/core/glm_config.py` with frozen dataclasses:

```python
@dataclass(frozen=True)
class GLMModelMetadata:
    context_window: int
    max_output_tokens: int
    reasoning_content: bool
    streaming_tool_calls: bool
    reasoning_effort: bool


@dataclass(frozen=True)
class GLMThinkingConfig:
    enabled: bool = True
    reasoning_effort: str = "max"
    preserve: bool = True


@dataclass(frozen=True)
class GLMGuardConfig:
    enabled: bool = True
    reasoning_token_ceiling: int = 16_384
    retry_without_thinking: bool = True
    recovery_max_tokens: int = 16_384


@dataclass(frozen=True)
class GLMConfig:
    thinking: GLMThinkingConfig
    guard: GLMGuardConfig
```

Use 1,000,000 context and 131,072 output for `glm-5.2`; use the Z.AI documented 131,072 output limit for known GLM-5.1/5/4.7 entries. Mark `reasoning_effort=True` only for `glm-5.2`; mark streamed tools true only for documented GLM-4.6-and-newer entries.

Implement `parse_glm_extra()` with exact key sets, exact field-path errors, boolean checks, effort enum checks, and ranges from the spec. Clamp effective recovery cap when building requests, not while parsing saved intent.

- [ ] **Step 5: Preserve deep-copied `extra` and validate active GLM config**

Import `copy` in `config.py`. Add GLM to `PROVIDER_DEFAULT_MODELS` and built-in names. In `_snapshot_current_provider()` include:

```python
"extra": copy.deepcopy(self.provider.extra),
```

In `switch_provider()` restore:

```python
self.provider.extra = copy.deepcopy(saved.get("extra", {}))
```

and reset fresh providers with `self.provider.extra = {}`.

In `validate()`, when `provider.extra.get("dialect") == "glm"`, call `parse_glm_extra()` and append its exact `ValueError` string. Do not reinterpret unknown non-GLM extras.

- [ ] **Step 6: Run config tests green**

```bash
uv run pytest tests/core/test_glm_config.py tests/headless/test_provider_config.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/constants.py lucnhan/core/config.py lucnhan/core/glm_config.py tests/core/test_glm_config.py tests/headless/test_provider_config.py
git commit -m "feat(config): add GLM dialect settings"
```

---

### Task 5: Thread immutable request context through all providers

**Files:**
- Modify: `lucnhan/providers/base.py:95-198`
- Modify: `lucnhan/providers/openai_provider.py:320-351`
- Modify: `lucnhan/providers/anthropic_provider.py:517-579`
- Modify: `lucnhan/providers/gemini_provider.py:256-307`
- Modify: `lucnhan/providers/codex_provider.py:474-535`
- Modify: `lucnhan/providers/minimax_provider.py:193-210`
- Modify: `tests/agent/test_agent_loop.py:35-90`
- Modify: `tests/agent/test_exploration_loop.py:33-95`
- Modify: `tests/agent/test_subagent_manager.py:35-70`
- Test: `tests/providers/test_provider_streaming.py`
- Test: `tests/providers/test_openai_provider.py`
- Test: `tests/providers/test_anthropic_provider.py`
- Test: `tests/providers/test_gemini_provider.py`
- Test: `tests/providers/test_codex_provider.py`

**Interfaces:**
- Consumes: `LLMRequestContext` from Task 1.
- Produces: keyword-only `request_context: LLMRequestContext | None = None` from `chat()`/`chat_stream()` to `_build_request_kwargs()`; no non-GLM payload changes.

- [ ] **Step 1: Write a failing sentinel forwarding test**

In `tests/providers/test_provider_streaming.py`, add this minimal test provider and assertion:

```python
class ContextCapturingProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test", model="test")
        self.seen_context: LLMRequestContext | None = None

    @property
    def name(self) -> str:
        return "context-capturing"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def _get_client(self):
        return object()

    def _fetch_models_live(self):
        return []

    @staticmethod
    def _builtin_models():
        return []

    def _format_messages(self, messages):
        return messages

    def _build_request_kwargs(
        self,
        messages,
        tools,
        temperature,
        max_tokens,
        system,
        *,
        request_context=None,
    ):
        self.seen_context = request_context
        return {}

    def _call_api(self, client, kwargs):
        return None

    def _normalize_response(self, raw):
        return Message(role=Role.ASSISTANT)

    def _handle_api_error(self, error):
        raise error

    def _stream_chunks(self, client, kwargs, cancel_event=None):
        if False:
            yield StreamChunk()


def test_chat_stream_forwards_request_context():
    provider = ContextCapturingProvider()
    context = LLMRequestContext(attempt_number=2, recovery=True)

    list(provider.chat_stream([], request_context=context))

    assert provider.seen_context is context
```

- [ ] **Step 2: Write non-GLM payload equivalence tests**

In each provider request test, construct baseline kwargs and kwargs with `LLMRequestContext(attempt_number=2, recovery=True)`, then assert equality for OpenAI, Anthropic, Gemini, Codex, and MiniMax. For example in `tests/providers/test_openai_provider.py`:

```python
def test_request_context_does_not_change_openai_payload():
    provider = _make_provider()
    messages = [Message(role=Role.USER, content="hello")]
    baseline = provider._build_request_kwargs(messages, None, 0.3, 4096, "system")
    contextual = provider._build_request_kwargs(
        messages,
        None,
        0.3,
        4096,
        "system",
        request_context=LLMRequestContext(recovery=True),
    )

    assert contextual == baseline
```

- [ ] **Step 3: Verify red**

```bash
uv run pytest tests/providers/test_provider_streaming.py tests/providers/test_openai_provider.py -k "request_context" -v
```

Expected: FAIL due to unexpected keyword arguments.

- [ ] **Step 4: Extend the base interfaces**

Add keyword-only `request_context` to `LLMProvider._build_request_kwargs()`, `chat()`, and `chat_stream()`. Build the effective system and token limit once:

```python
context = request_context or LLMRequestContext()
effective_system = safe_system + context.system_suffix
effective_max_tokens = context.max_tokens_override or max_tokens
kwargs = self._build_request_kwargs(
    safe_messages,
    tools,
    temperature,
    effective_max_tokens,
    effective_system,
    request_context=context,
)
```

Do not pass context to `_stream_chunks()`; the request kwargs already encode request-local transport behavior, and only the agent layer needs attempt metadata.

- [ ] **Step 5: Update every concrete `_build_request_kwargs()` override and mock**

Add the same keyword-only parameter with a default to OpenAI, Anthropic, Gemini, Codex, MiniMax, and all test mock providers listed in this task. Ignore it in non-GLM implementations.

- [ ] **Step 6: Run all provider tests green**

```bash
uv run pytest tests/providers tests/agent/test_agent_loop.py tests/agent/test_exploration_loop.py tests/agent/test_subagent_manager.py -v
```

Expected: PASS and payload equivalence assertions pass.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/providers/base.py lucnhan/providers/openai_provider.py lucnhan/providers/anthropic_provider.py lucnhan/providers/gemini_provider.py lucnhan/providers/codex_provider.py lucnhan/providers/minimax_provider.py tests/providers tests/agent/test_agent_loop.py tests/agent/test_exploration_loop.py tests/agent/test_subagent_manager.py
git commit -m "refactor(providers): thread request context"
```

---

### Task 6: Implement the GLM provider dialect and registry routing

**Files:**
- Create: `lucnhan/providers/glm_provider.py`
- Modify: `lucnhan/providers/openai_provider.py:35-70,115-235,440-640`
- Modify: `lucnhan/providers/registry.py:18-199`
- Modify: `lucnhan/ui/session_controller_base.py:425-447`
- Test: `tests/providers/test_glm_provider.py`
- Test: `tests/providers/test_providers.py`
- Test: `tests/providers/test_openai_provider.py`

**Interfaces:**
- Consumes: `GLMConfig`, GLM metadata, capability flags, `LLMRequestContext`.
- Produces: `GLMProvider`, built-in `glm`, custom `extra.dialect == "glm"` routing, separate `reasoning_delta`.

- [ ] **Step 1: Write failing GLM message/request tests**

Create `tests/providers/test_glm_provider.py` with tests:

```python
def test_glm_replays_reasoning_content_without_think_tags():
    provider = GLMProvider(api_key="test", api_base="https://api.z.ai/api/paas/v4", model="glm-5.2")
    message = Message(
        role=Role.ASSISTANT,
        content="Visible",
        reasoning_content="Reasoning",
        tool_calls=[ToolCall(id="call_1", name="read_bytes", arguments={"address": 4096})],
    )

    wire = provider._format_messages([message])[0]

    assert wire["reasoning_content"] == "Reasoning"
    assert wire["content"] == "Visible"
    assert "<think>" not in wire["content"]


def test_glm_recovery_request_disables_thinking():
    provider = GLMProvider(api_key="test", model="glm-5.2", extra={"dialect": "glm"})
    context = LLMRequestContext(
        attempt_number=2,
        recovery=True,
        max_tokens_override=16_384,
        disable_thinking=True,
    )

    kwargs = provider._build_request_kwargs([], [], 0.3, 16_384, "system", request_context=context)

    assert kwargs["extra_body"]["thinking"]["type"] == "disabled"
    assert kwargs["extra_body"]["reasoning_effort"] == "none"
    assert kwargs["max_tokens"] == 16_384


def test_unknown_glm_model_omits_tool_stream_and_reasoning_effort():
    provider = GLMProvider(api_key="test", model="glm-experimental", extra={"dialect": "glm"})

    kwargs = provider._build_request_kwargs([], [{"type": "function"}], 0.3, 4096, "")

    assert "tool_stream" not in kwargs["extra_body"]
    assert "reasoning_effort" not in kwargs["extra_body"]
```

- [ ] **Step 2: Write failing stream and registry tests**

Add synthetic OpenAI-shaped deltas to assert GLM yields `StreamChunk.reasoning_delta` while OpenAI still yields inline `<think>` text. Add registry tests:

```python
def test_builtin_glm_resolves_glm_provider():
    provider = ProviderRegistry().new_instance("glm", api_key="test", model="glm-5.2")
    assert isinstance(provider, GLMProvider)


def test_custom_glm_dialect_resolves_glm_provider():
    registry = ProviderRegistry()
    registry.register_custom_providers(["glm-coding"], dialects={"glm-coding": "glm"})
    provider = registry.new_instance("glm-coding", api_key="test", model="glm-5.2")
    assert isinstance(provider, GLMProvider)
```

- [ ] **Step 3: Verify red**

```bash
uv run pytest tests/providers/test_glm_provider.py tests/providers/test_providers.py -k "glm" -v
```

Expected: FAIL because the provider and registry route do not exist.

- [ ] **Step 4: Implement `GLMProvider` without copying the stream state machine**

Create `GLMProvider(OpenAIProvider)` with:

```python
class GLMProvider(OpenAIProvider):
    def __init__(
        self,
        api_key: str = "",
        api_base: str = "",
        model: str = "glm-5.2",
        provider_name: str = "glm",
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(api_key=api_key, api_base=api_base, model=model)
        self._provider_name = provider_name
        self._glm_config = parse_glm_extra(extra or {"dialect": "glm"}, model)

    @property
    def name(self) -> str:
        return self._provider_name
```

Override `_get_client()` with the custom-base behavior and the `api_key="no-key"` fallback rule from `OpenAICompatProvider`, but keep timeout 120 seconds. Override `_format_messages()` by calling a protected OpenAI formatter helper that accepts `include_reasoning_content=True`; refactor the existing formatter only enough to avoid duplicating its tool-ID repair logic.

Override `_build_request_kwargs()` to put GLM extensions under `extra_body`, and derive `capabilities`/model lists exclusively from GLM metadata.

- [ ] **Step 5: Make OpenAI's stream parser capability-gated**

In `_iter_stream_chunks()`, replace the unconditional OpenAI reasoning block with:

```python
reasoning = getattr(delta, "reasoning_content", None)
if reasoning and self.capabilities.reasoning_content:
    yield StreamChunk(reasoning_delta=reasoning)
elif reasoning:
    if not _in_reasoning:
        yield StreamChunk(text="<think>")
        _in_reasoning = True
    yield StreamChunk(text=reasoning)
```

Close inline tags only when `_in_reasoning` is true. GLM inherits the indexed tool-call implementation unchanged.

- [ ] **Step 6: Route built-in and custom GLM providers**

In `ProviderRegistry`:

```python
"glm": "lucnhan.providers.glm_provider:GLMProvider",
```

Change `register_custom_providers()` to accept optional `dialects: dict[str, str] | None`, track GLM custom names separately from compat names, and select the GLM import spec when dialect is `glm`. In `new_instance()`, pass `provider_name` for custom GLM and compat profiles.

Update `SettingsDialog` and `SessionControllerBase` registration calls to pass:

```python
dialects={
    name: str(config.providers.get(name, {}).get("extra", {}).get("dialect", ""))
    for name in config.custom_providers
}
```

Update `_create_provider()` to pass `extra=copy.deepcopy(self.config.provider.extra)`.

- [ ] **Step 7: Run GLM and OpenAI regression tests green**

```bash
uv run pytest tests/providers/test_glm_provider.py tests/providers/test_providers.py tests/providers/test_openai_provider.py tests/providers/test_provider_streaming.py -v
```

Expected: PASS; OpenAI inline thinking tests retain current output.

- [ ] **Step 8: Commit**

```bash
git add lucnhan/providers/glm_provider.py lucnhan/providers/openai_provider.py lucnhan/providers/registry.py lucnhan/ui/session_controller_base.py tests/providers
git commit -m "feat(providers): add GLM reasoning dialect"
```

---

### Task 7: Implement the bounded GLM reasoning guard

**Files:**
- Create: `lucnhan/agent/glm_guard.py`
- Test: `tests/agent/test_glm_guard.py`

**Interfaces:**
- Consumes: ceiling from `GLMGuardConfig`, exposed tool names.
- Produces: `GLMReasoningGuard`, `GLMGuardSnapshot`, exact bounded detector metrics.

- [ ] **Step 1: Write hard-ceiling boundary tests**

Create `tests/agent/test_glm_guard.py`:

```python
def test_hard_ceiling_flips_at_first_16384_estimated_token_byte():
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])

    guard.on_reasoning_delta("a" * 49_149)
    assert guard.should_abort() is False

    guard.on_reasoning_delta("a")
    assert guard.should_abort() is True
    assert guard.trigger == "reasoning_ceiling"
```

- [ ] **Step 2: Write repetition and false-positive tests**

```python
def test_31_segments_never_trigger_repetition():
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])
    for _ in range(31):
        guard.on_reasoning_delta("outputting read_bytes tool now\n")
    assert guard.should_abort() is False


def test_full_window_requires_39_repeated_positions_and_8_meta_segments():
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])
    repeated = "outputting read_bytes tool now"
    unique = [f"unique analysis line {i}" for i in range(25)]
    lines = [repeated] * 39 + unique

    guard.on_reasoning_delta("\n".join(lines))

    assert guard.should_abort() is True
    assert guard.trigger == "repetition_meta_intent"


def test_visible_answer_disables_only_repetition_path():
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])
    guard.on_visible_delta("v" * 256)
    guard.on_reasoning_delta(("outputting read_bytes tool now\n" * 64))
    assert guard.should_abort() is False

    guard.on_reasoning_delta("x" * 49_150)
    assert guard.should_abort() is True


def test_tool_start_disables_guard():
    guard = GLMReasoningGuard(exposed_tool_names=["read_bytes"])
    guard.on_tool_call_start()
    guard.on_reasoning_delta("x" * 60_000)
    assert guard.should_abort() is False
```

- [ ] **Step 3: Verify red**

```bash
uv run pytest tests/agent/test_glm_guard.py -v
```

Expected: import failure.

- [ ] **Step 4: Implement exact normalization and bounded windows**

Implement:

```python
@dataclass(frozen=True)
class GLMGuardSnapshot:
    estimated_reasoning_tokens: int
    reasoning_utf8_bytes: int
    visible_non_whitespace_chars: int
    total_segments: int
    repeated_positions: int
    meta_intent_segments: int
    repetition_ratio_millis: int
    trigger: str
```

`GLMReasoningGuard` must:

- count UTF-8 bytes incrementally and estimate with `(bytes + 2) // 3`;
- buffer at most one unfinished logical line plus 128 normalized segment strings;
- split long lines into 240-character segments;
- casefold, collapse whitespace, and collapse punctuation runs;
- evaluate `min(64, total_segments)` only after 32 segments;
- count a position as repeated when its fingerprint frequency in the evaluation window is at least two;
- compute threshold with `math.ceil(0.60 * window_size)`;
- identify meta intent only when both an action term and a tool-action/exposed-tool term occur;
- return `T AND (H OR (R AND M AND V))`.

- [ ] **Step 5: Add bounded-state test and run green**

Add:

```python
def test_guard_state_stays_bounded():
    guard = GLMReasoningGuard(exposed_tool_names=[])
    for i in range(10_000):
        guard.on_reasoning_delta(f"unique segment {i}\n")

    assert guard.retained_segment_count <= 128
    assert guard.max_retained_segment_chars <= 240
```

Run:

```bash
uv run pytest tests/agent/test_glm_guard.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/agent/glm_guard.py tests/agent/test_glm_guard.py
git commit -m "feat(agent): detect GLM reasoning degeneration"
```

---

### Task 8: Refactor streamed attempts into typed outcomes

**Files:**
- Modify: `lucnhan/agent/loop.py:656-710,740-1077`
- Modify: `lucnhan/agent/modes/plan.py:52-70`
- Test: `tests/agent/test_agent_loop.py:294-559`
- Test: `tests/agent/test_exploration_loop.py`

**Interfaces:**
- Consumes: `TurnOutcome`, `TurnDisposition`, `AttemptUsage`, `LLMRequestContext`, `GLMReasoningGuard`.
- Produces: `_stream_llm_turn()` and `_stream_llm_turn_inner()` return one `TurnOutcome`; no tuple return remains.

- [ ] **Step 1: Convert one existing finish-reason test to require `TurnOutcome`**

In `tests/agent/test_agent_loop.py`, add a direct-call test:

```python
def test_stream_turn_returns_typed_completed_outcome():
    provider = MockProvider(responses=[_text_response("done")])
    loop = AgentLoop(provider, ToolRegistry(), LucNhanConfig(), SessionState())

    generator = loop._stream_llm_turn("system", None)
    events, outcome = _drain_generator_with_return(generator)

    assert outcome.visible_text == "done"
    assert outcome.disposition == TurnDisposition.COMPLETED
    assert outcome.tool_calls == []
```

Add this local helper near the test fixtures:

```python
def _drain_generator_with_return(generator):
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stopped:
            return events, stopped.value
```

- [ ] **Step 2: Verify red**

```bash
uv run pytest tests/agent/test_agent_loop.py -k "typed_completed_outcome" -v
```

Expected: FAIL because a tuple is returned.

- [ ] **Step 3: Return `TurnOutcome` while preserving existing stream behavior**

Change return annotations and construct an outcome after usage finalization. Implement one `_classify_turn_outcome()` helper with deterministic precedence:

```python
def _classify_turn_outcome(
    *,
    guard_triggered: bool,
    filtered: bool,
    has_open_tool_calls: bool,
    truncated: bool,
    stream_broke: bool,
    tool_calls: list[ToolCall],
    visible_text: str,
) -> TurnDisposition:
```

Apply `DEGENERATED > FILTERED > TRUNCATED_PARTIAL_TOOL_USE > TRUNCATED_TEXT > STREAM_BROKEN > TOOL_USE > COMPLETED`; return `FAILED` only when no usable partial data exists.

Accumulate `chunk.reasoning_delta` separately and yield `TurnEvent.reasoning_event()` without adding it to `assistant_text_parts`.

- [ ] **Step 4: Integrate the guard only for GLM requests with tools**

Create the guard when:

```python
is_glm = self.config.provider.extra.get("dialect") == "glm" or self.provider.name == "glm"
guard_enabled = is_glm and bool(tools_schema) and parsed_glm_config.guard.enabled
```

Feed reasoning/text/tool starts to the guard. When it triggers, close the stream generator, stop consuming generated content, finalize `AttemptUsage` with authoritative or estimated provenance, and return `DEGENERATED`. Do not emit `RECOVERY_START` here; transaction events belong to `execute_single_turn()`.

- [ ] **Step 5: Migrate the direct plan caller**

In `_generate_plan_text()` replace tuple destructuring with:

```python
outcome = yield from loop._stream_llm_turn(system_prompt, None)
plan_text = outcome.visible_text
usage = outcome.usage
```

No tool schema means no guard. Update plan/exploration tests to assert unchanged visible behavior.

- [ ] **Step 6: Run stream regression tests green**

```bash
uv run pytest tests/agent/test_agent_loop.py tests/agent/test_exploration_loop.py -v
```

Expected: PASS, including all existing finish-reason and broken-stream cases.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/agent/loop.py lucnhan/agent/modes/plan.py tests/agent/test_agent_loop.py tests/agent/test_exploration_loop.py
git commit -m "refactor(agent): return typed stream outcomes"
```

---

### Task 9: Reject incomplete GLM tool calls with a safe persisted prefix

**Files:**
- Modify: `lucnhan/agent/loop.py:880-1037`
- Modify: `lucnhan/agent/turn.py`
- Test: `tests/agent/test_agent_loop.py`
- Test: `tests/providers/test_glm_provider.py`

**Interfaces:**
- Consumes: GLM dialect, `TOOL_CALL_DISCARDED`, `TRUNCATED_PARTIAL_TOOL_USE`.
- Produces: contiguous `safe_tool_calls`; malformed/incomplete GLM calls never become `{}`.

- [ ] **Step 1: Write a failing truncated parallel-call test**

Add to `tests/agent/test_agent_loop.py` a GLM-capable mock stream:

```python
def test_glm_length_cutoff_keeps_only_safe_completed_tool_prefix():
    chunks = [
        StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_start=True),
        StreamChunk(tool_call_id="call_1", tool_name="read_bytes", tool_args_delta='{"address":4096}'),
        StreamChunk(tool_call_id="call_1", tool_name="read_bytes", is_tool_call_end=True),
        StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", is_tool_call_start=True),
        StreamChunk(tool_call_id="call_2", tool_name="get_pseudocode", tool_args_delta='{"address":'),
        StreamChunk(finish_reason="length"),
    ]

    outcome, events = run_glm_stream(chunks)

    assert outcome.disposition == TurnDisposition.TRUNCATED_PARTIAL_TOOL_USE
    assert [call.id for call in outcome.tool_calls] == ["call_1"]
    assert any(event.type == TurnEventType.TOOL_CALL_DISCARDED and event.tool_call_id == "call_2" for event in events)
```

Add another case where the first call is incomplete and a later call completes; expected safe list is empty and both are discarded.

- [ ] **Step 2: Verify red**

```bash
uv run pytest tests/agent/test_agent_loop.py -k "safe_completed_tool_prefix" -v
```

Expected: FAIL because malformed arguments currently fall back to `{}` and execute.

- [ ] **Step 3: Track start/completion order and parse validity**

Maintain ordered call states with:

```python
@dataclass
class _StreamToolState:
    id: str
    name: str
    raw_args: list[str] = field(default_factory=list)
    started: bool = False
    ended: bool = False
    parsed_arguments: dict[str, Any] | None = None
    parse_error: str = ""
```

For GLM, JSON parse failure sets `parse_error` and does not append `ToolCall`. At finish, find the first state that is not ended/parsed. Persist only the contiguous valid prefix before it; emit `TOOL_CALL_DISCARDED` for that state and all later states.

For non-GLM, retain the current warning plus `{}` fallback unchanged.

- [ ] **Step 4: Verify persisted call/result pairing through a full turn**

Add a test that runs `execute_single_turn()` with one safe call plus one incomplete call and asserts:

```python
assistant = session.messages[-2]
tool_message = session.messages[-1]
assert [call.id for call in assistant.tool_calls] == ["call_1"]
assert [result.tool_call_id for result in tool_message.tool_results] == ["call_1"]
```

- [ ] **Step 5: Run agent and GLM provider tests green**

```bash
uv run pytest tests/agent/test_agent_loop.py tests/providers/test_glm_provider.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lucnhan/agent/loop.py lucnhan/agent/turn.py tests/agent/test_agent_loop.py tests/providers/test_glm_provider.py
git commit -m "fix(agent): reject incomplete GLM tool calls"
```

---

### Task 10: Implement the one-shot recovery transaction and usage accounting

**Files:**
- Modify: `lucnhan/agent/modes/turn_helpers.py:18-99`
- Modify: `lucnhan/state/session.py:109-125`
- Modify: `lucnhan/agent/loop.py:656-710`
- Create: `tests/agent/test_glm_recovery.py`
- Modify: `tests/agent/test_agent_loop.py`

**Interfaces:**
- Consumes: `TurnOutcome`, `AttemptUsage`, `LLMRequestContext`, GLM recovery config, request suffix.
- Produces: extended `TurnResult`, `SessionState.record_usage()`, exactly one logical recovery.

- [ ] **Step 1: Write a failing successful-recovery test**

Create `tests/agent/test_glm_recovery.py` with a scripted GLM mock whose first response triggers the guard and whose second response emits a valid call:

```python
def test_degenerated_attempt_retries_once_without_persisting_reasoning():
    provider = ScriptedGLMProvider(
        responses=[
            repeated_reasoning_response(),
            complete_tool_call_response("call_1", "read_bytes", {"address": 4096}),
        ]
    )
    session = SessionState()
    loop = make_glm_loop(provider, session)

    events = list(loop.run("Inspect the bytes"))

    assert provider.logical_attempt_contexts[0].attempt_number == 1
    assert provider.logical_attempt_contexts[1].attempt_number == 2
    assert provider.logical_attempt_contexts[1].disable_thinking is True
    assert provider.logical_attempt_contexts[1].max_tokens_override == 16_384
    assert all("outputting" not in message.reasoning_content for message in session.messages)
    assert sum(event.type == TurnEventType.RECOVERY_START for event in events) == 1
```

- [ ] **Step 2: Write failure, cancellation, and usage tests**

Add tests:

```python
def test_second_degeneration_fails_without_third_attempt():
    provider = ScriptedGLMProvider(responses=[repeated_reasoning_response(), repeated_reasoning_response()])
    result = run_single_glm_turn(provider)

    assert provider.call_count == 2
    assert result.recovery_attempted is True
    assert result.recovery_failed is True
    assert result.disposition == "recovery_failed"


def test_cancellation_between_attempts_prevents_recovery_dispatch():
    loop = make_loop_that_cancels_on_recovery_boundary()

    events = list(loop.run("Inspect"))

    assert any(event.type == TurnEventType.CANCELLED for event in events)
    assert loop.provider.call_count == 1


def test_discarded_usage_is_estimated_without_terminal_usage_and_not_double_counted():
    session, result = run_recovered_turn_without_first_usage_chunk()

    assert result.attempt_usages[0].provenance == "estimated"
    assert result.attempt_usages[1].provenance == "authoritative"
    expected_total = sum(item.usage.total_tokens for item in result.attempt_usages)
    assert session.total_usage.total_tokens == expected_total
```

- [ ] **Step 3: Verify red**

```bash
uv run pytest tests/agent/test_glm_recovery.py -v
```

Expected: FAIL because no recovery transaction exists.

- [ ] **Step 4: Add `SessionState.record_usage()` without double counting**

In `SessionState`:

```python
def record_usage(self, usage: TokenUsage) -> None:
    with self._lock:
        self.total_usage.prompt_tokens += usage.prompt_tokens
        self.total_usage.completion_tokens += usage.completion_tokens
        self.total_usage.total_tokens += usage.total_tokens
        self.total_usage.cache_read_tokens += usage.cache_read_tokens
        self.total_usage.cache_creation_tokens += usage.cache_creation_tokens
        if usage.prompt_tokens > 0:
            self.last_prompt_tokens = usage.context_tokens
```

Refactor `add_message()` to call `record_usage()` after releasing or while safely using the existing reentrant lock. Only call `record_usage()` directly for discarded attempts; successful persisted messages still account through `add_message()`.

- [ ] **Step 5: Extend `TurnResult` and preserve the durable commit boundary**

Add:

```python
finish_reason: str | None = None
disposition: TurnDisposition | str | None = None
recovery_attempted: bool = False
recovery_failed: bool = False
attempt_usages: list[AttemptUsage] = field(default_factory=list)
```

Capture pre-turn message IDs at function entry. Call `_stream_llm_turn()` with attempt 1 context. If `DEGENERATED`, record its non-persisted usage, yield `RECOVERY_START`, assert history IDs/length unchanged, check cancellation, build attempt 2 context with the request-local suffix, and call `_stream_llm_turn()` once more.

Each logical attempt continues through `_stream_llm_turn()` so existing transport retries remain internal. Degeneration is a returned outcome, never a provider exception.

Persist only accepted attempt output. If recovery fails, emit one compact error and return `recovery_failed=True` without a generated assistant body.

- [ ] **Step 6: Run recovery and mode-consumer tests green**

```bash
uv run pytest tests/agent/test_glm_recovery.py tests/agent/test_agent_loop.py tests/agent/test_exploration_loop.py -v
```

Expected: PASS; normal/research/exploration/plan consumers continue reading existing `TurnResult.text`, `.tool_calls`, and `.usage` fields.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/agent/modes/turn_helpers.py lucnhan/state/session.py lucnhan/agent/loop.py tests/agent/test_glm_recovery.py tests/agent/test_agent_loop.py
git commit -m "feat(agent): recover GLM reasoning loops once"
```

---

### Task 11: Replace prose-oriented parallel-call prompting

**Files:**
- Modify: `lucnhan/agent/prompts/base.py:35-47`
- Test: `tests/agent/test_system_prompt.py`
- Test: `tests/agent/test_bulk_renamer_prompts.py`

**Interfaces:**
- Consumes: existing prompt composition.
- Produces: protocol-oriented batching instruction without `ALWAYS batch` meta-pressure.

- [ ] **Step 1: Write a failing prompt assertion**

Add to `tests/agent/test_system_prompt.py`:

```python
def test_parallel_tool_prompt_requires_structured_calls_without_rehearsal():
    prompt = build_system_prompt()

    assert "ALWAYS batch independent tool calls" not in prompt
    assert "structured tool calls" in prompt
    assert "Never describe, simulate, or rehearse tool calls in prose" in prompt
    assert "If no tool is needed, answer directly" in prompt
```

- [ ] **Step 2: Verify red**

```bash
uv run pytest tests/agent/test_system_prompt.py -k "parallel_tool_prompt" -v
```

Expected: FAIL on old wording.

- [ ] **Step 3: Replace only `PARALLEL_BATCHING_SECTION`**

Use exactly:

```python
PARALLEL_BATCHING_SECTION = """\
## Parallel Tool Calls

When multiple independent structured tool calls are needed, prefer emitting
them together in the same assistant turn. Never describe, simulate, or
rehearse tool calls in prose. If no tool is needed, answer directly.
"""
```

- [ ] **Step 4: Run prompt tests green**

```bash
uv run pytest tests/agent/test_system_prompt.py tests/agent/test_bulk_renamer_prompts.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lucnhan/agent/prompts/base.py tests/agent/test_system_prompt.py tests/agent/test_bulk_renamer_prompts.py
git commit -m "fix(prompts): require structured tool invocation"
```

---

### Task 12: Render transient reasoning and recovery safely in the UI

**Files:**
- Modify: `lucnhan/agent/loop.py:2394-2460`
- Modify: `lucnhan/ui/chat_view.py:86-130,290-359,752-922,1365-1413,2093-2154`
- Modify: `lucnhan/ui/tool_widgets.py:695-725`
- Create: `tests/ui/test_chat_view_glm.py`
- Modify: `tests/ui/test_chat_view_restore.py`
- Modify: `tests/agent/test_agent_loop.py`

**Interfaces:**
- Consumes: `REASONING_DELTA`, `RECOVERY_START`, `TOOL_CALL_DISCARDED`, `Message.reasoning_content`.
- Produces: transient `_ThinkingBlock`, deterministic queue boundaries, discarded tool terminal state, reasoning-aware restore.

- [ ] **Step 1: Write failing runner ordering tests**

Add to `tests/agent/test_agent_loop.py` a scripted runner sequence and assert dequeue order:

```python
def test_background_runner_keeps_reasoning_and_text_buffers_separate():
    events = [
        TurnEvent.reasoning_event("r1"),
        TurnEvent.reasoning_event("r2"),
        TurnEvent.text_delta("v1"),
        TurnEvent.recovery_start(
            attempt=2,
            reason="reasoning_degenerated",
            discard_transient_reasoning=True,
        ),
    ]

    emitted = run_background_event_sequence(events)

    assert [event.type for event in emitted] == [
        TurnEventType.REASONING_DELTA,
        TurnEventType.TEXT_DELTA,
        TurnEventType.RECOVERY_START,
    ]
    assert emitted[0].reasoning == "r1r2"
```

Add an exception/finalizer case proving pending reasoning is flushed or explicitly discarded.

- [ ] **Step 2: Write failing ChatView and restore tests**

Create `tests/ui/test_chat_view_glm.py`:

```python
def test_reasoning_delta_uses_one_transient_thinking_block(chat_view):
    chat_view.handle_event(TurnEvent.reasoning_event("first"))
    first_block = chat_view._message_thinking
    chat_view.handle_event(TurnEvent.reasoning_event(" second"))

    assert chat_view._message_thinking is first_block
    assert first_block._source_text == "first second"


def test_recovery_start_clears_transient_reasoning_once(chat_view):
    chat_view.handle_event(TurnEvent.reasoning_event("discard me"))
    chat_view.handle_event(
        TurnEvent.recovery_start(
            attempt=2,
            reason="reasoning_degenerated",
            discard_transient_reasoning=True,
        )
    )

    assert chat_view._message_thinking is None
    status_widgets = []
    for index in range(chat_view._layout.count()):
        item = chat_view._layout.itemAt(index)
        widget = item.widget() if item is not None else None
        if widget is not None and widget.objectName() == "recovery_status":
            status_widgets.append(widget)
    assert len(status_widgets) == 1


def test_restore_prefers_reasoning_field_and_preserves_legacy_tags(chat_view):
    new_message = Message(role=Role.ASSISTANT, content="answer", reasoning_content="reason")
    legacy = Message(role=Role.ASSISTANT, content="<think>old</think>legacy answer")

    new_spec, _ = RestoreWorker._build_spec(new_message, 0, None)
    legacy_spec, _ = RestoreWorker._build_spec(legacy, 1, None)

    assert new_spec.reasoning_content == "reason"
    assert legacy_spec.reasoning_content == ""
```

- [ ] **Step 3: Verify red**

```bash
uv run pytest tests/ui/test_chat_view_glm.py tests/ui/test_chat_view_restore.py tests/agent/test_agent_loop.py -k "reasoning or recovery_start or discarded" -v
```

Expected: FAIL because the new event branches/buffers/spec field do not exist.

- [ ] **Step 4: Add symmetric runner coalescing**

In `BackgroundAgentRunner._run`, maintain one pending event type and one text buffer rather than allowing cross-type coalescing. Flush before any other event type and in exception/finally paths. Construct the flush event with `TurnEvent.text_delta()` or `TurnEvent.reasoning_event()` based on pending type. `RECOVERY_START` remains a non-delta hard boundary.

- [ ] **Step 5: Add explicit ChatView event branches**

Route `REASONING_DELTA` to a helper that appends directly to `_message_thinking`, without `_split_thinking()`. Route `RECOVERY_START` to remove the transient block, close spinner state, and insert one compact status label. Do not create/update `_current_assistant` for reasoning.

Add `ToolCallWidget.mark_discarded(reason: str)` to stop its spinner, set a neutral/warning terminal glyph, and show a short status without result content. Route `TOOL_CALL_DISCARDED` to it.

- [ ] **Step 6: Add reasoning-aware sync and async restore**

Add `reasoning_content: str = ""` to `MessageSpec`. Populate it in `RestoreWorker._build_spec()`. Pre-render only visible `content`. In both `_render_restored_messages()` and `_build_widgets_from_spec()`, render `reasoning_content` first when present; otherwise use legacy `_split_thinking(content)`.

- [ ] **Step 7: Run UI and runner tests green**

```bash
uv run pytest tests/ui/test_chat_view_glm.py tests/ui/test_chat_view_restore.py tests/ui/test_restore_cap.py tests/ui/test_restore_worker_html.py tests/agent/test_agent_loop.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add lucnhan/agent/loop.py lucnhan/ui/chat_view.py lucnhan/ui/tool_widgets.py tests/ui/test_chat_view_glm.py tests/ui/test_chat_view_restore.py tests/agent/test_agent_loop.py
git commit -m "feat(ui): separate transient GLM reasoning"
```

---

### Task 13: Add GLM Settings controls and explicit migration

**Files:**
- Modify: `lucnhan/ui/settings_dialog.py:207-560,1003-1035,1284-1412`
- Test: `tests/tools/test_settings_dialog.py`
- Test: `lucnhan/tests/test_settings_dialog_fixes.py`

**Interfaces:**
- Consumes: `parse_glm_extra()`, GLM model metadata, deep-copy `extra` behavior.
- Produces: GLM-only controls and one-time opt-in migration for `api.z.ai` custom profiles.

- [ ] **Step 1: Write failing visibility and persistence tests**

Add tests using Qt stubs:

```python
def test_glm_controls_visible_only_for_glm_dialect(settings_dialog):
    settings_dialog._config.provider.extra = {"dialect": "glm"}
    settings_dialog._refresh_glm_controls()
    assert settings_dialog._glm_group.isVisible() is True

    settings_dialog._config.provider.extra = {}
    settings_dialog._refresh_glm_controls()
    assert settings_dialog._glm_group.isVisible() is False


def test_glm_controls_persist_exact_extra_schema(settings_dialog):
    settings_dialog._config.provider.extra = {"dialect": "glm"}
    settings_dialog._glm_thinking_combo.setCurrentText("Disabled")
    settings_dialog._glm_effort_combo.setCurrentData("none")
    settings_dialog._glm_preserve_cb.setChecked(True)
    settings_dialog._glm_guard_cb.setChecked(True)
    settings_dialog._glm_ceiling_spin.setValue(16_384)
    settings_dialog._glm_recovery_spin.setValue(16_384)

    settings_dialog._sync_config_from_ui()

    assert settings_dialog._config.provider.extra == {
        "dialect": "glm",
        "thinking": {"enabled": False, "reasoning_effort": "none", "preserve": True},
        "degeneration_guard": {
            "enabled": True,
            "reasoning_token_ceiling": 16_384,
            "retry_without_thinking": True,
            "recovery_max_tokens": 16_384,
        },
    }
```

- [ ] **Step 2: Write migration accept/decline tests**

Patch the prompt helper and assert a custom `api.z.ai` provider receives `dialect="glm"` only on accept. Decline must leave `extra` untouched and continue to register as OpenAI-compatible.

- [ ] **Step 3: Verify red**

```bash
uv run pytest tests/tools/test_settings_dialog.py lucnhan/tests/test_settings_dialog_fixes.py -k "glm" -v
```

Expected: FAIL because controls/migration do not exist.

- [ ] **Step 4: Build one GLM group with product-level controls only**

Add `_build_glm_group()` containing:

- Adaptive/Disabled combo;
- effort combo with provider mappings in tooltips;
- preserve checkbox;
- guard checkbox;
- ceiling spinbox range 1,024–65,536;
- recovery cap range 1,024–131,072, clamped to selected model max.

Do not expose repetition/window/meta thresholds.

Add `_load_glm_controls_from_config()`, `_sync_glm_controls_to_config()`, and `_refresh_glm_controls()`. Invoke them on initial build, provider changes, model changes, `_sync_config_from_ui()`, and `_on_accept()`.

- [ ] **Step 5: Add one-time explicit Z.AI migration**

Parse `urlparse(api_base).hostname`. Prompt only when host is exactly `api.z.ai`, provider is custom/generic compat, and no dialect is saved. Save a marker under `custom_providers[name]["glm_migration_prompted"]` so decline is durable. Accept sets active/saved `extra.dialect="glm"` and re-registers provider dialects; decline changes no provider behavior.

- [ ] **Step 6: Run settings tests green**

```bash
uv run pytest tests/tools/test_settings_dialog.py lucnhan/tests/test_settings_dialog_fixes.py tests/headless/test_provider_config.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add lucnhan/ui/settings_dialog.py tests/tools/test_settings_dialog.py lucnhan/tests/test_settings_dialog_fixes.py
git commit -m "feat(settings): configure GLM reasoning resilience"
```

---

### Task 14: Emit telemetry and lock headless/control semantics

**Files:**
- Modify: `lucnhan/agent/modes/turn_helpers.py`
- Modify: `lucnhan/headless/runner.py:109-154`
- Modify: `lucnhan/control/server.py:155-249`
- Modify: `lucnhan/agent/context_window.py:41-86`
- Test: `tests/core/test_logging.py`
- Test: `tests/headless/test_runner.py`
- Test: `tests/control/test_server.py`
- Test: `tests/agent/test_minify.py`

**Interfaces:**
- Consumes: `log_structured()`, `TurnOutcome`, `AttemptUsage`, new events.
- Produces: one safe record per logical attempt; reasoning/recovery pass through JSON events without changing terminal results; compaction omits old reasoning.

- [ ] **Step 1: Write a failing complete telemetry-record test**

Add a capture handler around `log_structured()` and drive one recovered turn. Assert two records, one per logical attempt:

```python
def test_recovered_turn_logs_two_content_free_attempt_records():
    records = capture_recovered_turn_records()

    assert [record["attempt_number"] for record in records] == [1, 2]
    assert records[0]["discarded_attempt"] is True
    assert records[0]["usage_provenance"] == "estimated"
    assert records[1]["recovery_result"] == "success"
    forbidden = {"content", "reasoning_content", "tool_args", "tool_results", "messages", "error"}
    assert not any(forbidden & set(record) for record in records)
```

- [ ] **Step 2: Write headless/control pass-through tests**

Add to `tests/headless/test_runner.py` and `tests/control/test_server.py`:

```python
def test_reasoning_and_recovery_events_do_not_change_final_text_or_exit_code():
    result = run_with_events(
        [
            TurnEvent.reasoning_event("hidden"),
            TurnEvent.recovery_start(
                attempt=2,
                reason="reasoning_degenerated",
                discard_transient_reasoning=True,
            ),
            TurnEvent.text_done("visible"),
        ],
        json_events=True,
    )

    assert result.final_text == "visible"
    assert result.exit_code == EXIT_SUCCESS
    assert [event["type"] for event in result.events][:2] == ["reasoning_delta", "recovery_start"]
```

Control server must show the same events in `event_buffer` while `run.final_text` stays visible-only.

- [ ] **Step 3: Verify red**

```bash
uv run pytest tests/core/test_logging.py tests/headless/test_runner.py tests/control/test_server.py -k "attempt or reasoning or recovery" -v
```

Expected: FAIL because attempt logging and explicit event semantics are missing.

- [ ] **Step 4: Emit one allowlisted record per attempt**

In `execute_single_turn()`, call a helper after every outcome is finalized. Populate only allowlisted scalars, use integer thousandths for repetition ratio, and set `discarded_attempt`/`usage_provenance` accurately. Do not pass provider exceptions or message text to telemetry.

- [ ] **Step 5: Make pass-through branches explicit**

In headless/control event handling, add explicit branches for `REASONING_DELTA`, `RECOVERY_START`, and `TOOL_CALL_DISCARDED` that perform no `final_text`, error, status, or exit-code mutation. Events still enter JSON buffers through existing `to_dict()` calls.

- [ ] **Step 6: Make compaction's reasoning rule explicit**

In `ContextWindowManager.compact_messages()`, old messages that are summarized lose `reasoning_content` because only visible `content` enters the summary source. Tail messages remain untouched, preserving unresolved tool call/result pairs and recent GLM reasoning. Add a test that compacted summary text does not contain old reasoning and the last four messages retain it.

- [ ] **Step 7: Run telemetry/headless/control/compaction tests green**

```bash
uv run pytest tests/core/test_logging.py tests/headless/test_runner.py tests/control/test_server.py tests/agent/test_minify.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add lucnhan/agent/modes/turn_helpers.py lucnhan/headless/runner.py lucnhan/control/server.py lucnhan/agent/context_window.py tests/core/test_logging.py tests/headless/test_runner.py tests/control/test_server.py tests/agent/test_minify.py
git commit -m "feat(agent): record GLM recovery telemetry"
```

---

### Task 15: Run cross-provider, mode, UI, and static quality gates

**Files:**
- Modify only files required to fix regressions introduced by Tasks 1–14.
- Do not modify the two out-of-scope memory/orchestra documents.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: verified implementation with unchanged non-GLM behavior.

- [ ] **Step 1: Run focused provider and agent suites**

```bash
uv run pytest tests/providers tests/agent -v
```

Expected: PASS. If a non-GLM payload assertion fails, fix the implementation rather than weakening the assertion.

- [ ] **Step 2: Run UI, headless, control, and config suites**

```bash
uv run pytest tests/ui tests/tools/test_settings_dialog.py tests/headless tests/control lucnhan/tests/test_settings_dialog_fixes.py -v
```

Expected: PASS.

- [ ] **Step 3: Run all project tests**

```bash
uv run pytest tests lucnhan/tests -v
```

Expected: PASS with zero failures/errors.

- [ ] **Step 4: Format and lint only changed Python files**

Get the changed file list and run:

```bash
changed_py=$(git diff --name-only HEAD~14 -- '*.py')
uv run ruff format $changed_py
uv run ruff check $changed_py
```

On PowerShell use:

```powershell
$changedPy = git diff --name-only HEAD~14 -- '*.py'
uv run ruff format $changedPy
uv run ruff check $changedPy
```

Expected: no lint errors. Do not run `--fix` across the whole repository.

- [ ] **Step 5: Run strict type checks on affected packages**

```bash
uv run mypy lucnhan/core lucnhan/providers lucnhan/agent
```

Expected: no type errors.

- [ ] **Step 6: Verify scope and secrets**

```bash
git status --short
git diff --check
git diff --name-only HEAD~14
```

Expected:

- the two pre-existing memory/orchestra docs remain untracked and unstaged;
- no API keys, raw reasoning fixtures from production, authorization headers, or user-specific paths are added;
- only GLM resilience implementation/tests/docs appear in the branch diff.

- [ ] **Step 7: Run the required post-change reviews**

Invoke:

- `code-reviewer` for all changes;
- `python-reviewer` for Python changes;
- `ida-tooling-reviewer` only if any file under `lucnhan/tools/`, `lucnhan/ida/tools/`, or `lucnhan/agent/mutation.py` was unexpectedly changed.

Apply only verified findings, rerun the affected focused tests, then rerun Steps 3–6.

- [ ] **Step 8: Commit final integration fixes**

```bash
git add lucnhan tests lucnhan/tests
git commit -m "test: verify GLM reasoning resilience"
```

If there are no integration fixes after prior task commits, do not create an empty commit.

---

## Final Verification Checklist

- [ ] The incident fixture aborts on the first chunk where `ceil(reasoning_utf8_bytes / 3) >= 16_384`.
- [ ] The repetition path cannot trigger before 32 segments and requires the documented repeated-position/meta-intent thresholds.
- [ ] A tool-call start permanently disables the guard for that attempt.
- [ ] Recovery occurs exactly once with thinking disabled and the effective 16K/model cap.
- [ ] Existing transport retries stay inside each logical attempt.
- [ ] Discarded reasoning is absent from `SessionState.messages`, checkpoints, visible assistant content, and future provider context.
- [ ] Discarded usage is accounted with `estimated` or `authoritative` provenance and not double-counted.
- [ ] GLM `reasoning_content` is sanitized on load and round-tripped separately from visible content.
- [ ] Legacy/OpenAI inline `<think>` behavior remains unchanged.
- [ ] Incomplete GLM calls never become `{}`; persisted calls and tool results remain one-to-one.
- [ ] Started-but-omitted tool widgets receive `TOOL_CALL_DISCARDED` and terminate without approval/result.
- [ ] Headless/control JSON events expose reasoning/recovery but final text and exit status remain visible-result-only.
- [ ] Structured telemetry accepts only the fixed scalar allowlist.
- [ ] All provider, agent, UI, headless, control, config, and restore tests pass.
- [ ] Ruff, mypy, `git diff --check`, and scope checks pass.

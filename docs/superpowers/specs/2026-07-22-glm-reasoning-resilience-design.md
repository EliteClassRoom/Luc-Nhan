# GLM Reasoning Resilience Design

**Date:** 2026-07-22
**Status:** Draft for user review
**Scope:** GLM-5.x provider protocol, reasoning-loop protection, and one-shot recovery

## 1. Purpose

This change prevents GLM-5.x agent turns from consuming the entire output budget while repeatedly describing intended tool calls without emitting structured `tool_calls`. It also makes GLM thinking history conform to Z.AI's preserved/interleaved-thinking protocol.

The implementation is deliberately GLM-specific. Existing OpenAI, generic OpenAI-compatible, Anthropic, MiniMax, Gemini, Codex, and Ollama wire behavior must remain unchanged.

`GLMProvider` inherits `OpenAIProvider`, not `OpenAICompatProvider`, because it needs the former's transport/parser internals but must replace model metadata, provider identity, and custom-base client behavior explicitly. Its constructor accepts `api_base`, uses the same custom client construction contract as `OpenAICompatProvider`, and reports provider name `glm` or the configured custom profile name. It never exposes OpenAI's curated GPT model list.

## 2. Confirmed incident

A production session using custom provider `glm-coding` and model `glm-5.2` produced one assistant response with:

- configured `max_tokens`: 81,920;
- completion tokens: exactly 81,920;
- 81,916 stream chunks;
- 273,925 rendered characters;
- zero structured tool calls;
- one opening `<think>` marker and no closing marker;
- 589 occurrences of `outputting`;
- 480 occurrences of `parallel calls` or `parallel tool`.

The response repeatedly stated that it was about to call `read_bytes` and `get_pseudocode`, but the upstream stream never emitted a valid `delta.tool_calls` entry. The six tool calls and six tool results immediately preceding the incident had matching IDs, which excludes an orphaned-result failure for this turn.

The direct failure is model-side reasoning degeneration until `finish_reason=length`. Luc Nhan contributes to the risk because it currently:

1. routes GLM through the generic OpenAI-compatible adapter;
2. folds `delta.reasoning_content` into visible `Message.content` using literal `<think>` tags;
3. does not replay GLM `reasoning_content` as a separate assistant-message field;
4. sends no GLM-specific `thinking`, `reasoning_effort`, or `tool_stream` controls;
5. has no repetition or reasoning-budget circuit breaker;
6. treats a no-tool response as a normal final turn even when the response is truncated reasoning;
7. advises increasing `max_tokens` for every length cutoff, including degenerated reasoning.

## 3. Goals

1. Preserve GLM reasoning separately from visible assistant content.
2. Round-trip GLM reasoning and tool calls according to Z.AI's preserved/interleaved-thinking protocol.
3. Stop a degenerated tool-use turn before it can consume the full configured output budget.
4. Retry exactly once from the unchanged pre-turn history with thinking disabled.
5. Never persist the discarded attempt's generated content or feed it into recovery context.
6. Account for successful attempts exactly and represent discarded-attempt usage with an explicit authoritative-or-estimated provenance.
7. Never execute an incomplete or malformed GLM streamed tool call with empty fallback arguments.
8. Keep reasoning visible in the UI through a dedicated event/channel.
9. Leave non-GLM provider request payloads and behavior unchanged.
10. Produce enough content-free telemetry to distinguish degeneration, truncation, parser failure, and transport failure.

## 4. Non-goals

This tranche does not:

- create a universal reasoning dialect for all OpenAI-compatible models;
- automatically classify every custom endpoint by model name;
- implement autonomous multi-retry or escalating retry chains;
- expose detector internals such as n-gram thresholds in Settings;
- change tool execution from sequential to concurrent;
- rewrite old checkpoint content containing literal `<think>` tags;
- add automatic retry for ordinary truncated final answers;
- alter existing approval, mutation, undo, or tool-policy behavior.

## 5. Chosen approach

Introduce a dedicated `GLMProvider` built on the existing OpenAI Chat Completions transport, plus provider-neutral reasoning fields and turn outcomes. Add a GLM-only degeneration guard and a one-shot non-thinking recovery transaction.

This approach is preferred over a regex-only hotfix because it fixes both the protocol mismatch and the runtime symptom. It is preferred over a universal reasoning framework because only GLM behavior has been reproduced and verified, and broad provider abstraction would increase regression risk without current evidence.

The internal boundaries should remain narrow enough to extract a generic reasoning dialect later without rewriting the GLM implementation.

## 6. Architecture

### 6.1 Dedicated GLM provider

Create `lucnhan/providers/glm_provider.py` with `GLMProvider(OpenAIProvider)`.

It reuses:

- OpenAI SDK client construction;
- streaming tool-call accumulation by index;
- usage parsing;
- cancellation/watchdog behavior;
- OpenAI-style error normalization.

It overrides only GLM-specific behavior:

```text
GLMProvider
├── _get_client()             custom Z.AI-compatible base URL
├── _build_request_kwargs()   GLM thinking/tool-stream request fields
├── _format_messages()        reasoning_content round-trip
├── _normalize_response()     separate reasoning from visible content
└── capabilities/models       model-specific GLM limits and feature flags
```

The existing OpenAI streaming state machine remains the single implementation for indexed tool-call assembly. `OpenAIProvider._iter_stream_chunks()` branches only at reasoning emission: providers with `capabilities.reasoning_content=True` yield `StreamChunk.reasoning_delta`, while OpenAI retains its legacy inline-`<think>` text behavior. `GLMProvider` therefore does not copy or override the full stream parser.

Provider selection rules:

1. a built-in `glm` provider name selects `GLMProvider`;
2. a custom provider with `provider.extra.dialect == "glm"` selects `GLMProvider`;
3. an `api.z.ai` endpoint without the dialect may produce a migration hint but must not silently change provider behavior.

The existing `glm-coding` configuration is migrated explicitly to `dialect: "glm"`.

### 6.2 Provider-neutral message representation

Extend `Message` with:

```python
reasoning_content: str = ""
```

Semantics:

- `content` contains only user-visible assistant text;
- `reasoning_content` contains the provider-emitted GLM reasoning sequence;
- `_raw_parts` remains the in-memory escape hatch for provider-native blocks or signatures that cannot be represented generically.

`reasoning_content` is serialized to checkpoints. Old checkpoints remain valid because the field defaults to an empty string when absent. GLM needs no serialized provider metadata beyond reasoning, standard tool calls, and ordinary message fields in this tranche; request-time settings such as `reasoning_effort`, `clear_thinking`, and `tool_stream` belong to provider config, not to each message. `_raw_parts` remains `None` for GLM.

On checkpoint load, `reasoning_content` passes through `_safe_persisted_text`, including lone-surrogate and injection-marker stripping, before it can re-enter provider context. The resulting sanitized string is then the canonical stored/replayed sequence; runtime provider output is otherwise not normalized, reordered, independently truncated, or reconstructed from UI tags.

No automatic migration parses literal `<think>` tags from old `content`; doing so could reinterpret user-visible text or data that legitimately contains those tags. Restored legacy messages continue to render through the existing inline-tag UI path and are replayed as plain `content` exactly as before. Only newly generated GLM messages use the separate field. Existing OpenAI o-series behavior remains unchanged in this tranche; `OpenAIProvider` continues its current inline-tag convention so the GLM change cannot silently reclassify old or mixed-provider history.

### 6.3 Internal stream representation

Extend `StreamChunk` with a dedicated optional `reasoning_delta` field. Existing `text` retains visible-content semantics and existing `finish_reason` remains the per-chunk terminal carrier.

Add `TurnEventType.REASONING_DELTA`, a dedicated `TurnEvent.reasoning: str = ""` payload field, and a `TurnEvent.reasoning_event(text)` constructor. Add `TurnEventType.RECOVERY_START` with recovery metadata in the existing `metadata` field. Add `TurnEventType.TOOL_CALL_DISCARDED` carrying the existing tool-call ID/name plus `metadata.reason`, so a started but unsafe GLM call can leave the UI in a terminal discarded state without entering persisted history. Only the GLM path emits these new events in this tranche. Existing providers retain their current event streams.

UI contract:

- `BackgroundAgentRunner` maintains separate pending text and pending reasoning strings; it coalesces `REASONING_DELTA` only with adjacent `REASONING_DELTA` events and `TEXT_DELTA` only with adjacent `TEXT_DELTA` events;
- before enqueueing any event of the other delta type or any non-delta event, it flushes the currently pending buffer; `RECOVERY_START` therefore forms a hard boundary without an additional queue primitive;
- `ChatView` reuses the existing `ThinkingBlock` widget class as a **transient, turn-scoped widget**, but feeds it directly from a dedicated reasoning buffer rather than parsing it out of visible text;
- `RECOVERY_START(discard_transient_reasoning=True)` removes that transient `ThinkingBlock`, then inserts exactly one compact retry-status widget;
- `TEXT_DELTA` creates or updates the visible assistant widget only;
- history rendering creates a normal persisted thinking block from `Message.reasoning_content` first and falls back to the legacy inline-`<think>` parser when that field is empty;
- `TurnEvent.to_dict()` serializes the event type plus reasoning in a `reasoning` field and recovery metadata in `metadata`; headless and control-server consumers expose these events distinctly;
- local A2A widgets that receive `TurnEvent` may ignore reasoning without treating it as visible output; the subprocess A2A bridge emits its own `A2AEvent` stdout/completion protocol and is not required to forward `REASONING_DELTA` in this tranche.

The stream accumulator tracks separately:

- reasoning text and character count;
- visible text and character count;
- structured tool-call starts and completed calls;
- finish reason;
- usage;
- guard metrics.

### 6.4 Attempt result and user-visible turn result

`TurnOutcome` is the result of **one provider request attempt**, not an entire user-visible turn:

```python
@dataclass
class TurnOutcome:
    visible_text: str
    reasoning_content: str
    tool_calls: list[ToolCall]
    usage: TokenUsage | None
    raw_parts: Any
    finish_reason: str | None
    disposition: TurnDisposition
```

`execute_single_turn()` owns the user-visible transaction. It may consume one normal `TurnOutcome` and, only for GLM degeneration, one recovery `TurnOutcome`. It then returns the existing `TurnResult` to mode runners. This keeps `normal`, `research`, `exploration`, and tool-producing phases of `plan` on their current `execute_single_turn()` contract. The only current direct no-tool caller, plan generation in `agent/modes/plan.py`, receives `TurnOutcome` but does not enable the tool-use degeneration guard because its request has no tools. Exploration synthesis already goes through `execute_single_turn()` and is not a direct `_stream_llm_turn()` caller. `_stream_llm_turn()` and `_stream_llm_turn_inner()` stop returning positional four-tuples.

`TurnDisposition` includes:

- `COMPLETED`: provider ended deliberately with visible text and no tool calls;
- `TOOL_USE`: one or more complete structured tool calls are present and no higher-priority abnormal terminal condition occurred;
- `TRUNCATED_TEXT`: `length`/`max_tokens` after visible text and without open tool calls;
- `TRUNCATED_PARTIAL_TOOL_USE`: terminal cutoff with at least one open tool call, even if a safe completed prefix exists;
- `DEGENERATED`: the GLM guard aborted before structured output;
- `FILTERED`: sensitive/content-filter terminal reason;
- `STREAM_BROKEN`: retryable provider/transport exception after partial output; this disposition takes precedence over `TOOL_USE` even when completed calls exist, while those calls remain available as the safe completed set;
- `FAILED`: provider/transport exception without usable partial output.

Disposition precedence is: `DEGENERATED` > `FILTERED` > `TRUNCATED_PARTIAL_TOOL_USE` > `TRUNCATED_TEXT` > `STREAM_BROKEN` > `TOOL_USE` > `COMPLETED` > `FAILED`, with `FAILED` used when no usable partial data exists rather than as a lower-priority override. This ordering makes telemetry deterministic when an outcome contains both completed calls and an abnormal terminal signal.

`RECOVERY_FAILED` is a **transaction status on `TurnResult`**, not a provider-attempt disposition: it means the first attempt was `DEGENERATED` and the single recovery attempt did not produce a usable `COMPLETED` or `TOOL_USE` outcome. `TurnResult` gains `finish_reason`, `disposition`, `recovery_attempted`, and `attempt_usages: list[AttemptUsage]` while preserving its existing `text`, `tool_calls`, `usage`, `cancelled`, and `error` fields for mode compatibility. `TurnResult.usage` is the normalized sum of its attempt usages for existing consumers.

Control flow uses these typed fields; warnings are presentation only. The normal loop must no longer infer all terminal behavior solely from `bool(tool_calls)`.

The GLM guard is enabled in all in-process callers sharing `AgentLoop`, including subagents, when they use a GLM provider and pass tools. A2A remote agents enforce their own provider policy. The disabled Orchestra path is outside this tranche.

### 6.5 Detector and recovery ownership

The streaming attempt owns detection; the turn transaction owns recovery:

- `_stream_llm_turn_inner()` observes normalized GLM reasoning deltas, updates a `GLMReasoningGuard`, closes the active stream when the guard triggers, and returns `TurnOutcome(disposition=DEGENERATED)` without persisting a message;
- `_stream_llm_turn()` retains its existing provider-error retry/backoff role for transport failures and returns one `TurnOutcome`;
- `execute_single_turn()` inspects that outcome and, only for `DEGENERATED`, performs the one request-local recovery attempt before reaching the existing assistant-message commit point;
- no mode runner, provider adapter, UI component, or detector initiates recovery independently.

A recovery attempt uses the same `tools_schema` passed into the first attempt. If existing consecutive-error policy had already set `_tools_disabled_for_turn`, the first attempt would have no tools and the degeneration guard would be inapplicable; recovery never re-enables disabled tools.

### 6.6 Request-scoped attempt options and transport retries

Add an immutable request context threaded through the existing call chain:

```python
@dataclass(frozen=True)
class LLMRequestContext:
    attempt_number: int = 1
    recovery: bool = False
    max_tokens_override: int | None = None
    system_suffix: str = ""
    disable_thinking: bool = False
```

`execute_single_turn()` constructs it; `_stream_llm_turn()`, `_stream_llm_turn_inner()`, `LLMProvider.chat_stream()`, and `_build_request_kwargs()` receive it. Existing callers omit it and retain current behavior. Only `GLMProvider` consumes the GLM-specific fields; other providers ignore defaults.

A logical attempt may still use the existing `_stream_llm_turn()` transport retry/backoff wrapper. `config.max_retries` remains the maximum number of HTTP/SSE transport tries **within that logical attempt**. A provider retry caused by `RateLimitError` or retryable `ProviderError` does not create another logical recovery attempt and does not change `attempt_number`. Degeneration is returned as a normal `TurnOutcome`, not raised as `ProviderError`, so it never enters transport backoff.

The one-shot rule means at most two logical attempts: normal attempt 1 and recovery attempt 2. Each may have bounded transport retries. Telemetry records both logical attempt number and transport retry index. Before dispatching recovery attempt 2, `execute_single_turn()` must call `loop._check_cancelled()` after verifying the pre-turn history snapshot.

### 6.7 GLM thinking policy

A small GLM-specific policy converts validated config plus attempt context into request fields.

Normal request:

- thinking enabled according to the user's GLM setting;
- configured `reasoning_effort`;
- preserved thinking enabled when configured;
- `tool_stream=True` only for the Z.AI endpoints/models declared by `GLMProvider` to support streaming tool arguments and only when tools are present.

Recovery request:

- `thinking.type = "disabled"`;
- `reasoning_effort = "none"`;
- recovery-specific output cap;
- ephemeral recovery instruction.

`GLMProvider.capabilities` declares reasoning support, streaming-tool support, and reasoning-effort support through new `ProviderCapabilities` flags: `reasoning_content`, `streaming_tool_calls`, and `reasoning_effort`. Existing providers receive conservative `False` defaults. The policy consults these flags rather than endpoint string checks during request construction.

The implementation does not attempt to classify whether a turn "intends" to call a tool. Normal turns retain thinking quality; all GLM turns with tools receive the runtime guard, and only confirmed degeneration triggers non-thinking recovery.

## 7. Request and history protocol

### 7.1 Normal GLM request

`GLMProvider._build_request_kwargs()` builds an allowlisted request using the Z.AI Chat Completions wire format. The OpenAI SDK receives standard parameters directly and GLM extensions through `extra_body` so unknown top-level SDK arguments cannot fail client-side validation:

```python
kwargs = {
    "model": model,
    "messages": messages,
    "max_tokens": max_tokens,
    "temperature": temperature,
    "tools": tools,
    "extra_body": {
        "thinking": {
            "type": "enabled",
            "clear_thinking": False,
        },
        "reasoning_effort": "max",
        "tool_stream": True,
    },
}
```

`thinking.type` is exactly `enabled` or `disabled`. `clear_thinking=False` corresponds to preserved thinking. For GLM-5.2, accepted `reasoning_effort` values are `max`, `xhigh`, `high`, `medium`, `low`, `minimal`, and `none`; `none`/`minimal` skip thinking, `low`/`medium` map provider-side to `high`, and `xhigh` maps to `max`. Settings labels these mappings. `reasoning_effort` is omitted for models other than GLM-5.2. `tool_stream` is sent only when `stream=True`, tools are present, and `capabilities.streaming_tool_calls` is true.

Unknown `ProviderConfig.extra` keys never flow into either the SDK kwargs or `extra_body`. The provider validates enum values and types before request construction.

### 7.2 Assistant-message replay

For each assistant message, `GLMProvider._format_messages()` emits the fields that exist:

```json
{
  "role": "assistant",
  "content": "visible text",
  "reasoning_content": "original reasoning",
  "tool_calls": []
}
```

Reasoning must be replayed verbatim and in original sequence. It must not be normalized, truncated independently, reordered, or reconstructed from rendered HTML/tags.

Existing duplicate tool-call ID repair remains applicable and must rewrite only IDs and matching tool results, not reasoning text.

### 7.3 Streaming

The GLM parser maps:

```text
delta.reasoning_content → StreamChunk.reasoning_delta
delta.content           → StreamChunk.text
delta.tool_calls        → existing index-keyed structured accumulator
finish_reason           → TurnOutcome.finish_reason
usage                   → cumulative usage snapshot
```

No literal `<think>` tags are inserted into `Message.content`.

The reasoning guard runs on both logical attempts. If recovery attempt 2 degenerates, it returns `DEGENERATED`; `execute_single_turn()` converts that second failed outcome into transaction status `RECOVERY_FAILED` and performs no third attempt.

## 8. Degeneration circuit breaker

### 8.1 Applicability

The guard runs only when all conditions hold:

- provider dialect is GLM;
- the request contains tools;
- no structured tool-call start has been observed;
- no substantial visible answer has been emitted;
- the user has not cancelled the turn;
- degeneration protection is enabled.

Once a structured tool-call start is observed, the reasoning-degeneration guard is disabled for that attempt. Partial tool-call safety then governs the stream.

### 8.2 Signals and fixed defaults

The detector combines independent signals rather than relying on a single phrase or regex. Defaults are fixed for this tranche so acceptance tests are deterministic.

#### Estimated reasoning-token hard ceiling

Abort when `estimated_reasoning_tokens >= 16_384` before structured output. Estimate incrementally as `ceil(utf8_reasoning_bytes / 3)`. The three-bytes-per-token divisor intentionally errs toward early cancellation for mixed English/code reasoning; provider-reported completion usage remains authoritative for billing.

This 16,384 estimated-reasoning-token ceiling is distinct from both the user's normal request `max_tokens`—81,920 in the recorded incident—and the recovery request's 16,384 **total output-token** cap.

#### Rolling repetition

Split normalized reasoning into non-empty logical lines, or 240-character segments when a line exceeds 240 characters. Normalize each segment by Unicode case folding, whitespace collapse, and punctuation-run collapse while preserving tool names, numbers, and addresses.

Maintain only the latest 128 segment fingerprints and a frequency table for that window. Memory is bounded to at most 128 normalized segments, each truncated to 240 characters, plus their hashes and counts.

After at least 32 total segments, evaluate the latest `min(64, total_segments)` segments. A segment is repeated when its fingerprint count within that evaluation window is at least two. Repetition is high when `repeated_segment_count >= ceil(0.60 * evaluation_window_size)`. Therefore, a full 64-segment window requires at least 39 repeated positions. Exactly 31 total segments can never satisfy `R`, regardless of repetition.

#### Meta-intent loop

A segment is meta-intent when it contains both:

- an imminent-action term: `call`, `calling`, `execute`, `executing`, `emit`, `output`, or `outputting`; and
- a tool-action term: `tool`, `invoke`, `function`, `parallel`, or the exact name of any exposed tool.

Meta-intent is repeated when at least eight such segments occur within the latest 64 segments. These phrases are supporting evidence only and never trigger recovery by themselves.

#### Substantial visible answer

Visible output is substantial at 256 non-whitespace characters. The repetition path stops applying after that threshold. The hard ceiling still applies only while structured tool-call starts remain zero, preventing unlimited hidden reasoning before a nominal visible suffix.

### 8.3 Trigger rule

Let:

```text
H = estimated_reasoning_tokens >= 16_384
R = total_segments >= 32 AND repeated_positions >= ceil(0.60 * min(64, total_segments))
M = meta_intent_segments >= 8 in the latest 64 segments
T = structured_tool_call_start_count == 0
V = visible_non_whitespace_chars < 256
```

Classify the attempt as `DEGENERATED` exactly when:

```text
T AND (H OR (R AND M AND V))
```

The fixed constants are named, documented, and covered by boundary and false-positive tests. They are not user-facing settings in this tranche except the hard reasoning ceiling, which is represented in config as estimated reasoning tokens.

## 9. Recovery transaction

### 9.1 Transient-event boundary

GLM reasoning is streamed to a transient thinking widget and accumulated outside `session.messages`. It is never appended to a visible assistant message widget. This avoids retrospective deletion: a degenerated attempt may be visible while actively thinking, but recovery clears that transient widget before any attempt content becomes durable or visible as an assistant answer.

Add `TurnEventType.RECOVERY_START` with metadata `attempt=2`, `reason="reasoning_degenerated"`, and `discard_transient_reasoning=True`. `ChatView` handles it by clearing the current transient thinking block, closing the spinner, and showing one compact retry status. Headless/control consumers receive the status event but have no state to delete. Usage events remain additive and are never rolled back.

The event queue treats `RECOVERY_START` as a boundary: it cannot be coalesced with reasoning or text events on either side.

### 9.2 Steps

When the guard fires:

1. close the upstream stream and honor cancellation immediately;
2. finalize usage from the discarded attempt using the protocol in Section 9.3;
3. emit `RECOVERY_START`;
4. do not append the discarded assistant output to `session.messages`;
5. clear its transient reasoning presentation;
6. rebuild the retry from `tuple(session.messages)` plus `tuple(message.id for message in session.messages)` captured at `execute_single_turn()` entry, and assert both length and IDs are unchanged;
7. call `loop._check_cancelled()` immediately before dispatch, then send the retry with thinking disabled and `reasoning_effort=none` through `LLMRequestContext`;
8. apply an effective recovery cap of `min(16_384, selected_model_max_output_tokens)` **total output tokens** by default;
9. add a temporary recovery suffix to the existing system prompt string for this request only;
10. permit no further automatic retry for degeneration.

No rollback of `SessionState` is required because `execute_single_turn()` currently persists the assistant message only after `_stream_llm_turn()` returns. The implementation preserves that commit boundary and asserts that the history length and message IDs still equal the pre-turn snapshot before starting recovery.

The request-local system-prompt suffix is:

```text
Emit structured tool calls immediately when tools are required.
Do not describe, simulate, or rehearse tool calls in prose.
If no tool is required, answer directly.
```

It is request-local and is never persisted into conversation history.

### 9.3 Discarded-attempt usage

Z.AI normally emits authoritative cumulative usage only in the terminal stream chunk. A client-side guard may close the stream before that chunk, so discarded-attempt billing cannot always be exact.

`AttemptUsage` therefore carries:

```python
usage: TokenUsage
provenance: Literal["authoritative", "estimated"]
```

If the provider emitted usage before cancellation, preserve it as `authoritative`. Otherwise estimate only `completion_tokens` with the same `ceil((reasoning_utf8_bytes + visible_utf8_bytes + tool_argument_utf8_bytes) / 3)` rule, set prompt tokens to the latest known prompt usage or zero when unavailable, and mark the record `estimated`. Estimated usage contributes to the UI/session estimate but is never presented as exact provider billing. Telemetry includes `usage_provenance` in its fixed allowlist.

The post-guard close must not wait indefinitely for a terminal usage chunk: close the stream, consume no additional generated content, and use the rules above.

### 9.4 Recovery success

Only the successful recovery assistant message is persisted. Tools execute normally if it contains complete structured calls.

Both attempt usages contribute to session totals; discarded-attempt usage retains its provenance and `discarded_attempt=true` marker. Add `SessionState.record_usage(usage: TokenUsage)`, and refactor `add_message()` to call it when a message has token usage. `execute_single_turn()` calls `record_usage()` only for discarded/non-persisted attempts; persisted successful messages continue contributing through `add_message()`. This prevents double counting while preserving the existing message-level usage contract.

### 9.5 Recovery failure

If the retry degenerates, reaches a reasoning-only length cutoff, or otherwise fails before valid output:

- do not retry again;
- do not persist the generated reasoning body;
- persist or emit only a compact technical failure marker;
- classify the result as `RECOVERY_FAILED`;
- explain that automatic recovery was exhausted and offer manual retry or a settings change.

## 10. GLM partial tool-call safety

This section is GLM-specific in this tranche. Existing non-GLM malformed-argument behavior remains unchanged to honor the cross-provider compatibility goal; a later provider-wide hardening change requires its own compatibility review.

For `GLMProvider`, a started tool call is executable only after:

1. a matching tool-call end is observed;
2. its full argument buffer parses as a JSON object;
3. required schema validation succeeds through the existing tool path.

GLM malformed or truncated arguments must not become `{}` and must not be dispatched. The shared stream accumulator therefore records argument parse status on the call/outcome; `execute_single_turn()` applies strict rejection when the active provider dialect is GLM and preserves the legacy fallback for other dialects.

If a GLM stream ends with `length` while tool calls remain open:

- discard every incomplete call and every call after the first incomplete call;
- define `safe_tool_calls` as the contiguous prefix of completed, valid calls before that boundary;
- persist the assistant message with **only** `safe_tool_calls`; omitted/incomplete calls and their IDs never enter `Message.tool_calls`;
- execute `safe_tool_calls` in order and persist exactly one result for each retained ID;
- classify the attempt as `TRUNCATED_PARTIAL_TOOL_USE` and surface a truncation warning even when the safe prefix executes;
- if the safe prefix is empty, persist no assistant tool-use message and execute nothing.

This produces a valid assistant-call/tool-result history: the persisted assistant message never advertises a call without a matching result. Tool-call start events for discarded IDs are transient UI/protocol events only. Before turn completion, emit one `TOOL_CALL_DISCARDED` event for each omitted ID so the matching tool widget closes as discarded; this event never creates a `ToolCall`, approval request, or persisted result.

## 11. Prompt change

Replace the global imperative:

```text
ALWAYS batch independent tool calls in a single parallel block.
```

with protocol-oriented wording:

```text
When multiple independent structured tool calls are needed, prefer emitting
them together in the same assistant turn. Never describe, simulate, or
rehearse tool calls in prose. If no tool is needed, answer directly.
```

The wording remains provider-neutral. It reduces meta-reasoning pressure but is not treated as the safety mechanism.

## 12. Configuration and migration

### 12.1 Configuration schema

GLM options live under `ProviderConfig.extra`:

```json
{
  "dialect": "glm",
  "thinking": {
    "enabled": true,
    "reasoning_effort": "max",
    "preserve": true
  },
  "degeneration_guard": {
    "enabled": true,
    "reasoning_token_ceiling": 16384,
    "retry_without_thinking": true,
    "recovery_max_tokens": 16384
  }
}
```

User-facing values are validated and clamped. Internal repetition thresholds are not stored here.

### 12.2 Validation

Add a typed GLM config parser that rejects unknown keys within the GLM namespace and validates:

- `dialect` is exactly `glm`;
- thinking enabled/preserve values are booleans;
- reasoning effort belongs to the model-supported enum;
- guard and retry switches are booleans;
- reasoning ceiling is 1,024–65,536 estimated reasoning tokens;
- recovery cap is 1,024–131,072 total output tokens and no greater than the selected model's declared output limit.

Invalid configuration fails validation with a user-facing field path; it never silently disables the guard. Non-GLM `extra` dictionaries remain opaque to avoid breaking custom providers.

### 12.3 Persistence fix

`LucNhanConfig._snapshot_current_provider()` must retain a deep copy of `provider.extra`; provider switching must restore a deep copy. `_apply_loaded_config()` already restores the active provider's top-level `extra`, so the confirmed defect is switching/snapshot persistence rather than initial disk load.

This behavior correction applies to all providers that use `extra`, not only GLM. Tests cover aliasing so editing one provider's nested options cannot mutate another snapshot.

API-key handling remains unchanged. Migration must not copy, log, or expose credentials.

### 12.4 Registry wiring and model capabilities

Add the built-in registry entry:

```python
"glm": "lucnhan.providers.glm_provider:GLMProvider"
```

Custom-provider creation selects `GLMProvider` when the saved profile's deep-copied `extra.dialect` is `glm`; otherwise it preserves the existing `OpenAICompatProvider` path. The factory passes `api_key`, `api_base`, `model`, profile name, and validated GLM options explicitly.

Streaming-tool support is model-specific. `GLMProvider` owns a model-metadata lookup whose entries include context/output limits plus `reasoning_content`, `streaming_tool_calls`, and `reasoning_effort`; `GLMProvider.capabilities` derives its current boolean flags from the selected model entry. Unknown GLM model IDs keep `reasoning_content=True` because the user explicitly selected GLM dialect, but disable streamed tool arguments with `streaming_tool_calls=False`; `tool_stream` is therefore omitted to avoid unsupported-parameter failures. Known capability metadata follows Z.AI's current API contract: `reasoning_content` starts with GLM-4.5-series-and-newer models, `tool_stream` starts with GLM-4.6-and-newer models, and `reasoning_effort` is sent only for GLM-5.2.

### 12.5 Existing custom provider migration

A custom provider is migrated to GLM dialect only when one of these explicit conditions holds:

- its saved provider name is the known `glm-coding` entry created by Luc Nhan's migration;
- its custom-provider configuration already declares `dialect: "glm"`;
- the user accepts the Settings migration prompt or selects GLM dialect.

For existing `openai_compat` or custom profiles whose base URL host is `api.z.ai`, Settings shows a one-time opt-in migration prompt. Declining preserves generic OpenAI-compatible behavior. Endpoint inspection never mutates configuration silently.

The built-in `glm` provider and migrated `glm-coding` profile preserve the user-configured model ID rather than listing GPT defaults. Initial supported IDs are `glm-5.2`, `glm-5.1`, `glm-5`, and `glm-4.7`; unknown GLM IDs remain selectable but use conservative capabilities until model metadata confirms support.

## 13. Settings and UI

When the selected provider dialect is GLM, Settings exposes:

- Thinking: Adaptive or Disabled;
- Reasoning effort: values supported by the configured GLM model;
- Preserve reasoning across tool turns: enabled by default for Coding Plan;
- Reasoning-loop protection: enabled by default;
- Hard reasoning ceiling: default 16,384 estimated reasoning tokens, range 1,024–65,536;
- Recovery output cap: default 16,384 total output tokens, clamped to model maximum.

Detailed repetition/meta-intent thresholds are internal and not exposed. The hard ceiling and recovery cap are product controls, not detector-internal tuning.

The chat UI:

- adds explicit `ChatView.handle_event()` branches: `REASONING_DELTA` directly updates the existing `_ThinkingBlock` widget without legacy tag parsing, `RECOVERY_START` removes that transient block and inserts exactly one compact retry-status widget, and `TOOL_CALL_DISCARDED` marks the matching tool widget terminal/discarded without approval or result content;
- renders `TEXT_DELTA` as visible answer only;
- closes the thinking state when recovery starts;
- shows one concise retry status;
- does not render or checkpoint the discarded multi-hundred-kilobyte reasoning attempt as a durable assistant message;
- continues to display and account for token usage from the discarded attempt.

`BackgroundAgentRunner` flushes both `pending_text` and `pending_reasoning` in normal non-delta boundaries, exception handling, cancellation, and its finalizer. Headless `run_prompt()` and the control-server event broker append `REASONING_DELTA` and `RECOVERY_START` to JSON event streams when enabled, but neither event changes `final_text`, error lists, terminal status, or exit code.

## 14. Outcome and error policy

| Provider signal/state | Attempt disposition | UI event/result | Persist assistant | Execute tools | Automatic retry |
|---|---|---|---:|---:|---:|
| deliberate stop with visible text and no calls | `COMPLETED` | normal text/turn end | Yes | No | No |
| one or more complete structured calls | `TOOL_USE` | tool events/turn continuation | Yes | Yes | No |
| GLM guard trigger before structured start | `DEGENERATED` | `RECOVERY_START` | No | No | Once, thinking off |
| successful recovery | recovery's `COMPLETED` or `TOOL_USE` | normal recovered result | Yes | If complete | No more |
| recovery ends `DEGENERATED`, reasoning-only `length`, `FILTERED`, or `FAILED` | transaction `RECOVERY_FAILED` | compact error/turn end | No generated body | No | No |
| `length`/`max_tokens` with visible text and no open call | `TRUNCATED_TEXT` | partial text + warning | Partial visible text | No | No |
| terminal cutoff with an open GLM tool call | `TRUNCATED_PARTIAL_TOOL_USE` | truncation error + valid prefix | Safe completed prefix only | Safe ordered prefix only | No |
| retryable transport/provider exception after partial output | `STREAM_BROKEN` | existing partial-stream warning | Existing valid partial data | Completed calls only | Existing transport policy |
| exception before usable partial output | `FAILED` | error | No | No | Existing transport policy |
| GLM `sensitive`/`content_filter` | `FILTERED` | compact filter error | No generated body | No | No |
| non-GLM content filtering | Existing provider behavior | Existing warning | Existing partial-message behavior | Existing behavior | Existing behavior |

A length warning for `DEGENERATED` or reasoning-only recovery failure must recommend reducing/disabling thinking or using recovery. It must not recommend increasing `max_tokens`.

Cancellation always wins over recovery: cancellation during either attempt closes the stream, emits `CANCELLED`, performs no additional retry, and persists no uncommitted attempt. A later user turn starts with the configured normal GLM policy; automatic-retry exhaustion is scoped to the previous user-visible turn.

Existing approval behavior applies only after a complete tool call reaches normal execution. The guard and partial-call handling cannot emit approval requests.

Mixed-provider history is supported: GLM replays `reasoning_content`; other providers ignore that field and retain their current formatter behavior. Switching providers does not delete the serialized field.

Context compaction treats `reasoning_content` as auxiliary assistant context. It may omit old reasoning when producing a compacted summary, but it never creates a compact recovery marker or rewrites current unresolved tool-call/result pairs.

## 15. Telemetry and diagnostics

Emit one structured record per attempt through the existing `lucnhan_structured.jsonl` JSON logging sink; do not add a new sink. Extend `_JSONFormatter` to copy one allowlisted `lucnhan_event` dictionary from `LogRecord` into the JSON object. Add `log_structured(event: dict[str, JSONScalar])`, which calls the existing logger with a constant message such as `agent_attempt` and `extra={"lucnhan_event": sanitized_event}`. One log record can contain exactly one `lucnhan_event`; duplicate assignment is rejected by the helper.

The helper uses an allowlist rather than a heuristic blocklist. Allowed keys are exactly:

```text
provider, model, dialect, turn_number, attempt_number,
transport_retry_index, thinking_mode, reasoning_effort,
configured_max_tokens, effective_max_tokens, prompt_tokens,
completion_tokens, total_tokens, usage_provenance, reasoning_chars, visible_chars,
tool_start_count, tool_end_count, finish_reason, disposition,
guard_trigger, repetition_ratio_millis, recovery_result,
discarded_attempt
```

Values are JSON scalars only. No arbitrary nested dictionaries or extra keys are accepted. Therefore `content`, `reasoning_content`, `text`, `tool_args`, `tool_results`, `raw_parts`, `messages`, `request`, `response`, `authorization`, `api_key`, `error`, and exception strings cannot enter `lucnhan_event`. Every allowed string value still passes through `strip_injection_markers()` and `strip_lone_surrogates()` before serialization. The human-readable debug log receives only a compact one-line disposition summary built from the same safe scalar fields, without repetition samples or generated content.

Emit the structured record without storing reasoning text:

- provider, model, and dialect;
- turn and attempt number;
- thinking mode and reasoning effort;
- configured and recovery output caps;
- prompt and completion token usage plus authoritative/estimated provenance;
- reasoning and visible character counts;
- tool-call start and completion counts;
- finish reason and disposition;
- guard trigger and repetition score;
- recovery result;
- discarded-attempt flag.

Never log API keys, raw request authorization, tool-result bodies, or reasoning content.

## 16. Testing strategy

### 16.1 Message and checkpoint tests

- new messages serialize and restore sanitized `reasoning_content`;
- old checkpoints without the field still load;
- literal `<think>` content in an old message is not migrated or altered;
- GLM `_raw_parts` remains `None`, while existing provider raw-parts behavior remains unchanged.

### 16.2 Config and migration tests

- `_snapshot_current_provider()` stores `copy.deepcopy(provider.extra)`;
- `switch_provider()` restores a separate deep copy and prevents nested aliasing;
- GLM enum/range validation reports exact field paths;
- unknown non-GLM `extra` remains opaque;
- built-in and custom-dialect registry paths select the expected provider;
- the Z.AI one-time migration prompt preserves generic behavior when declined.

### 16.3 Type and event contract tests

- `Message.to_dict()`/`from_dict()` round-trip sanitized `reasoning_content` with safe defaults;
- `StreamChunk.reasoning_delta` defaults to `None` and does not alter existing providers;
- `ProviderCapabilities.reasoning_content`, `streaming_tool_calls`, and `reasoning_effort` default to `False`;
- `TurnEvent.reasoning_event()`, `RECOVERY_START`, and `TOOL_CALL_DISCARDED` serialize through `to_dict()` with distinct payloads;
- `_stream_llm_turn()` direct callers use `TurnOutcome`, while `execute_single_turn()` callers retain the extended `TurnResult` contract;
- every `TurnDisposition` maps to the exact signal and precedence in Section 14;
- existing tuple assertions and direct-call tests in `tests/agent/test_agent_loop.py` are migrated to typed outcomes.

### 16.4 GLM request tests

- normal request includes validated thinking controls;
- `LLMRequestContext` reaches `_build_request_kwargs()` unchanged through the stream call chain;
- recovery request disables thinking, uses `reasoning_effort=none`, applies the effective cap, and appends the system suffix only for that request;
- transport retries stay within one logical attempt and degeneration never enters provider backoff;
- `tool_stream=True` is present only for GLM model metadata with `streaming_tool_calls=True` and only on streamed requests containing tools;
- unknown extra keys do not reach the SDK;
- GLM history replays reasoning verbatim alongside tool calls;
- duplicate tool-call ID repair still pairs results correctly.

### 16.5 GLM stream tests

- reasoning deltas and visible text deltas remain separate;
- parallel tool calls accumulate independently by index;
- finish reason and cumulative usage are emitted once;
- a complete tool call remains executable;
- an incomplete argument buffer is never converted to `{}` or executed.

### 16.6 Detector tests

- a reduced fixture modeled on the incident triggers at 16,384 estimated reasoning tokens and never later;
- the hard ceiling remains false at 49,149 UTF-8 reasoning bytes and becomes true at 49,150 bytes because `ceil(bytes / 3)` first reaches 16,384 there;
- exactly 31 segments never satisfy repetition; at 32–64 segments the threshold is `ceil(0.60 * window_size)` repeated positions; a full 64-segment window requires 39;
- repeated `outputting`/`parallel calls` prose with no tool start triggers at the documented repetition/eight-meta-segment thresholds;
- long, non-repetitive analysis does not trigger before the hard ceiling;
- ordinary short meta-intent prose does not trigger;
- 256 visible non-whitespace characters disable only the repetition path, while the hard ceiling remains effective until a structured call starts;
- any structured tool-call start disables the reasoning guard;
- detector state never exceeds 128 normalized segments of 240 characters plus hashes/counts;
- user cancellation is not classified as degeneration.

### 16.7 Recovery transaction tests

- discarded attempt content is absent from session history;
- discarded attempt usage is included with authoritative/estimated provenance in totals and telemetry;
- a guard abort before terminal usage creates a bounded estimate rather than reporting exact billing;
- retry uses the exact pre-turn history snapshot;
- retry instruction is request-local and not persisted;
- only one automatic degeneration retry occurs;
- successful recovery persists and executes complete tool calls;
- failed recovery emits a compact terminal error without persisting reasoning;
- the guard remains active on recovery and a second `DEGENERATED` outcome becomes `RECOVERY_FAILED` without another retry;
- cancellation before and during recovery prevents further attempts, including the boundary between attempts;
- each logical attempt may use existing bounded transport retries, which do not increment logical attempt count;
- the turn immediately after recovery failure uses normal configured policy;
- history message IDs and length are unchanged between discarded attempt and recovery start.

### 16.8 Cross-provider and mode regression tests

- OpenAI request payloads remain byte-for-byte equivalent for equivalent inputs;
- generic OpenAI-compatible providers do not receive GLM fields;
- Anthropic/MiniMax raw-parts replay still works;
- Gemini thought signatures remain unaffected;
- mixed GLM-to-Anthropic and Anthropic-to-GLM restored sessions remain valid;
- normal, research, exploration, plan generation, and plan execution consume the new result types correctly;
- in-process subagent GLM turns receive the guard; A2A event forwarding tolerates `REASONING_DELTA`;
- headless/control event serialization exposes reasoning and recovery status as pass-through JSON events without changing final text, errors, status, or exit code;
- existing finish-reason, broken-stream, approval, and tool-ID tests pass.

### 16.9 UI and Settings tests

- reasoning uses the thinking channel, not visible text;
- recovery clears the original transient thinking presentation through the explicit `RECOVERY_START` branch;
- exactly one recovery status is shown;
- runner exception, cancellation, and finalizer paths flush or discard pending reasoning deterministically;
- discarded attempt content is not left in the message widget;
- visible final text and tool progress behave as before;
- GLM-only settings appear only for the GLM dialect and validate ranges/enums;
- provider switching deep-copies and restores nested `extra` values;
- the one-time `api.z.ai` migration prompt honors accept and decline paths.

## 17. Acceptance criteria

1. The recorded incident fixture is aborted on the first chunk for which `ceil(utf8_reasoning_bytes / 3) >= 16_384`; it cannot consume a later reasoning chunk.
2. The recorded 81,920-token normal request cannot reach its provider cap under default guard settings while tool starts remain zero.
3. The repetition path cannot trigger before 32 segments and uses `ceil(0.60 * min(64, total_segments))` repeated positions plus at least eight meta-intent segments.
4. Recovery occurs exactly once with `thinking.type=disabled`, `reasoning_effort=none`, and an effective total-output cap of `min(16_384, selected_model_max_output_tokens)`.
5. Discarded reasoning is absent from checkpoint, visible assistant content, and future model context; its transient thinking widget is cleared by `RECOVERY_START`.
6. Recovery usage is authoritative when supplied by the provider; discarded-attempt usage is included with explicit `authoritative` or `estimated` provenance and is never mislabeled as exact billing.
7. GLM reasoning round-trips as `reasoning_content`, not literal `<think>` content; legacy and OpenAI histories retain existing behavior.
8. Incomplete or malformed GLM tool arguments are never dispatched as `{}`.
9. Completed parallel calls maintain ID/result pairing and ordering.
10. Cancellation during either attempt prevents recovery continuation and leaves no uncommitted attempt message.
11. Non-GLM provider wire payloads are unchanged for equivalent inputs.
12. All affected modes, headless/control serialization, subagents, UI, and Settings satisfy the contracts in Sections 6, 14, and 16.
13. Existing tests and all new tests pass.
14. Structured logs contain disposition and guard metrics but no reasoning bodies, tool bodies, authorization data, or credentials.

## 18. Rollout

Implement behind the explicit GLM dialect and enabled guard configuration. Migrate the known `glm-coding` profile, then verify against:

1. the deterministic incident fixture;
2. a mock GLM streaming integration test;
3. a live GLM Coding Plan session using non-sensitive prompts and tools;
4. existing provider regression suites.

If live recovery proves incompatible with an endpoint, disabling the GLM degeneration guard restores transport behavior without changing other providers. The message-model and protocol correctness changes remain independently useful.

## 19. Implementation surfaces

The implementation plan must account for these concrete surfaces:

- `lucnhan/core/types.py`: add `Message.reasoning_content: str = ""`, `StreamChunk.reasoning_delta: str | None = None`, `ProviderCapabilities.reasoning_content/streaming_tool_calls/reasoning_effort: bool = False`, immutable `LLMRequestContext`, and `AttemptUsage` provenance, plus serialization and persisted-reasoning sanitization;
- `lucnhan/core/config.py`: nested GLM validation and deep-copy snapshot/restore in both `_snapshot_current_provider()` and `switch_provider()`;
- `lucnhan/providers/glm_provider.py`: GLM transport dialect;
- `lucnhan/providers/registry.py`: built-in/custom dialect selection and metadata;
- `lucnhan/agent/turn.py`: `REASONING_DELTA`/`RECOVERY_START`, a dedicated reasoning payload field and constructors, and `to_dict()` serialization;
- `lucnhan/agent/loop.py`: `TurnOutcome`, stream accumulation, attempt telemetry;
- `lucnhan/agent/modes/turn_helpers.py`: recovery transaction and durable commit boundary;
- `lucnhan/state/session.py`: `record_usage()` for non-persisted attempts without double counting successful messages;
- direct `_stream_llm_turn()` consumers: plan generation plus the shared `execute_single_turn()` wrapper; exploration remains an indirect `execute_single_turn()` consumer;
- `lucnhan/ui/chat_view.py` and runner queue: separate transient reasoning and recovery boundary;
- headless/control/A2A event serializers: tolerate and expose new events as specified;
- Settings dialog/provider controls: GLM-only options and opt-in migration;
- session history and context compaction: new reasoning field without unresolved tool-pair damage;
- `lucnhan/core/log_sinks.py` and logging facade: allowlisted `lucnhan_event` JSON records in the existing sink;
- provider, agent-loop, config, checkpoint, UI, headless, and cross-provider tests.

The implementation plan must enumerate every direct `_stream_llm_turn()` and `execute_single_turn()` consumer discovered at planning time rather than assuming the current list remains static. At spec-review time, direct `_stream_llm_turn()` consumers are `execute_single_turn()` and plan generation only.

## 20. Security and privacy

The implementation must not log or persist discarded reasoning. Provider configuration diagnostics must redact API keys. Tests use synthetic credentials and fixtures. Existing plaintext credentials discovered during investigation require separate operational rotation; this design does not modify secret storage.

## 21. References

- [Z.AI GLM-5.2](https://docs.z.ai/guides/llm/glm-5.2)
- [Z.AI Thinking Mode](https://docs.z.ai/guides/capabilities/thinking-mode)
- [Z.AI Function Calling](https://docs.z.ai/guides/capabilities/function-calling)
- [Z.AI Streaming Tool Calls](https://docs.z.ai/guides/capabilities/stream-tool)
- [Z.AI Chat Completion API](https://docs.z.ai/api-reference/llm/chat-completion)

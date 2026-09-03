# Review Fixes Phase 1 — Security & Critical Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the 6 Critical/High findings from the 2026-09-02 full-project review: the `install_microcode_optimizer` RCE chain, the `execute_python` AST-blocklist bypasses, compaction orphaning tool-results, silently-lost subagent cancellation, the `OPENAI_API_KEY` env leak to third-party endpoints, and the plaintext API-key downgrade on `save()` without password.

**Architecture:** Fix at the existing seams — no new layers. The optimizer exec sink joins the `script_guard` + approval path that `execute_python` already uses (via a `requires_approval` flag on `@tool` honored by `_execute_single_tool`). The AST check gains receiver-agnostic blocked-call matching and module-blocklist additions. Compaction learns to shift its tail boundary instead of adding a post-hoc repair pass. Cancel-event ownership is made explicit on `AgentLoop`. Provider env-fallback becomes opt-in per class. `save()` preserves the existing encrypted block when no password is supplied.

**Tech Stack:** Python 3.10+ target (IDA host), pytest with stubbed IDA API (`tests/mocks/ida_mock.py`), ruff/mypy per `pyproject.toml`.

**Spec:** Full-project review findings, 2026-09-02 (this conversation). Source of truth for each finding's evidence: file:line listed per task below.

## Global Constraints

- Every module keeps `from __future__ import annotations`; type hints on all new signatures.
- Never hardcode the string `"execute_python"` — use `rikugan.constants.EXECUTE_PYTHON_TOOL_NAME`.
- Host API imports only via `importlib.import_module()` inside `try/except ImportError`.
- No new deps. No `eval`/`exec` outside `script_guard.run_guarded_script()` (Task 1 removes the one exception).
- CI must stay green: `./ci-local.sh` (ruff format+lint, mypy core+providers, pytest, desloppify ≥ 88.5).
- Commit style `type(scope): description`, branch `fix/review-phase1` off `master`.

---

### Task 1: Gate `install_microcode_optimizer` behind script_guard + user approval

**Files:**
- Modify: `rikugan/tools/base.py:150-200` (add `requires_approval` param to `@tool`, store on `ToolDefinition`)
- Modify: `rikugan/agent/loop.py:1761-1810` (`_execute_single_tool`: approval fires when `tc.name == EXECUTE_PYTHON_TOOL_NAME` **or** `definition.requires_approval`)
- Modify: `rikugan/ida/tools/microcode.py:311-379` (`install_microcode_optimizer`: run `python_code` through `_check_ast`, declare `requires_approval=True`)
- Modify: `rikugan/ida/tools/microcode_optim.py:121-139` (`compile_optimizer`: call `script_guard._check_ast` and raise on violation — make it public as `check_ast` re-export)
- Test: `tests/tools/test_script_guard.py` (existing file, extend), `tests/agent/test_approval_gate.py` (new)

**Interfaces:**
- Produces: `ToolDefinition.requires_approval: bool = False`; `rikugan.tools.script_guard.check_ast(code: str) -> str | None` (public alias of `_check_ast`).
- `_execute_single_tool` gate condition becomes:
  ```python
  needs_approval = (
      tc.name == constants.EXECUTE_PYTHON_TOOL_NAME
      or self.tools.get_definition(tc.name).requires_approval
  )
  ```

- [ ] **Step 1: Write failing tests**

```python
# tests/tools/test_script_guard.py — add
from rikugan.ida.tools.microcode_optim import compile_optimizer

def test_compile_optimizer_rejects_subprocess_import():
    code = "def optimize(mbi, ins): return 0\nimport subprocess\nsubprocess.run(['calc'])"
    try:
        compile_optimizer("evil", code)
    except ValueError as e:
        assert "disallowed module" in str(e) or "Blocked" in str(e)
    else:
        raise AssertionError("compile_optimizer accepted blocked code")

def test_compile_optimizer_accepts_pure_code():
    code = "def optimize(mbi, ins):\n    return 0\n"
    fn = compile_optimizer("ok", code)
    assert callable(fn)
```

```python
# tests/agent/test_approval_gate.py — new
"""install_microcode_optimizer must be approval-gated like execute_python."""
from rikugan.constants import EXECUTE_PYTHON_TOOL_NAME

def test_microcode_optimizer_flagged_requires_approval():
    from rikugan.ida.tools.microcode import install_microcode_optimizer
    assert getattr(install_microcode_optimizer, "requires_approval", None) is True or \
        install_microcode_optimizer.definition.requires_approval is True

def test_gate_condition_includes_requires_approval():
    # Inspect the source-level contract: any tool whose definition sets
    # requires_approval goes through _wait_for_approval.
    from rikugan.tools.base import ToolDefinition
    td = ToolDefinition(name="x", description="d", parameters={}, function=lambda: "", requires_approval=True)
    assert td.requires_approval is True
```

(Adapt attribute access to the actual `@tool` wrapping — read `rikugan/tools/base.py` first; the decorator currently attaches the `ToolDefinition` either to the function or returns it.)

- [ ] **Step 2: Run tests — expect FAIL**

Run: `python3 -m pytest tests/tools/test_script_guard.py -k compile_optimizer -v` and `python3 -m pytest tests/agent/test_approval_gate.py -v`
Expected: FAIL — `compile_optimizer` accepts blocked code; no `requires_approval` attribute.

- [ ] **Step 3: Implement**

1. `script_guard.py`: add `check_ast = _check_ast` public alias (or rename `_check_ast` → `check_ast` and keep private alias; update internal callers).
2. `microcode_optim.py::compile_optimizer` — first lines:
   ```python
   from ...tools.script_guard import check_ast
   violation = check_ast(textwrap.dedent(python_code))
   if violation:
       raise ValueError(f"Optimizer code rejected by script guard: {violation}")
   ```
3. `tools/base.py` `@tool(...)`: add `requires_approval: bool = False` parameter, set on the `ToolDefinition`.
4. `microcode.py::install_microcode_optimizer`: `@tool(..., requires_approval=True)`; wrap `compile_optimizer` ValueError → tool error message (existing error-return pattern in that file).
5. `agent/loop.py::_execute_single_tool`: extend the condition per Interfaces; on approval-deny, return the same deny message shape used for `execute_python`.

- [ ] **Step 4: Run tests — expect PASS** (same commands as Step 2)
- [ ] **Step 5: Full gate + commit**

Run: `./ci-local.sh`
```bash
git add -A && git commit -m "fix(security): gate install_microcode_optimizer behind script_guard and user approval"
```

---

### Task 2: Close AST-blocklist bypasses in `script_guard`

**Files:**
- Modify: `rikugan/tools/script_guard.py:18-80` (`_BLOCKED_MODULES`), `:120-159` (`_BLOCKED_ATTRS`), `:248-271` (`_check_ast` Call branch)
- Test: `tests/tools/test_script_guard.py`

**Interfaces:**
- Produces: hardened `_check_ast`; no signature change.

- [ ] **Step 1: Write failing tests** — one test per bypass vector from the review:

```python
BYPASSES = [
    "import builtins\nbuiltins.exec(\"import subprocess; subprocess.run('calc')\")",
    "import builtins\nbuiltins.__import__('subprocess')",
    "import timeit\ntimeit.timeit(\"import os; os.system('id')\", number=1)",
    "import pdb\npdb.set_trace()",
    "import inspect\ninspect.currentframe().f_back.f_builtins",
    "import operator\nf = operator.attrgetter('__class__')",
    "import builtins\nbuiltins.getattr(builtins, 'eval')",
]
@pytest.mark.parametrize("code", BYPASSES)
def test_known_bypasses_blocked(code):
    from rikugan.tools.script_guard import check_ast
    assert check_ast(code) is not None
```

Note: `inspect` frame walks hit the `_BLOCKED_DUNDER_ATTRS` dunder rule (`f_back`/`f_builtins` must be added there if absent). `pdb.set_trace` is a bare-attr call on an unblocked module — the receiver-agnostic rule in Step 3 catches it.

- [ ] **Step 2: Run — expect FAIL** (`builtins.exec` etc. currently pass)
- [ ] **Step 3: Implement**

1. Add to `_BLOCKED_MODULES`: `builtins`, `timeit`, `pdb`, `doctest`, `operator`, `inspect` (with comment: exec/getattr re-export + introspection vectors).
2. Add to `_BLOCKED_DUNDER_ATTRS`: `f_back`, `f_builtins`, `f_globals`, `f_locals`, `f_code` (frame-object attrs) — verify none legitimates a common analysis idiom first by grepping `rikugan/data/idapython-examples` and builtin skills for these attrs; IDAPython analysis scripts do not use frame introspection.
3. In `_check_ast`'s `ast.Call` branch, add receiver-agnostic rule after the pair check:
   ```python
   # Any call whose attribute name is itself a blocked built-in
   # (e.g. builtins.exec, X.eval, timeit.repeat-with-exec semantics)
   if isinstance(func, ast.Attribute) and func.attr in _BLOCKED_CALLS:
       return f"Blocked — attribute call to disallowed built-in '{func.attr}()'"
   ```
   Reviewer note: this also catches `builtins.getattr(...)` since `getattr` ∈ `_BLOCKED_CALLS`.
4. Leave `open()` in the namespace (analysis scripts legitimately read files); documented residual risk, user approval still gates execution.

- [ ] **Step 4: Run — expect PASS**; also run the whole existing guard suite to catch false positives:
  `python3 -m pytest tests/tools/test_script_guard.py -v`
- [ ] **Step 5: Grep builtin skills + `rikugan/data/idapython-docs` examples for newly blocked modules used in documented flows; confirm none. Commit**

```bash
git commit -am "fix(security): close builtins/timeit/pdb/inspect AST blocklist bypasses"
```

---

### Task 3: Stop compaction from orphaning tool-result messages

**Files:**
- Modify: `rikugan/agent/context_window.py:41-93` (`compact_messages`)
- Test: `tests/agent/test_context_window.py` (extend existing)

**Interfaces:**
- Produces: unchanged signature `compact_messages(messages: list[Message]) -> list[Message]`; new invariant — the first tail message is never `Role.TOOL` unless it is messages[0].

- [ ] **Step 1: Write failing test**

```python
def test_compaction_does_not_orphan_leading_tool_result():
    # Shape: [..., A(tc), T] boundary would normally start tail at the TOOL.
    msgs = build_messages([  # helper synthesizing Message objects per Role
        ("user", "u1"), ("assistant_tc", "decompile_function"), ("tool", "result1"),
        ("assistant", "a1"), ("user", "u2"), ("assistant_tc", "rename_function"),
        ("tool", "result2"), ("assistant", "a2"),
    ])
    compacted = manager.compact_messages(msgs)
    for i, m in enumerate(compacted[1:], start=1):  # skip retained head
        if m.role == Role.TOOL:
            prev = compacted[i - 1]
            assert prev.role == Role.ASSISTANT and prev.tool_calls, \
                f"orphaned TOOL at compacted index {i}"
```

(Use the existing message-builder fixtures in that test file; if none exist, construct `Message(role=..., content=..., tool_calls=[ToolCall(...)], tool_results=[...])` directly per `rikugan/core/types.py`.)

- [ ] **Step 2: Run — expect FAIL** (current fixed `[-4:]` cut orphans `("tool", "result2")` in the shape above)
- [ ] **Step 3: Implement** — replace the fixed cut with a boundary walk:

```python
keep_tail = 4
# Advance the tail start past any TOOL message whose assistant tool_call
# partner falls outside the tail — otherwise the provider rejects the
# orphaned tool result (OpenAI/Anthropic both 400).
tail_start = max(1, len(messages) - keep_tail)
while tail_start < len(messages) and messages[tail_start].role == Role.TOOL:
    tail_start += 1
head = messages[:1]
tail = messages[tail_start:]
middle = messages[1:tail_start]
```

(`if not middle: return messages` stays. Summary-building loop unchanged.)

- [ ] **Step 4: Run — expect PASS**; run full file `python3 -m pytest tests/agent/test_context_window.py -v`
- [ ] **Step 5: Commit**

```bash
git commit -am "fix(agent): compaction no longer orphans tool-result messages"
```

---

### Task 4: Don't clear an inherited/external cancel event in `AgentLoop.run()`

**Files:**
- Modify: `rikugan/agent/loop.py:355-380` (constructor: record `self._owns_cancel_event`), `:2571-2576` (`run()` clear conditionally)
- Test: `tests/agent/test_agent_loop.py` (extend; search for existing cancel tests and follow their harness)

**Interfaces:**
- Produces: `AgentLoop.__init__(..., cancel_event: threading.Event | None = None)` sets `self._owns_cancel_event = cancel_event is None`. Subagent pipelines additionally check `self._cancelled.is_set()` between child runs — that check lands in the callers named below, not the loop.

- [ ] **Step 1: Write failing test**

```python
def test_run_does_not_clear_inherited_cancel_event():
    parent_evt = threading.Event()
    parent_evt.set()  # user cancelled during child N
    child = AgentLoop(..., cancel_event=parent_evt, ...)  # same kwargs as existing tests
    child.run_once_or_run(...)  # smallest public entry existing tests use
    assert parent_evt.is_set(), "run() must not clear an externally-owned cancel event"

def test_run_clears_owned_cancel_event():
    loop = AgentLoop(...)  # no cancel_event kwarg
    loop._cancelled.set()
    run_once(loop)
    assert not loop._cancelled.is_set()
```

- [ ] **Step 2: Run — expect FAIL** on the first test.
- [ ] **Step 3: Implement**

```python
# __init__
self._owns_cancel_event = cancel_event is None
# run()
if self._owns_cancel_event:
    self._cancelled.clear()
```
Add an abort check between sequential child runs in: `agent/modes/research.py:280-345`, `agent/report_review.py:269-313`, `agent/hypothesis_verification.py` (its child loop ~:286):
```python
if self._cancelled.is_set():
    raise CancellationError()
```
(match the exception type already imported in those modules; loop.py:2715 converts it to CANCELLED.)

- [ ] **Step 4: Run — expect PASS**; full `python3 -m pytest tests/agent/test_agent_loop.py -k cancel -v`
- [ ] **Step 5: Commit**

```bash
git commit -am "fix(agent): preserve inherited cancel event across subagent pipelines"
```

---

### Task 5: Stop `OPENAI_API_KEY` env fallback leaking to compat/GLM endpoints

**Files:**
- Modify: `rikugan/providers/openai_provider.py:169-171` (env fallback behind class attr)
- Test: `tests/providers/test_openai_compat.py` (extend or create)

**Interfaces:**
- Produces: `OpenAIProvider._ALLOW_OPENAI_ENV_KEY: bool = True`; subclass override `False` in `OpenAICompatProvider` and `GLMProvider`.

- [ ] **Step 1: Write failing test**

```python
def test_compat_never_inherits_openai_env_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-secret")
    p = OpenAICompatProvider(base_url="https://api.z.ai/v1", api_key="")
    assert p.api_key in ("", None) or p.api_key == "no-key"  # NOT the env value

def test_glm_never_inherits_openai_env_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-secret")
    p = GLMProvider(api_key="")
    assert p.api_key != "sk-real-secret"
```

- [ ] **Step 2: Run — expect FAIL** (both currently read the env var).
- [ ] **Step 3: Implement**

```python
# openai_provider.py __init__, replacing the env read
class OpenAIProvider(LLMProvider):
    _ALLOW_OPENAI_ENV_KEY: bool = True
    def __init__(...):
        ...
        if not api_key and self._ALLOW_OPENAI_ENV_KEY:
            api_key = os.environ.get("OPENAI_API_KEY", "")
```
Add `_ALLOW_OPENAI_ENV_KEY = False` to `OpenAICompatProvider` (openai_compat.py:26) and `GLMProvider` (glm_provider.py:66). The "no-key" placeholder branches at openai_compat.py:42-44 / glm_provider.py:135-137 then take effect as designed.

- [ ] **Step 4: Run — expect PASS**; also `python3 -m pytest tests/providers -v` (env leakage into other tests guarded by monkeypatch).
- [ ] **Step 5: Commit**

```bash
git commit -am "fix(providers): opt-in OPENAI_API_KEY env fallback; stop leak to compat/GLM endpoints"
```

---

### Task 6: `save()` without password must not downgrade encrypted keys to plaintext

**Files:**
- Modify: `rikugan/core/config.py:282-296` (the `else` branch of the encryption block)
- Test: `tests/core/test_config.py` (extend)

**Interfaces:**
- Consumes: existing `self._encryption_block` (loaded by `load()` at config.py:~300) and `encrypt_keys`/blob shape from `core/crypto.py`.
- Produces: when `self.encrypt_api_keys` is True and no password is given, `save()` re-emits the existing `_encryption_block` verbatim and zeroes plaintext key fields in the dump — no downgrade, no plaintext write.

- [ ] **Step 1: Write failing test**

```python
def test_save_without_password_preserves_encryption(tmp_path, monkeypatch):
    cfg = RikuganConfig(config_dir=str(tmp_path))
    cfg.provider.api_key = "sk-secret"
    cfg.encrypt_api_keys = True
    cfg.save(password="pw123")
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["encryption"]["enabled"] is True
    cfg2 = RikuganConfig(config_dir=str(tmp_path))
    cfg2.load()
    cfg2.decrypt_stored_keys("pw123")
    cfg2.knowledge_show_retrieved_in_chat = True   # the panel_core.py:3655 path
    cfg2.save()                                     # no password
    raw = (tmp_path / "config.json").read_text()
    assert "sk-secret" not in raw, "plaintext downgrade"
    assert json.loads(raw)["encryption"]["enabled"] is True
```

- [ ] **Step 2: Run — expect FAIL** (`"sk-secret"` currently lands in the plaintext dump).
- [ ] **Step 3: Implement** — replace the `else` branch:

```python
        if self.encrypt_api_keys and password:
            ... existing encryption path unchanged ...
        elif self.encrypt_api_keys and self._encryption_block:
            # Password-less save (e.g. a UI toggle): preserve the existing
            # encrypted blob instead of downgrading keys to plaintext.
            d["encryption"] = {"enabled": True, **self._encryption_block}
            d.pop("encryption_ciphertext", None) if False else None
            d["provider"]["api_key"] = ""
            for info in d.get("providers", {}).values():
                info["api_key"] = ""
        else:
            d["encryption"] = {"enabled": False}
```

(Drop the no-op `d.pop(...) if False else None` line — shown only to mark placement; final code is the two assignments + the `d["encryption"]` re-emit. Verify `_encryption_block`'s dict keys are exactly what `load()` consumed so the round-trip is verbatim.)

- [ ] **Step 4: Run — expect PASS**; `python3 -m pytest tests/core/test_config.py -v`
- [ ] **Step 5: Commit**

```bash
git commit -am "fix(core): preserve encrypted API keys on password-less config save"
```

---

## Final verification (after Task 6)

- [ ] `./ci-local.sh` green end-to-end
- [ ] `git log --oneline` shows 6 commits on `fix/review-phase1`
- [ ] Push branch + open PR to `master` (fork `EliteClassRoom/rikugan`)

## Out of scope (Phase 2 plan, separate doc)

WorkspaceStore cross-thread lock, delegate_external_task approval, watchdog thread leak, Gemini/Codex retryable classification + empty-candidate guard, subagent approval deadlocks (bulk_renamer/SubagentManager), 13 missing mutation.py entries + consistency test, subagent mutation propagation, `oauth_consent_accepted` boolean guard, skill-description sanitization, bundle-import hash verification, RestoreWorker queue+QTimer migration, MemoryProjector portalocker fix, GLM model fetch, MiniMax thinking filter, `/undo` pop-before-reverse, plan-step status, low-priority cleanups.

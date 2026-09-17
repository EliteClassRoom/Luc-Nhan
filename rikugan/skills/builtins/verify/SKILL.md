---
name: Verify Hypotheses
description: Independent hypothesis verification for `/verify`. Delegates tool-backed verification to a fresh read-only agent that returns a JSON verdict per hypothesis (status, claim, citations). Run before `/report` so the report only includes verified claims.
tags: [verify, hypothesis, knowledge, memory, verification]
---

# Verify Hypotheses (`/verify`)

Use `/verify` to mark pending exploration hypotheses as `verified` or
`wrong` and to record the verdict claim plus the evidence citations.
The command dispatches a fresh, read-only subagent (no IDA mutation,
no scripts, no execution of the target) that must produce a JSON
verdict for every hypothesis. On a valid batch, the verdict fields
are committed to the raw store; otherwise the entire batch is
rejected and no record is mutated.

## Usage

- `/verify` — verify every hypothesis whose status is `unverified`.
- `/verify <id>` — verify a single hypothesis by its memory id.

## Flow

1. **Open store** — the command reuses `_open_knowledge_store`. If
   the store is unavailable, the command aborts with an error event.
2. **Select** — the command picks all unverified hypotheses, or the
   single record named in the argument. Already-terminal records
   (`verified` or `wrong`) and non-hypothesis records are no-ops.
3. **Verify** — the chosen records are passed to
   `verify_hypotheses(loop, hypotheses, max_attempts=3)`. The
   subagent must return JSON of the form
   `{"verdicts": [{"id": ..., "status": "verified" or "wrong",
   "claim": "<non-empty>", "citations": ["<citation>", ...]}]}`.
   Each citation must start with `function:`, `address:`, or
   `tool_result:`. The batch is rejected if any entry is missing,
   duplicate, or has empty claim or citations, or has an unknown id.
4. **Commit** — the verdict fields are upserted and the read-back is
   compared before emitting the `HYPOTHESIS_VERDICT` event so the
   committed state is observable to the UI and the knowledge store.
5. **Observe** — one `HYPOTHESIS_VERDICT` event is emitted per
   verdict. The visible text is the claim; the metadata carries the
   status, claim, and citations.

## Hard rules

- `/verify` is read-only with respect to the analyzed IDA database.
  The verifier must use only read-only tools.
- The verdict `claim` must be a non-empty explanation of why the
  hypothesis is right or wrong. For `wrong` verdicts the claim
  must state what the hypothesis got wrong.
- Citations must match one of `function:<name>`, `address:0x<hex>`,
  or `tool_result:<id>`. At least one citation is required per
  verdict.
- Hypotheses are the only memory type `/verify` accepts. Any other
  id is a no-op and a `not a hypothesis` event is emitted.
- A failed verification batch leaves every record unchanged and
  reports the failure as a single terminal error event.

## Failure behavior

- No provider configured — `/verify` returns an error event.
- No unverified hypotheses — `/verify` returns a no-pending text
  event and does not invoke an agent.
- All three attempts produce an invalid response — `/verify`
  returns an error event listing the unresolved ids; no record is
  mutated.

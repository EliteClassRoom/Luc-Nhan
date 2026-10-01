---
name: Verified Report
description: Generate a verified Markdown report from stored hypotheses about a binary. Triggered by `/report` or `/report <scope>` (`full`, `executive`, `technical`, `iocs`, `network`). Only hypotheses whose status is `verified` are surfaced. Draft is written only after the user confirms.
tags: [report, knowledge, memory, verification, hypothesis]
---


# Verified Report (`/report`)

Generate a Markdown report from the stored knowledge for the currently
loaded binary. The draft only contains hypothesis memories whose
`status == "verified"` and the verdict claim plus the citations
recorded by `/verify`. The Markdown draft is written only after the
user explicitly confirms.

## Usage

- `/report` — full scope, all sections, every verified hypothesis.
- `/report executive` — high-level summary; no technical detail.
- `/report technical` — verified-hypothesis claim and citations.
- `/report iocs` — verified-hypothesis IOCs only.
- `/report network` — verified-hypothesis network summary.

## Flow

1. **Open store** — `_handle_report_command` resolves the per-binary
   raw knowledge store. If unavailable, the command aborts with an
   error event and no draft is produced.
2. **Filter** — Only memories with `type == "hypothesis" and
   `status == "verified"` enter the report pack. Unverified and wrong
   hypotheses, every other memory type, and `report` records are
   excluded.
3. **Collect evidence** — For `full` and `technical` scopes, the
   handler decompiles (via Hex-Rays `decompile_function`) — or
   disassembles when the decompiler is unavailable — the addresses
   cited by the verified memories and attaches the output as a
   `## Binary Evidence` (tool-verified) block in the writer prompt,
   along with `## File Metadata` from `get_binary_info` when
   available. When no decompiler or IDA tools are available, the
   report is generated without evidence blocks and the writer omits
   `### Evidence` subsections.
4. **Draft** — The verified-hypothesis subset is wrapped in a
   sanitized report-pack envelope and the LLM is asked for a Markdown
   draft. The report must cite each hypothesis by ID and reflect the
   stored claim and citations.
5. **Confirm** — The agent yields a `Write report | Cancel` question
   for the user. No file is created and no report is ingested until
   the user answers `Write report`, `Write`, `Yes`, `Save`, or `1`.
   Any other answer discards the draft.
6. **Write** — On confirmation, the report is written atomically
   beside the analyzed binary
   (`<binary-parent>/report-YYYY-MM-DD-HHMM.md`) with collision
   suffixes, and the report is ingested back into the raw store as
   a `report_generated` observation.

## Hard rules

- Run `/verify` first. `/report` consumes stored verdicts; it must
  not invoke a fresh verifier or convert a provisional or wrong
  hypothesis on its own.
- Distinguish verified from provisional. `/report` only surfaces
  hypotheses whose status is `verified`. Unverified and wrong
  hypotheses are never reportable.
- Do not bypass the confirmation step. The user must answer
  before any file is written. The atomic-write step guarantees no
  partial file is left behind on failure.
- Do not run `/report` automatically. It creates a new file in
  the binary's folder. Always wait for explicit user request.

## Failure behavior

- No verified hypotheses pending — `/report` returns a `No stored
  knowledge to report. Try running /research <goal>, save_memory, or
  exploration_report first.` text event and writes nothing.
- No provider configured — `/report` returns an error event; no
  draft is produced.
- Raw store unavailable — the command aborts before drafting; no
  draft, no question, no file.

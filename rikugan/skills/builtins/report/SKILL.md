---
name: Verified Report
description: Generate a verified Markdown report from stored findings about a binary. Triggered by `/report` or `/report <scope>`. Verified-only review pipeline runs before drafting; draft is written only after the user confirms.
tags: [report, knowledge, memory, verification]
---

# Verified Report (`/report`)

Generate a Markdown report from the stored knowledge for the currently
loaded binary. Every non-report candidate finding is independently
verified with IDA tools (up to three correction cycles) before drafting.
The Markdown draft is written only after the user explicitly confirms.

## Usage

- `/report` — full scope, all sections, all verified findings.
- `/report executive` — high-level summary; no technical detail.
- `/report technical` — function/data-structure detail.
- `/report iocs` — indicators of compromise only.
- `/report network` — C2 endpoints, protocols, network summary.

## Flow

1. **Open store** — `_handle_report_command` resolves the per-binary
   raw knowledge store. If unavailable, the command aborts with an
   error event and no draft is produced.
2. **Verify** — Every non-report candidate is run through an
   independent tool-backed reviewer. Findings that fail
   verification are corrected and re-verified, up to three cycles.
   Findings that still fail after three cycles abort the report.
3. **Persist** — Verified findings are marked `verified=True` and
   upserted into the raw store.
4. **Draft** — The verified subset is wrapped in a sanitized
   report-pack envelope and the LLM is asked for a Markdown draft.
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

- Do not call `/report` on a store that has not been explored.
  The review pipeline runs but the result is meaningless.
- Distinguish verified from provisional. `/report` only surfaces
  verified findings. A failed review aborts the report and lists
  the unresolved IDs.
- Do not bypass the confirmation step. The user must answer
  before any file is written. The atomic-write step guarantees no
  partial file is left behind on failure.
- Do not run `/report` automatically. It creates a new file in
  the binary's folder. Always wait for explicit user request.

## Failure behavior

- Reviewer disagrees with a claim — the corrected content is
  shown via the corrected finding; the report proceeds only when
  every finding passes.
- No provider configured — `/report` returns an error event; no
  draft is produced.
- Raw store unavailable — the command aborts before drafting; no
  draft, no question, no file.

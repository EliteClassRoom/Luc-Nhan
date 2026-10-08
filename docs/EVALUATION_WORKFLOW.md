# Project Evaluation Workflow

> A reusable, language-agnostic workflow for evaluating one or more software projects.
> Produces ranked issues, migration candidates, an executive summary, and a full evidence trail.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Inputs](#2-inputs)
3. [Phases](#3-phases)
4. [Workflow Patterns](#4-workflow-patterns)
5. [Quality Gates](#5-quality-gates)
6. [Token Budget Guidance](#6-token-budget-guidance)
7. [Anti-Patterns](#7-anti-patterns-to-avoid)
8. [Reusability Notes](#8-reusability-notes)
9. [Worked Example (Luc Nhan)](#9-worked-example)

---

## 1. Overview

### Purpose

Evaluate a software project (or compare two) and produce **actionable findings** backed by
file-and-line evidence. The workflow is designed to be run by AI agents orchestrated in
parallel, with adversarial verification to eliminate false positives.

### When to Use

| Scenario | Notes |
|---|---|
| Quarterly code audit | Run Phases 1-2-4; skip 3 unless comparing |
| Pre-merge evaluation | Run Phase 2 only on the diff |
| Taking over a project | Full Phases 1-2-4, with emphasis on architecture |
| Evaluating a fork vs upstream | Full Phases 1-2-3-4 |
| Security audit | Phase 2 with extra security agents; add Phase 2.5 threat model |
| Migration planning | Phase 3 comparison + Phase 4 migration plan |

### What It Produces

| Deliverable | Format | Consumer |
|---|---|---|
| Ranked issues | JSON `ISSUES_SCHEMA` + markdown table | Developer, CI gate |
| Ranked migration candidates | JSON `MIGRATION_SCHEMA` + ordered list | Tech lead, PM |
| Executive summary | Markdown (user's preferred language) | Stakeholders |
| Evidence trail | `file:line` citations per finding | Developer, auditor |
| Discovery baseline | JSON `DISCOVERY_SCHEMA` | Any downstream tooling |

---

## 2. Inputs

### Required

| Input | Type | Example |
|---|---|---|
| `project_paths` | `string[]` (1-2 absolute paths) | `["D:/re_dev_projects/vibe-clone/lucnhan"]` |
| `project_names` | `string[]` (labels) | `["current", "fork"]` |

### Optional

| Input | Type | Default | Purpose |
|---|---|---|---|
| `focus_dimensions` | `string[]` | `["quality","security","architecture","testing"]` | Narrow analysis scope |
| `risk_appetite` | `"low" \| "medium" \| "high"` | `"medium"` | Controls severity thresholds |
| `time_budget_minutes` | `number` | `30` | Caps total agent runtime |
| `known_pain_areas` | `string[]` | `[]` | Direct agents to specific concerns |
| `output_language` | `string` (BCP 47) | `"en"` | Executive summary language |
| `token_budget` | `number` | Inferred from LOC | Hard cap on total token spend |
| `skip_phases` | `number[]` | `[]` | e.g. `[3]` to skip comparative |

### Invocation Sketch

```
You are the Evaluation Orchestrator. Run the Evaluation Workflow on:

PROJECT_PATHS: ["D:/re_dev_projects/vibe-clone/lucnhan"]
PROJECT_NAMES: ["current"]
FOCUS_DIMENSIONS: ["quality", "security", "architecture", "testing"]
RISK_APPETITE: medium
OUTPUT_LANGUAGE: en
SKIP_PHASES: [3]
```

---

## 3. Phases

### Phase 1: Discovery (Always)

**Goal:** Produce a factual baseline of every target project. No opinions -- only measurements.

#### Agents

| Agent | Count | Purpose |
|---|---|---|
| Project Scanner | 1 per project | Emits `DISCOVERY_SCHEMA` |
| Git State Scanner | 1 | Dirty files, recent commits, branch divergence, remotes |

#### Project Scanner Prompt

```
You are a Project Scanner. Analyze the project at {{PROJECT_PATH}}.
Produce a JSON object matching DISCOVERY_SCHEMA.
Steps:
1. List all source directories and their file counts.
2. Count total LOC (lines of code, excluding blanks/comments).
3. Identify the tech stack: language(s), framework(s), build system, test runner.
4. List the top-20 largest files by LOC (flag any >800 lines).
5. Identify entry points (main, CLI, plugin entry, server start).
6. Map module boundaries (directory structure = module map).
7. Check for dependency manifests (pyproject.toml, package.json, Cargo.toml, go.mod).
8. Report presence/absence of: CI config, linter config, type checker config, pre-commit hooks.
9. Note any .env, config, or secret-like files present in the tree.
Output ONLY valid JSON matching DISCOVERY_SCHEMA.
```

#### Git State Scanner Prompt

```
You are a Git State Scanner. Analyze the repository at {{PROJECT_PATH}}.
Produce: current branch, working tree status, recent 20 commits, all remotes,
all branches, divergence count if comparing, tags/releases, CI/CD workflow presence.
Output as structured JSON.
```

#### DISCOVERY_SCHEMA (Outline)

```json
{
  "project_name": "string",
  "path": "string",
  "loc": { "total": 0, "source": 0, "test": 0, "config": 0, "other": 0 },
  "tech_stack": {
    "languages": [], "frameworks": [], "build_system": "",
    "test_runner": "", "linter": "", "type_checker": ""
  },
  "modules": [{ "name": "", "path": "", "file_count": 0, "loc": 0 }],
  "top_files": [{ "path": "", "loc": 0, "flagged": false }],
  "entry_points": [],
  "has_ci": false, "has_linter": false, "has_type_checker": false,
  "has_precommit": false, "secret_like_files": []
}
```

#### Barrier Conditions

- [ ] `DISCOVERY_SCHEMA` is valid JSON for every project
- [ ] Total LOC is known (needed for token budget)
- [ ] No critical path-access errors (project path must be readable)

#### Estimated Cost

| Project Size | Tokens |
|---|---|
| Small (<5k LOC) | 15-30k |
| Medium (5-30k LOC) | 30-60k |
| Large (>30k LOC) | 60-100k |

---

### Phase 2: Deep Analysis (Always, Parallel)

**Goal:** Produce a ranked list of issues per project across multiple dimensions.
Each finding includes `file:line` evidence and a suggested fix.

#### Agents

| Agent | Dimension | Focus |
|---|---|---|
| Code Quality Scanner | `quality` | Mutation, magic numbers, error handling, type safety, naming, file/function size, deep nesting |
| Security Scanner | `security` | Secrets, input validation, auth, path traversal, injection, data exposure, crypto, dependencies |
| Architecture Scanner | `architecture` | Separation of concerns, coupling, state mgmt, threading, immutability, abstraction, dependency flow, dead code |
| Test Quality Scanner | `testing` | Coverage gaps, isolation, AAA pattern, flakiness, naming, assertions, mock quality |

#### Code Quality Scanner Prompt

```
You are a Code Quality Scanner evaluating {{PROJECT_NAME}} at {{PROJECT_PATH}}.
For EACH finding, produce: severity (CRITICAL|HIGH|MEDIUM|LOW), dimension ("quality"),
category (mutation|magic_number|error_handling|type_safety|naming|file_size|function_size|deep_nesting),
file (relative path), line (number or range), description, suggested_fix, evidence (code snippet).
Focus on: in-place mutation, unnamed numeric literals, bare except/swallowed errors,
Any types/missing return types, ambiguous names, files >800 lines, functions >50 lines,
nesting >4 levels.
Output a JSON array matching ISSUES_SCHEMA.
```

#### Security Scanner Prompt

```
You are a Security Scanner evaluating {{PROJECT_NAME}} at {{PROJECT_PATH}}.
Same output format as Code Quality Scanner but dimension: "security".
Categories: secrets, input_validation, injection, path_traversal, auth, data_exposure,
crypto, dependencies.
Check: hardcoded credentials, unsanitized input, string concat in queries/commands,
user-built file paths, missing auth checks, verbose error messages, weak crypto,
known-vulnerable dependency versions.
Output a JSON array matching ISSUES_SCHEMA.
```

#### Architecture Scanner Prompt

```
You are an Architecture Scanner evaluating {{PROJECT_NAME}} at {{PROJECT_PATH}}.
Same output format, dimension: "architecture".
Categories: separation_of_concerns, coupling, state_management, threading, immutability,
abstraction, dependency_flow, dead_code.
Check: modules mixing UI/logic/data, circular dependencies, global mutable state,
race conditions, mutation where immutability is safer, leaky abstractions,
dependencies pointing outward instead of inward, unreachable code.
Output a JSON array matching ISSUES_SCHEMA.
```

#### Test Quality Scanner Prompt

```
You are a Test Quality Scanner evaluating {{PROJECT_NAME}} at {{PROJECT_PATH}}.
Same output format, dimension: "testing".
Categories: coverage_gaps, isolation, aaa_pattern, flakiness, naming, assertions, mock_quality.
Check: source modules with no tests, shared state between tests, non-AAA structure,
timeouts/sleep/date dependencies, vague test names, missing/trivial assertions,
over-mocked or under-mocked tests.
Output a JSON array matching ISSUES_SCHEMA.
```

#### ISSUES_SCHEMA (Outline)

```json
[{
  "id": "quality-001",
  "severity": "CRITICAL|HIGH|MEDIUM|LOW",
  "dimension": "quality|security|architecture|testing",
  "category": "string",
  "file": "relative/path.py",
  "line": "42 or 100-115",
  "description": "string",
  "suggested_fix": "string",
  "evidence": "actual code snippet"
}]
```

#### Optional Phase 2b: Domain-Specific Agents

For large codebases (>30k LOC), split into domain-specific agents:

| Domain | Example Focus |
|---|---|
| Provider layer | API integration, retry logic, auth flow |
| UI layer | Component size, accessibility, theme handling |
| Agent/MCP layer | Tool dispatch, prompt engineering, subagent lifecycle |
| Data/State layer | Serialization, state machines, persistence |
| Build/CI layer | Pipeline correctness, caching |

Spawn if more than 8 non-trivial modules exist in the discovery output.

#### Barrier Conditions

- [ ] At least 3 of 4 dimension agents have returned valid findings arrays
- [ ] Every CRITICAL finding has `file:line` evidence
- [ ] Findings are deduplicated (same issue from multiple agents merged)

#### Estimated Cost

| Project Size | Tokens (all 4 agents) |
|---|---|
| Small (<5k LOC) | 80-150k |
| Medium (5-30k LOC) | 200-400k |
| Large (>30k LOC) | 400-800k (or 150-250k per domain agent) |

---

### Phase 3: Comparative (Only When Comparing 2+ Projects)

**Goal:** Identify what differs between projects and produce a ranked migration plan.

#### Agents

| Agent | Purpose |
|---|---|
| Diff Analyzer | Structural diff: files only-in-A, only-in-B, modified |
| Strengths Analyzer | What B does better than A (and vice versa) |

#### Diff Analyzer Prompt

```
You are a Diff Analyzer comparing {{PROJECT_A}} and {{PROJECT_B}}.
Using DISCOVERY_SCHEMA outputs and git diff information:
1. FILES ONLY IN A / ONLY IN B: list with kind (source|test|config|other) and LOC.
2. MODIFIED FILES: summarize nature of difference, rate conflict risk HIGH/MEDIUM/LOW.
3. DIVERGENCE METRIC: commits apart, files different, percentage diverged.
Output JSON matching DIFF_SCHEMA.
```

#### DIFF_SCHEMA (Outline)

```json
{
  "project_a": "string", "project_b": "string",
  "only_in_a": [{ "path": "", "kind": "", "loc": 0, "notes": "" }],
  "only_in_b": [{ "path": "", "kind": "", "loc": 0, "notes": "",
                   "migration_priority": "P0|P1|P2|P3",
                   "migration_risk": "HIGH|MEDIUM|LOW",
                   "migration_effort": "HIGH|MEDIUM|LOW" }],
  "modified": [{ "path": "", "change_type": "", "conflict_risk": "", "summary": "" }],
  "divergence": { "commits_apart": 0, "files_different": 0, "diverged_percentage": 0 }
}
```

#### Strengths Analyzer Prompt

```
You are a Strengths Analyzer comparing {{PROJECT_A}} and {{PROJECT_B}}.
Using DISCOVERY_SCHEMA, ISSUES_SCHEMA, and DIFF_SCHEMA:
1. STRENGTHS OF B: what B does better, with evidence, migration_value, migration_complexity.
2. STRENGTHS OF A: what A does better that should be preserved.
3. MIGRATION CANDIDATES (ranked): ordered changes from B to port to A, each with
   priority (P0-P3), risk, effort, dependencies, files_affected.
Output JSON matching MIGRATION_SCHEMA.
```

#### MIGRATION_SCHEMA (Outline)

```json
{
  "source_project": "string", "target_project": "string",
  "candidates": [{
    "id": "string", "change": "string",
    "priority": "P0|P1|P2|P3",
    "risk": "HIGH|MEDIUM|LOW",
    "effort": "HIGH|MEDIUM|LOW",
    "dependencies": [], "files_affected": [],
    "evidence": "", "preserves_from_a": ""
  }]
}
```

#### Barrier Conditions

- [ ] `DIFF_SCHEMA` is valid and complete
- [ ] `MIGRATION_SCHEMA` candidates are priority-ranked
- [ ] Every P0 candidate has `files_affected` listed

#### Estimated Cost

| Comparison Type | Tokens |
|---|---|
| Small vs Small | 40-80k |
| Medium vs Medium | 80-200k |
| Large vs Large | 200-400k |

---

### Phase 4: Synthesis (Always)

**Goal:** Cross-reference all phase outputs, verify findings adversarially, produce deliverable.

#### Agents

| Agent | Purpose |
|---|---|
| Synthesizer | Merges Phase 1+2+(3) into `SYNTHESIS_SCHEMA` |
| Report Writer | Converts `SYNTHESIS_SCHEMA` into final markdown report |
| Verifier (x3) | Adversarial verification of CRITICAL and HIGH findings |
| Completeness Critic | Asks "what did we miss?" |

#### Synthesizer Prompt

```
You are the Synthesizer. You receive:
1. DISCOVERY_SCHEMA for each project
2. ISSUES_SCHEMA findings from Phase 2 (all dimensions)
[3. DIFF_SCHEMA and MIGRATION_SCHEMA from Phase 3 (if applicable)]

Tasks:
A. DEDUPLICATION: Merge findings referring to the same underlying issue.
   Keep the most specific evidence. Assign canonical IDs.
B. CROSS-REFERENCING: Escalate severity when security correlates with architecture,
   when test gaps correlate with known bug patterns, when one root cause explains
   multiple quality findings.
C. RANKING: Sort by severity, then blast radius, then fix urgency.
D. EXECUTIVE SUMMARY: 5-10 sentences in {{OUTPUT_LANGUAGE}}.
   Cover: overall health, top 3 risks, recommended immediate actions.
Output JSON matching SYNTHESIS_SCHEMA.
```

#### SYNTHESIS_SCHEMA (Outline)

```json
{
  "projects": [{
    "name": "", "loc": 0, "health_score": 0,
    "finding_counts": { "critical": 0, "high": 0, "medium": 0, "low": 0 }
  }],
  "deduplicated_findings": [{
    "canonical_id": "C-001", "source_ids": [],
    "severity": "", "dimension": "", "category": "",
    "file": "", "line": "", "description": "",
    "suggested_fix": "", "evidence": "", "blast_radius": ""
  }],
  "root_cause_groups": [{
    "group_id": "RC-01", "root_cause": "",
    "finding_ids": [], "recommended_action": ""
  }],
  "ranking": ["C-001", "C-002", "..."],
  "executive_summary": "string",
  "migration_plan": [{
    "step": 1, "action": "", "priority": "", "risk": "",
    "effort": "", "files": []
  }]
}
```

#### Verifier Prompt (spawned 3x per CRITICAL/HIGH finding)

```
You are a Verifier. Attempt to REFUTE this finding (adversarial: assume it is WRONG).
FINDING: {{FINDING_JSON}}
Steps:
1. Read the file at the cited location.
2. Check if the evidence actually supports the finding.
3. Look for mitigating context (nearby error handling, downstream validation).
4. Check for false positives (test code, example code, documented intentional patterns).
Output: verdict ("CONFIRMED"|"REFUTED"), confidence (0.0-1.0), reasoning, counter_evidence.
```

#### Completeness Critic Prompt

```
You are a Completeness Critic. Review the SYNTHESIS_SCHEMA and ask:
1. Were all requested dimensions actually analyzed? (0 findings is suspicious)
2. Were all modalities covered? (source, test, build/CI, deps, docs, git history)
3. Are there tech-stack-specific domains we missed?
4. Are there common issues for this stack that appear nowhere in the findings?
Output: dimensions_covered, dimensions_missing, modalities_covered, modalities_missing,
potential_false_negatives, recommended_additional_scans.
```

#### Report Writer Prompt

```
You are the Report Writer. Convert SYNTHESIS_SCHEMA into a markdown evaluation report.
Structure: Executive Summary, Project Health, Critical/High/Medium/Low Findings
(each with file:line, evidence, description, suggested fix, verification status),
Root Cause Analysis, Migration Plan (if applicable), Coverage Notes, Methodology.
Write the executive summary in {{OUTPUT_LANGUAGE}}.
```

#### Barrier Conditions

- [ ] All CRITICAL findings survived adversarial verification (>=2/3 verifiers confirmed)
- [ ] REFUTED findings removed or downgraded
- [ ] Completeness critic consulted and gaps noted

#### Estimated Cost

| Project Size | Tokens |
|---|---|
| Small | 50-80k |
| Medium | 100-200k |
| Large | 200-400k |

---

### Phase Summary Table

| Phase | Always? | Agents | Est. Cost (Medium) | Output Schema |
|---|---|---|---|---|
| 1. Discovery | Yes | 2-3 | 30-60k | `DISCOVERY_SCHEMA` |
| 2. Deep Analysis | Yes | 4+ | 200-400k | `ISSUES_SCHEMA` |
| 2b. Domain Split | If large | 2-5/domain | 150-250k/domain | `ISSUES_SCHEMA` |
| 3. Comparative | If 2+ projects | 2 | 80-200k | `DIFF_SCHEMA`, `MIGRATION_SCHEMA` |
| 4. Synthesis | Yes | 6+ | 100-200k | `SYNTHESIS_SCHEMA` + Report |

---

## 4. Workflow Patterns

### Pipeline (Default)

```
FIND --> VERIFY --> DEDUP --> RANK --> WRITE
```

- **FIND:** Dimension agents produce raw findings.
- **VERIFY:** Verifier agents attempt to refute CRITICAL/HIGH findings.
- **DEDUP:** Synthesizer merges overlapping findings.
- **RANK:** Synthesizer sorts by severity, blast radius, urgency.
- **WRITE:** Report Writer produces the deliverable.

### Parallel Between Stages

```
Phase 2:
  [Quality Agent]  ---\
  [Security Agent] ----+--> MERGE --> Phase 4
  [Arch Agent]     ---/
  [Test Agent]     ---/
```

No agent waits for another agent within the same phase.

### Barriers ONLY Between Phases

```
Phase 1 complete --> Barrier: schemas valid, LOC known
Phase 2 complete --> Barrier: >=3/4 dimensions returned, CRITICALs have evidence
Phase 3 complete --> Barrier: diff valid, migration ranked
Phase 4 complete --> Barrier: CRITICALs verified, completeness checked
```

Early exit: If Phase 2 produces 0 CRITICAL and 0 HIGH findings, skip adversarial verification.

### Adversarial Verify

For each CRITICAL/HIGH finding:

1. Spawn 3 independent Verifier agents (same prompt, different reasoning seeds).
2. Each votes CONFIRMED or REFUTED.
3. Finding survives if >=2/3 vote CONFIRMED.
4. REFUTED findings are marked `disputed` and included with a note.

```
CRITICAL finding --> Verifier A --> CONFIRMED
                  --> Verifier B --> REFUTED
                  --> Verifier C --> CONFIRMED
                  --> Result: CONFIRMED (2/3)
```

### Loop-Until-Dry

For large codebases, iterate discovery:

```
Round 1: Spawn all dimension agents
Round 2: If new areas revealed, spawn targeted agents
Round K: 0 new findings for 2 consecutive rounds --> STOP
```

### Completeness Critic

A final meta-agent that critiques the process:

- "You checked source code but never looked at the build pipeline."
- "This is a plugin system but no agent examined the plugin API surface."
- "The project uses threading but no concurrency issues were flagged."

Output appended to the final report as "Coverage Notes."

---

## 5. Quality Gates

### Gate 1: Evidence Required

| Severity | Evidence Standard |
|---|---|
| CRITICAL | `file:line` + code snippet + verified by >=2/3 verifiers |
| HIGH | `file:line` + code snippet |
| MEDIUM | `file:line` (snippet optional) |
| LOW | `file` or module name |

### Gate 2: Fix Required

Every CRITICAL and HIGH finding must include a **concrete suggested fix**.

Invalid: "Improve error handling."
Valid: "Wrap the `json.loads()` call at `lucnhan/core/config.py:47` in a try/except that catches `json.JSONDecodeError` and raises `ConfigError` with the file path."

### Gate 3: Migration Specificity

Every migration candidate must specify which files to change, what to add/remove/modify, risk of the change, and what must NOT be broken.

### Gate 4: No Vague Action Items

Banned: "Improve test coverage", "Refactor this module", "Add error handling", "Fix security issues", "Clean up code" -- all without specifics.

### Gate 5: Language Compliance

Executive summary in user's `output_language`. Technical findings in English for precision.

---

## 6. Token Budget Guidance

### Total Budget by Project Size

| Project Size | LOC Range | Total Tokens | Phase Breakdown |
|---|---|---|---|
| Small | <5k | 150-300k | P1:20k + P2:100k + P4:80k |
| Medium | 5-30k | 400-800k | P1:40k + P2:300k + P3:100k + P4:200k |
| Large | >30k | 1-2M | P1:80k + P2:500k + P2b:300k + P3:200k + P4:400k |

### Budget Allocation

| Allocation | % | Purpose |
|---|---|---|
| Discovery | 10% | Phase 1 baseline |
| Analysis | 50% | Phase 2 (and 2b if needed) |
| Comparison | 15% | Phase 3 (if applicable) |
| Synthesis + Verify | 20% | Phase 4 including adversarial verify |
| Overhead | 5% | Orchestration, dedup, retries |

### Cost Controls

- If a dimension agent exceeds 25% of total budget, halt and use what it has.
- If total spend exceeds budget, verify only CRITICAL findings (skip MEDIUM/LOW verify).
- Completeness critic always runs (~5k tokens).

### Model Selection

| Agent Type | Recommended Model | Rationale |
|---|---|---|
| Scanners | Sonnet-equivalent | Broad file reading, pattern matching |
| Verifiers | Sonnet-equivalent | Careful evidence checking |
| Synthesizer | Opus-equivalent (if available) | Cross-referencing and judgment |
| Report Writer / Critic | Sonnet-equivalent | Good prose, breadth awareness |

---

## 7. Anti-Patterns to Avoid

| # | Anti-Pattern | Problem | Fix |
|---|---|---|---|
| AP-1 | Findings without `file:line` evidence | Unverifiable, not actionable | Every finding must cite file:line with actual code |
| AP-2 | "Improve test coverage" as action item | Which files? Which cases? | Specify: "Add tests for `crypto.py` covering key rotation, invalid format" |
| AP-3 | Skipping verify on CRITICALs | LLMs hallucinate findings | Always run 3-verifier protocol on CRITICAL and HIGH |
| AP-4 | Comparing before understanding | Diff misattributes without context | Always run Phase 1+2 before Phase 3 |
| AP-5 | Quick wins dominating report | CRITICALs must come first | Executive summary leads with CRITICALs regardless of count |
| AP-6 | Single-pass on large codebases | Hidden dependencies | Use loop-until-dry pattern |
| AP-7 | Ignoring tech stack specifics | Each language has unique concerns | Phase 1 identifies stack; Phase 2 agents get stack-specific instructions |
| AP-8 | Over-abstracting ("refactor module") | User cannot act on it | Break into specific file:line findings with concrete fixes |

---

## 8. Reusability Notes

### Schema Reusability

All schemas are language-agnostic (relative paths, line numbers, generic categories).
Adapt per language:

| Language | Extra Categories |
|---|---|
| Python | `GIL`, `decorator_abuse`, `pickle_insecurity` |
| Rust | `unsafe_block`, `lifetime_issue`, `borrow_violation` |
| Go | `goroutine_leak`, `nil_pointer`, `error_unwrap` |
| TypeScript/JS | `any_type`, `prototype_pollution`, `npm_audit` |
| Java/Kotlin | `null_safety`, `resource_leak`, `serialization` |

### Phase Reusability

The 4-phase structure works for any project without modification:
1. **Discovery** -- file counting, LOC, tech stack. Universal.
2. **Deep Analysis** -- quality, security, architecture, testing. Universal dimensions.
3. **Comparative** -- structural diff. Universal.
4. **Synthesis** -- dedup, verify, rank, write. Universal.

### Domain-Specific Extensions

| Domain | Extension | Additional Agents |
|---|---|---|
| UI-heavy projects | Phase 2.5 | Visual reviewer (screenshots, design tokens, accessibility) |
| Security-critical (payments, medical) | Phase 2 extra | Threat model agent (STRIDE, attack surface) |
| API-first projects | Phase 2 extra | Contract tester (OpenAPI compliance, versioning) |
| Embedded/IoT | Phase 2 extra | Hardware interface reviewer (memory safety, interrupts) |
| ML/AI projects | Phase 2 extra | Data pipeline reviewer (drift, metrics, bias) |

### Customization Points

1. **`focus_dimensions`** -- which dimensions to analyze
2. **`known_pain_areas`** -- inject user knowledge
3. **`risk_appetite`** -- adjust severity thresholds
4. **`output_language`** -- localized executive summary
5. **Phase 2b domain split** -- customize per project structure
6. **Verifier count** -- 5 for security audits, 1 for quick checks
7. **Loop-until-dry threshold** -- increase K for thorough audits

---

## 9. Worked Example (Luc Nhan)

> **Note**: §9 là **snapshot minh họa** từ đợt đánh giá 2026-06, KHÔNG phải state hiện hành
> của project (LOC, số commit ahead, git state có thể đã đổi). Workflow (§1-8) vẫn chính xác
> và tái sử dụng được.

### 9.1 Project Context

| Property | Value |
|---|---|
| Project | Luc Nhan -- reverse-engineering agent for IDA Pro with multi-provider LLM |
| Language | Python 3.11+ |
| Source LOC | ~45,000 across 171 files |
| Test LOC | ~18,700 across 74 files |
| Source:test ratio | ~2.4:1 (healthy) |
| Remotes | `origin` (EliteClassRoom/Luc-Nhan), `tuna-main` (tuna1999/Rikugan) |
| Modules | `agent/`, `core/`, `providers/`, `ui/`, `ida/`, `skills/`, `mcp/`, `cli/`, `state/`, `plans/` |
| Tooling | ruff (lint), mypy (types, partial strict), `ci-local.sh` |
| Key feature | 60+ IDA tools, exploration mode with subagents, deobfuscation, headless mode, MCP |

### 9.2 Inputs

```
PROJECT_PATHS: ["D:/re_dev_projects/vibe-clone/lucnhan"]
PROJECT_NAMES: ["current"]
FOCUS_DIMENSIONS: ["quality", "security", "architecture", "testing"]
RISK_APPETITE: medium
OUTPUT_LANGUAGE: vi
SKIP_PHASES: [3]
```

### 9.3 Phase 1: Discovery Results

**Agents:** 2 (Project Scanner, Git State Scanner)

| Metric | Value |
|---|---|
| Total source LOC | 45,118 |
| Total test LOC | 18,685 |
| Largest files | `lucnhan/agent/system_prompt.py` (prompt construction, likely >800 lines) |
| Tech stack | Python 3.11, IDA Pro SDK, Qt (PySide/Shiboken), ruff, mypy |
| CI | Local only (`ci-local.sh`); no GitHub Actions |
| Type checking | Partial: only `lucnhan.core.*` and `lucnhan.providers.*` have `disallow_untyped_defs` |
| Git state | Clean tree, `master` branch, **23 commits ahead** of `tuna-main/main`, 0 behind |
| Recent activity | Binary Ninja removal, theme system, OpenAI/Anthropic provider updates |

**Key insight:** The fork (`tuna-main`) has no independent commits. All change flows from this repo.

### 9.4 Phase 2: Deep Analysis Results

**Agents:** 4 (Quality, Security, Architecture, Testing) running in parallel.

**Sample Findings:**

| ID | Sev | Dim | File | Description |
|---|---|---|---|---|
| Q-001 | MEDIUM | quality | `lucnhan/agent/system_prompt.py` | Large prompt construction file (>800 lines) |
| Q-002 | LOW | quality | `lucnhan/core/config.py` | Numeric thresholds without named constants |
| Q-003 | MEDIUM | quality | `lucnhan/ui/*.py` | UI modules excluded from strict mypy |
| S-001 | HIGH | security | `lucnhan/providers/auth_cache.py` | OAuth token caching on disk -- verify encryption at rest |
| S-002 | MEDIUM | security | `lucnhan/core/sanitize.py` | Sanitizer correctness for user-controlled strings |
| S-003 | MEDIUM | security | `lucnhan/core/logging.py` | Ensure LLM API keys are not logged |
| A-001 | MEDIUM | architecture | `lucnhan/ui/chat_view.py` | UI component directly importing agent internals |
| A-002 | LOW | architecture | `lucnhan/ida/` | IDA stubs may contain unused declarations (intentional) |
| A-003 | MEDIUM | architecture | `lucnhan/state/` | Global state patterns need thread-safety audit |
| T-001 | HIGH | testing | Provider modules | Per-provider test coverage may be incomplete |
| T-002 | MEDIUM | testing | `tests/agent/` | Agent tests depend on LLM response mock quality |
| T-003 | LOW | testing | `tests/core/test_thread_safety.py` | Some test names could be more descriptive |

### 9.5 Phase 3: Comparative (Skipped)

Skipped per configuration. The discovery phase established that `tuna-main/main` is
23 commits behind with 0 unique commits, so no meaningful comparison exists.

### 9.6 Phase 4: Synthesis Results

**Agents:** Synthesizer + Report Writer + 3 Verifiers + Completeness Critic

**Deduplication and Cross-Referencing:**

| Merged | Root Cause | Recommended Action |
|---|---|---|
| A-001 + Q-003 | UI-agent boundary lacks abstraction | Extract `ChatViewModel` mediator between UI and agent loop |
| S-001 + S-003 | Security practices implicit, not enforced | Add security lint rules; audit `auth_cache` encryption |
| Q-002 + A-003 | Configuration and state lack type discipline | Extend `disallow_untyped_defs` to all modules incrementally |

**Health Score:** 72/100

Rationale: Strong test ratio (2.4:1), active development, good tooling (ruff, mypy).
Deductions: partial type coverage, UI-agent coupling, implicit security practices.

**Executive Summary (Vietnamese):**

> Dự án Luc Nhan có sức khỏe tổng thể ở mức tốt (72/100) với bộ kiểm thử đáng nể
> (tỷ lệ source:test khoảng 2.4:1) và công cụ phát triển hiện đại (ruff, mypy).
> Ba rủi ro chính: (1) ranh giới UI-agent thiếu lớp trừu tượng, dẫn đến coupling cao;
> (2) bảo mật (mã hóa token, logging) chưa được thực thi hệ thống; (3) type safety
> chỉ áp dụng một phần. Hành động ưu tiên: tách interface UI-agent, mở rộng mypy
> strict, kiểm toán auth_cache.

**Token Budget Actuals:**

| Phase | Estimated | Actual |
|---|---|---|
| Phase 1 | 30-60k | ~45k |
| Phase 2 | 200-400k | ~350k |
| Phase 4 | 100-200k | ~150k |
| **Total** | **330-660k** | **~545k** |

### 9.7 Adaptation Notes for Other Project Types

| Project Type | Phase 2b Domains | Extra Agents |
|---|---|---|
| Web frontend | Components, State, Routing, API | Accessibility, Visual regression |
| API backend (Go/Rust) | Handlers, Middleware, DB, Auth | Threat model, Contract test |
| Mobile (Flutter/Swift) | UI, Navigation, Platform, State | Device-specific testing |
| Data pipeline | Ingestion, Transform, Storage, ML | Data quality, Bias audit |

---

## Appendix A: Quick-Start Checklist

- [ ] Project path(s) are accessible and readable
- [ ] Git is installed and repositories are valid (if comparing)
- [ ] Token budget is set appropriately for project size
- [ ] Output language is specified
- [ ] Focus dimensions are selected (or use defaults: all four)
- [ ] Known pain areas are noted (or leave empty for unbiased scan)

## Appendix B: Schema Quick Reference

| Schema | Phase | Purpose |
|---|---|---|
| `DISCOVERY_SCHEMA` | 1 | Factual project baseline |
| `ISSUES_SCHEMA` | 2 | Ranked findings per dimension |
| `DIFF_SCHEMA` | 3 | Structural comparison of two projects |
| `MIGRATION_SCHEMA` | 3 | Ranked migration candidates |
| `SYNTHESIS_SCHEMA` | 4 | Deduplicated, verified, cross-referenced findings |

## Appendix C: Finding ID Convention

```
Dimensions: Q=quality, S=security, A=architecture, T=testing
Raw: Q-001, S-001, A-001, T-001, ...
Canonical (post-dedup): C-001, C-002, ...
```

## Appendix D: Severity Definitions

| Severity | Definition | Response Time |
|---|---|---|
| CRITICAL | Security vulnerability, data loss, or broken core functionality | 24 hours |
| HIGH | Bug or significant quality issue affecting reliability | 1 week |
| MEDIUM | Maintainability concern or non-critical quality issue | Current sprint |
| LOW | Style, naming, or minor improvement | When convenient |

---

*Document version: 1.0. Self-contained and runnable on any software project.*

# Development Guide

> If you are a coding agent, please read [AGENTS.md](AGENTS.md) instead.

This document is for human contributors. It covers how to set up a development environment, the branch workflow, and what to do before opening a PR.

---

## Prerequisites

- **IDA Pro 9.x**
- **Python 3.10–3.11** recommended (see note below on IDA Pro + Python versions)
- **Git**
- An API key for at least one supported LLM provider (Anthropic, OpenAI, Google, or a local Ollama instance)

> **IDA Pro note:** Python 3.10 is the safest choice. Higher versions may trigger a Shiboken UAF crash during Qt signal dispatch. See the IDA API Notes section of AGENTS.md for details.

---

## Installation (Development)

Clone the repo and symlink it into the IDA plugin directory so changes take effect on the next launch without reinstalling.

**IDA Pro**

```bash
# macOS / Linux
git clone https://github.com/EliteClassRoom/rikugan
ln -s "$(pwd)/rikugan" ~/.idapro/plugins/rikugan

# Windows (run as Administrator)
mklink /D "%APPDATA%\Hex-Rays\IDA Pro\plugins\rikugan" "<full path to cloned repo>"
```

---

## Python Dependencies

Install the runtime dependencies into the Python environment used by your host:

```bash
pip install anthropic>=0.39.0 openai>=1.50.0 google-genai>=1.0.0 tomli>=2.0.0
```

For development tooling (CI checks, running tests locally):

```bash
pip install ruff mypy pytest desloppify
```

---

## Branch Workflow

```
feat/my-thing  ─┐
fix/some-bug   ─┤──► master
chore/deps     ─┘
```

> **Lưu ý fork:** Fork này dùng `master` làm branch chính. Upstream (`tuna-main` remote) dùng mô hình `dev → main`. Khi port từ upstream, branch của nó là `dev`/`main`.

1. Branch off `master` using a descriptive prefix:
   - `feat/` — new feature
   - `fix/` — bug fix
   - `refactor/` — code restructure, no behavior change
   - `chore/` — deps, tooling, docs
2. Make your changes in small, focused commits
3. Run the local CI script (see below) before pushing
4. Open a PR targeting `master`
5. Once reviewed, it gets merged to `master`
6. Releases are cut from `master` with a version tag

**Lưu ý CI trigger:** `.github/workflows/ci.yml` trigger trên `branches: [main, dev]` (từ upstream) — push/PR lên `master` **không** kích CI. Luôn chạy `./ci-local.sh` trước.

---

## Before Pushing — Local CI Check

Run this script after every feature or fix, before opening a PR:

```bash
./ci-local.sh
```

This mirrors exactly what GitHub Actions runs. It will catch formatting errors, lint issues, type errors, test failures, and code quality regressions before they reach CI.

If ruff reports formatting issues, auto-fix them:

```bash
./ci-local.sh --fix
```

The script installs `ruff` and `mypy` if they are not already available. It skips steps whose tools are missing rather than failing hard, so it is safe to run in a partial environment.

---

## Running Tests

```bash
python3 -m pytest tests/ -v
```

Tests are organized under `tests/` by subsystem:

```
tests/
├── agent/       # Agent loop, plan mode, exploration, session
├── core/        # Config, sanitize, errors, profile, logging
├── providers/   # All LLM providers
├── tools/       # Tool implementations (IDA, shared)
└── mocks/       # ida_mock — stubs the IDA Pro API for testing outside IDA
```

IDA Pro APIs are stubbed at test time — you do not need IDA installed to run the test suite.

---

## Code Quality

This project uses [desloppify](https://github.com/peteromallet/desloppify) to track codebase health. The current objective score is **89.0/100** (target: 95).

Run a scan locally at any time:

```bash
desloppify scan
desloppify status   # score dashboard
desloppify issues   # work queue of findings
```

The `desloppify review` command (subjective scoring) uses an LLM and is run manually before releases, not on every change.

**Python version note:** desloppify's AST-based detectors are sensitive to the Python version running the scan. GitHub Actions uses Python 3.11 (~89.4 score). Different local versions will yield slightly different scores — the 0.5-point baseline gap is intentional to absorb this variance. For consistent local results, install `uv`; the `.python-version` file in the repo root pins to 3.11 and `ci-local.sh` will use it automatically.

```bash
pip install uv                   # install uv once
uv add desloppify --dev          # add desloppify (ci-local.sh does this automatically)
```

---

## Commit Style

```
feat(agent): add streaming cancellation for plan mode
fix(ida): handle missing function at cursor gracefully
refactor(providers): extract retry logic into base class
security: strip homoglyph sequences in sanitize.py
docs: update tool registration guide in AGENTS.md
```

Format: `type(scope): short description`
- One logical change per commit
- Scope is the subsystem: `agent`, `ida`, `ui`, `providers`, `mcp`, `skills`, `core`

---

## Release Process

Pipeline release đầy đủ tự động:

1. Bump `version` trong `ida-plugin.json`
2. Commit + push:
   ```bash
   git add ida-plugin.json
   git commit -m "chore: bump version to 1.x.x"
   git push origin master
   ```
3. Tag và push:
   ```bash
   git tag v1.x.x
   git push origin v1.x.x
   ```
4. GitHub Actions tự chạy (verify → build → publish). Release xuất hiện tại `https://github.com/EliteClassRoom/rikugan/releases/tag/v1.x.x` với 2 artifact: `rikugan-v1.x.x.zip` + `SHA256SUMS`.

**Install artifact** (HCLI): `curl -L https://github.com/EliteClassRoom/rikugan/releases/download/v1.x.x/rikugan-v1.x.x.zip -o rikugan.zip` rồi `hcli plugin install rikugan.zip`. ZIP phẳng theo spec Hex-Rays.

**Re-run**: Actions tab → workflow "Release" → "Run workflow" → nhập tag.

**Pre-release**: thêm suffix `-rc1`, `-beta1`, `-dev1` → GitHub tự đánh dấu pre-release.

**Local dry-run** (test trước khi tag):
```bash
python scripts/build_release.py --version 1.x.x-test --out-dir /tmp/rikugan-test
unzip -l /tmp/rikugan-test/rikugan-v1.x.x-test.zip   # ida-plugin.json phải ở gốc
python scripts/validate_archive.py /tmp/rikugan-test/rikugan-v1.x.x-test.zip
rm -rf /tmp/rikugan-test
```

**Smoke test pipeline** (dùng suffix `-rc1` → GitHub đánh dấu pre-release, không phải stable):
```bash
git tag v0.0.0-rc1 && git push origin v0.0.0-rc1
# → check Actions tab: 3 jobs xanh
git push origin :refs/tags/v0.0.0-rc1
gh release delete v0.0.0-rc1 --repo EliteClassRoom/rikugan --yes
```

---

## Developing Headless Mode

Headless mode lets you run Rikugan inside ``idat.exe`` (Windows) / ``idat64`` (Linux/macOS) without the Qt GUI.

### Quick Smoke Commands

```bash
# Set IDA_PATH to the correct executable
export IDA_PATH="E:/ida pro 9.2/idat.exe"  # Windows (preferred for IDA 9.x headless)
# or
export IDA_PATH="/path/to/idat64"           # Linux/macOS

# One-shot mode — run a single prompt and get JSON output
python -m rikugan.cli.headless ask /path/to/sample.exe "summarize binary metadata"

# Server mode — start a control server
python -m rikugan.cli.headless serve /path/to/sample.exe --ready-file rikugan-ready.json

# Read the ready-file to get URL and token
cat rikugan-ready.json

# Health check
curl http://127.0.0.1:<PORT>/health

# Submit a prompt
curl -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"prompt":"decompile function 0x401000"}' \
     http://127.0.0.1:<PORT>/prompt

# Stream events
curl -H "Authorization: Bearer <TOKEN>" \
     "http://127.0.0.1:<PORT>/events?run_id=<RUN_ID>&index=0"

# Cancel
curl -X POST -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"run_id":"<RUN_ID>"}' \
     http://127.0.0.1:<PORT>/cancel

# Shutdown
curl -X POST -H "Authorization: Bearer <TOKEN>" \
     http://127.0.0.1:<PORT>/shutdown
```

### IDA_PATH Discovery

The CLI discovers the IDA executable in this order:
1. ``--ida /path/to/ida`` flag
2. ``IDA_PATH`` environment variable
3. Common install paths on each platform
4. ``PATH`` (searches for ``idat.exe`` > ``idat64.exe`` on Windows;
   ``idat`` > ``idat64`` on Linux/macOS)

Note: ``idat.exe`` is the console/headless executable for IDA 9.x on Windows.
The GUI executable ``ida64.exe`` is **not** used for headless mode.
``idat64.exe`` is retained as a legacy fallback for older IDA installs.

### Path-with-Spaces Testing

On Windows, IDA paths and binary paths often contain spaces. Always test with quoted paths:

```bash
python -m rikugan.cli.headless ask \
  "C:/Path With Spaces/sample.exe" \
  "summarize metadata" \
  --ida "E:/ida pro 9.2/idat.exe"
```

### Cancellation

- One-shot: ``Ctrl+C`` sends ``SIGINT``, triggering ``KeyboardInterrupt`` in the runner, which returns exit code 6 (``EXIT_CANCELLED``).
- Server: ``curl -X POST -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{"run_id":"<RUN_ID>"}' http://127.0.0.1:<PORT>/cancel`` cancels the active agent run. The run finishes cleanly (exit code 6).

### Provider Errors

- If the provider config is missing or invalid, ``start_agent()`` returns an error string. The runner returns exit code 4 (``EXIT_CONFIG_ERROR``).
- If the provider returns a 4xx error during streaming, ``AgentLoop`` retries with backoff. After max retries, ``ERROR`` events are emitted and exit code 5 (``EXIT_TOOL_FAILURE``) is returned.

### Shutdown Checks

- One-shot: IDA exits via ``idc.qexit(code)`` after writing results. Verify IDA releases its license.
- Server: ``POST /shutdown`` calls ``idc.qexit(0)`` after a 200ms delay to allow the response to be sent.
- The CLI direct-bootstrap path is the primary headless entry point; the plugin-based RIKUGAN_HEADLESS env var path is not supported for production use.

### /events JSON Envelope

All ``/events`` responses use a JSON envelope:

```json
{
  "events": [ ... ],
  "index": 42,
  "finished": false,
  "exit_code": 0,
  "final_text": ""
}
```

- ``events``: list of event objects since the requested ``index``.
- ``index``: the highest seen sequence number (monotonically increasing).
- ``finished``: true when the run has completed or when no active run exists.
- ``exit_code``: set when ``finished`` is true (0 for success, 6 for cancelled, etc.).
- ``final_text``: the concatenated message text, available after ``finished`` is true.

### Strict Run ID Semantics

- **Mutation endpoints** (``/answer``, ``/tool-approval``, ``/approval``, ``/cancel``): a missing, stale (mismatched), or finished ``run_id`` always returns a **4xx error** with no side effects.
- **``/events`` polling**: a stale or mismatched ``run_id`` returns a **200 response** with ``{"events": [], "index": 0, "finished": true, "exit_code": 0, "final_text": ""}``. This allows clients polling an old run ID to gracefully detect it has been replaced or completed without error retry loops.
- **``/prompt``**: a new prompt **replaces** the active run if the previous run has finished (``state.is_idle`` is True). If a run is still active, ``/prompt`` returns 409.
- **Finished-run retention**: a finished run remains in the ``RunState`` (so late ``/events`` polls see the finished envelope) until the next ``/prompt`` replaces it or the server shuts down.

### Bootstrap Mechanics

The CLI generates a **direct ``-S`` bootstrap script** that adds the repo root to ``sys.path``, imports ``rikugan.ida.headless_bootstrap``, and calls ``main()``. Configuration is passed via a **temp JSON file** referenced by the ``RIKUGAN_HEADLESS_BOOTSTRAP`` environment variable — never via ``-S`` command-line arguments (which have fragile quoting on Windows).

### Ready-File (Serve Mode)

In serve mode, the bootstrap writes a **ready file** (``rikugan-ready.json`` by default, or user-specified via ``--ready-file``) containing the server URL and auth token:

```json
{"url": "http://127.0.0.1:14913", "token": "abc123..."}
```

If no ``--ready-file`` is provided, the CLI creates an **internal temporary ready file** and polls it until the server is up.

### No Default CORS

The control server **does not** add CORS headers by default. All requests are expected to originate from the local machine. If cross-origin access is needed, a reverse proxy should provide CORS headers.

### Security Notes

- The control server **binds to 127.0.0.1** by default. ``--host 0.0.0.0`` is blocked.
- All non-``/health`` endpoints require a ``Bearer <TOKEN>`` Authorization header.
- The auth token is a 64-character hex string, auto-generated if not provided.
- ``/health`` exposes only ``{"status": "ok"}`` — no paths, tokens, or configuration.
- ``execute_python`` is **never** auto-approved, even in headless mode.

## Getting Help

- Read [AGENTS.md](AGENTS.md) for deep technical documentation on internals, architecture decisions, and coding rules
- Open an issue at https://github.com/EliteClassRoom/rikugan/issues

# Luc Nhan

![Version](https://img.shields.io/badge/version-1.13.3-blue)
![License](https://img.shields.io/badge/license-MIT-green)

A reverse-engineering agent for **IDA Pro** that integrates a multi-provider LLM directly into your analysis UI. Luc Nhan has its own agentic loop, in-process tool orchestration, streaming chat, multi-tab sessions, subagents, MCP, headless automation, and undoable mutation workflows — all built around the binary you are reversing.

> *Luc Nhan is the public display name for this project. The plugin metadata, package imports, repository URL, and memory filename still use the legacy `Rikugan` identifier until a separate migration is performed.*

![Luc Nhan in IDA Pro](assets/ida_showcase.png)

[Documentation](https://rikugan.reversing.codes/docs.html) | [Architecture](https://rikugan.reversing.codes/ARCHITECTURE.html) | [Changelog](CHANGELOG.md) | [Issues](https://github.com/EliteClassRoom/rikugan/issues)

## Install

Auto-detects IDA Pro.

**Linux / macOS:**

```bash
curl -fsSL https://raw.githubusercontent.com/EliteClassRoom/rikugan/master/install.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/EliteClassRoom/rikugan/master/install.ps1 | iex
```

> The installer targets the `master` branch.

Pre-built release ZIPs (HCLI flat layout) are published on the GitHub Releases page and can be installed with:

```bash
hcli plugin install rikugan-v1.10.1.zip
```

For host-specific install, manual setup, and configuration, see the [docs](https://rikugan.reversing.codes/docs.html).

## Is this another MCP client?

No, Luc Nhan is an ***agent*** built to live inside your RE host. It does not consume an MCP server to interact with the host database; it has its own agentic loop, context management, role prompt ([source](rikugan/agent/system_prompt.py)), and an in-process tool orchestration layer.

The agent loop is a generator-based turn cycle: each user message kicks off a stream→execute→repeat pipeline where the LLM response is streamed token-by-token and tool calls are intercepted and dispatched. It supports automatic error recovery, mid-run user questions, plan mode for multi-step workflows, and message queuing — all without leaving the disassembler.

The agent really ***lives*** and ***breaths*** reversing.

- No need to switch to an external MCP client
- Assistant-first, not designed to do your job (unless you ask it to)
- Extensible to many LLM providers and local installations (Ollama)
- Quick to enable — just hit Ctrl+Shift+I and the chat will appear

## Features

**Native IDA agent loop** — Streaming token-by-token responses, intercepted tool calls, automatic retries with backoff, mid-run cancellation, and queued follow-up messages. Pseudo-tools (`exploration_report`, `phase_transition`, `save_memory`, `spawn_subagent`) are handled inline.

**60+ native tools** covering navigation, functions, database (segments, imports, exports), strings, xrefs, disassembly, decompiler, annotations, types, microcode, scripting, and IDAPython docs lookup. The agent always asks permission before running scripts and will never execute the target binary. Full tool reference in the [docs](https://rikugan.reversing.codes/docs.html).

**Multi-tab sessions with persistent memory** — Each tab is an independent conversation with its own token tracking. Findings are saved to a per-binary central memory workspace (SQLite structured facts + `MEMORY.md` manual notes under your Luc Nhan user directory), persisting across sessions and re-injected into future prompts.

**Plan / explore / modify workflows** — `/plan`, `/modify`, and `/explore` enter structured workflows with approval gates. Every mutating tool records a `MutationRecord` so changes can be undone with `/undo` or via the Mutation Log panel.

**Subagents and orchestration** — The orchestrator maps the binary (imports, exports, strings, key functions), then spawns isolated subagents to analyze in parallel. Each reports back, and the orchestrator synthesizes a complete picture. Experimental — temporarily disabled pending shared execution-policy and context-isolation hardening.

|![alt text](assets/subagents_example_3.png)|
|:--:|
|Orchestrator spawning subagents in parallel|

**Tools Panel** — A slide-out panel with three tabs: Agents (live subagent tree), A2A Bridge for delegating tasks to external A2A-compatible agents or local CLI agents (Claude Code, Codex), and Knowledge (memory, entities, relations, observations). The Knowledge tab is the default view so opening Tools does not eagerly enumerate every function in the binary.

**Offline IDAPython docs** — A bundled copy of the Hex-Rays Python reference (`rikugan/data/idapython-docs/`, 54 modules) backs the `lookup_idapython_doc` tool. API verification prefers the offline bundle; web fetch against Hex-Rays is strictly a last resort.

**Natural Language Patches** (Experimental) — `/modify` lets you describe what you want changed in plain English. Luc Nhan explores the binary, builds context, and applies the patches.

|![alt text](assets/maze_solve.gif)|
|:--:|
|`/modify make this maze game easy, let me pass through walls`|

**Deobfuscation** (Experimental) — The `/deobfuscation` skill activates plan mode to recognize and remove control flow flattening, opaque predicates, MBA expressions, and junk code using microcode read/write primitives.

|![](assets/cff_remove_example.gif)|
|:--:|
|~3x speed of the workflow, original process took ~4:30 min|

**Headless Mode** — Run Luc Nhan inside `idat.exe` (Windows) / `idat64` (Linux/macOS) without Qt via one-shot prompts or a local HTTP control server bound to `127.0.0.1` with bearer-token authentication. Ideal for CI/CD pipelines and batch analysis.

**Skills & MCP** — 11 built-in skills, custom skill support (Markdown files with YAML frontmatter), and MCP server integration. Reuse skills and MCP servers from Claude Code and Codex.

| Skill | Purpose |
| ------- | --------- |
| `/ctf` | Capture-the-flag reverse engineering — find the flag efficiently |
| `/deobfuscation` | Remove control flow flattening, opaque predicates, MBA expressions, junk code |
| `/driver-analysis` | Windows kernel driver analysis — DriverEntry, dispatch table, IOCTL handlers |
| `/generic-re` | General-purpose binary analysis — understand functionality, architecture, and behavior |
| `/ida-scripting` | Write IDAPython scripts — verifies IDA 9.x APIs against the offline docs bundle first, web fetch against Hex-Rays only as a last resort |
| `/elf-malware-analysis` | Linux ELF malware analysis — packing, persistence, C2, rootkits, MITRE ATT&CK |
| `/malware-analysis` | Windows PE malware analysis — kill chain, IOC extraction, MITRE ATT&CK |
| `/modify` | Modify binary behavior using natural language — explore, plan, patch, save |
| `/naming-convention` | Apply consistent naming rules across the binary (PascalCase functions, lowercase fields, etc.) |
| `/smart-patch-ida` | Patch binary code in IDA Pro using natural language — read, assemble, write, verify |
| `/vuln-audit` | Binary vulnerability audit — taint analysis, buffer overflows, format strings |

### Profiles

Profiles let you customize the agent to fit your analysis needs. They give you granular control over which data the LLM can read, restrict which tools it can use, and let you define custom rules to filter data.

![alt text](assets/profile.png)

## Recommended Providers

| Provider | Notes |
| ---------- | ------- |
| **MiniMax M3** | Recommended for strong malware reverse engineering and long session context thanks to a 1M-token context window, 524,288-token output limit, and automatic adaptive thinking. Low cost, generous limits. |
| **Claude Opus / Sonnet** | Strong reasoning and prompt caching. OAuth (Claude Pro/Max) and API key both supported. |
| **Codex (GPT-5.x)** | OpenAI's Codex backend via ChatGPT OAuth device flow. Use the in-plugin "Setup Codex" button to authenticate. |
| **Gemini** | Useful alternative with thought-signature support. |
| **OpenAI-compatible / Ollama** | Custom endpoints and local models. |

Also supports Anthropic, OpenAI, Google Gemini, Ollama, and MiniMax as registered providers.

## Requirements

- IDA Pro **9.0+** (ships PySide6 / Qt6; PyQt5 is not used) with Hex-Rays decompiler recommended
- Python **3.11+** for repository tooling and the standalone headless CLI; the IDA-embedded Python version is determined by your IDA install
- At least one LLM provider
- Windows, macOS, or Linux

> **IDA Pro + Python:** Shiboken has a known Use-After-Free bug triggered when Python ≥ 3.14 imports C-extension modules during Qt signal dispatch. Luc Nhan mitigates this by routing all `ida_*` imports through `importlib.import_module()`. Python **3.10** remains the safest choice for the IDA-embedded runtime; higher versions may still work with the mitigations in place. See the [upstream report](https://community.hex-rays.com/t/ida-9-3-b1-macos-arm64-uaf-crash/646).

## Headless Mode

Luc Nhan supports running inside `idat.exe` (Windows) / `idat64` (Linux/macOS) without the Qt GUI. Two modes are available:

**One-shot** — the agent receives a single prompt, processes it to completion, and exits:

```bash
python -m rikugan.cli.headless ask sample.exe "summarize metadata"
```

**Server** — a local HTTP control server binds to `127.0.0.1` (never `0.0.0.0`) and accepts authenticated requests on `/prompt`, `/events`, `/tool-approval`, `/answer`, `/cancel`, `/shutdown`, and `/health`. All non-health endpoints require a bearer token emitted only to the ready-file and startup stdout:

```bash
python -m rikugan.cli.headless serve sample.exe
```

External clients can stream progress, approve tool executions, answer agent questions, and cancel or shut down runs over plain HTTP with SSE-style event polling. **`execute_python` is never auto-approved in headless mode** — even in one-shot, any approval-required event returns exit code 7 (`EXIT_APPROVAL_REQUIRED`) rather than silently approving.

## Security and Trust Boundaries

Luc Nhan runs inside a reverse-engineering environment processing **adversarial binaries**. Every data path from the binary to the model is an attack surface.

- **Binary content is untrusted.** Strings, function names, decompiled code, and comments are sanitized (injection-marker stripping, delimiter quoting, length capping) before they enter a prompt. Persistent memory writes are also stripped.
- **MCP and external agent outputs are untrusted.** They are wrapped with the strongest preamble instructing the model to treat them as data, not instructions.
- **`execute_python` requires explicit user approval.** A blocklist rejects `subprocess`, `os.system`, `os.exec*`, `os.spawn*`, `Popen`, and `__import__("subprocess")` before the prompt is shown. There is no auto-approve mode.
- **The target binary is not executed by the agent.** Script execution is captured (`stdout`/`stderr` redirected to `StringIO`) and runs in a controlled namespace.
- **The control server binds to `127.0.0.1` only.** Bearer-token authentication is required for all non-`/health` endpoints. Tokens appear only in the ready-file and startup stdout, never in log output.

See [ARCHITECTURE.md](ARCHITECTURE.md) for full technical details on the agent loop, event system, mutation tracking, and internal data flows.

## Conclusion

Luc Nhan started as a personal experiment to see whether agentic LLMs could meaningfully assist with reverse engineering. After several months of iteration it has grown into a stable companion that lives directly inside the disassembler — with native tool orchestration, multi-provider model support, subagents, mutation tracking, headless automation, and offline IDAPython docs at hand.

This is a work in progress with many areas for improvement. If you find bugs, have suggestions, or want quality-of-life improvements, please open an issue.

Thanks for using Luc Nhan.

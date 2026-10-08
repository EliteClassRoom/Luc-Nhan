---
name: Vulnerability Audit
description: Binary vulnerability audit — source-to-sink taint analysis, buffer overflows, format strings, integer issues, memory safety, command injection, type confusion, and parser/network/driver bug classes
tags: [vulnerability, security, audit, exploit, buffer-overflow, format-string, integer-overflow, memory-safety, taint, source-sink]
triggers: [vulnerability, vuln, audit, exploit, exploitable, buffer overflow, format string, memory corruption, integer overflow, use-after-free, command injection, type confusion, find bugs, security review]
mode: plan
---
Task: Binary Vulnerability Audit. Audit the binary for exploitable vulnerabilities using evidence-backed source-to-sink reasoning. Every finding must identify an attacker-controlled source, a reachable sink, the missing or incorrect validation, exploitability constraints, and false-positive checks. Do not report speculation as fact.

Detailed vulnerability classes, source/validator/sink tables, platform branches, and host-specific evidence tools are in the auto-loaded references:

- `references/common.md` — vulnerability model, false-positive gates, severity rules, and report template.
- `references/ida/tools.md` — IDA/Hex-Rays evidence workflow.

## Read-Only Discipline

This is a **read-only** audit by default:

- Do **not** rename, retype, comment, or patch anything unless the user explicitly asks.
- If annotations would help, propose them first and wait for approval.
- Prefer built-in read tools (`get_binary_info`, `list_segments`, `list_imports`, `list_exports`, `decompile_function`, `xrefs_to`, `function_xrefs`) over `execute_python`.
- Use `execute_python` only for bounded helper analysis, such as integer-limit calculations, decoding constants, or checking simple constraints when built-in tools are insufficient.

## Mandatory Workflow

### Phase 0: Binary and Platform Triage

Collect this before deep analysis; run these in parallel when the tool runner supports it:

1. `get_binary_info` — file format, architecture, bitness, entry point, function count, and file type hints.
2. `list_segments` — segment ranges and permissions. Flag W+X memory and unusual executable data.
3. `list_imports` — input APIs, allocator/copy APIs, process launch, crypto, file, network, IPC, and kernel/driver interfaces.
4. `list_exports` — externally reachable entry points.

Only discuss mitigation state (NX/DEP, ASLR, stack cookies, CFG, seccomp, RELRO, PIE) when tool output or binary metadata provides direct evidence. Otherwise mark it as unknown.

If the binary appears packed or heavily obfuscated (very few functions, very few strings, odd executable data, misleading pseudocode), recommend `/deobfuscation` first and avoid vulnerability claims from unreliable code.

### Phase 1: Choose the Audit Branch

Branch the audit based on target type:

- User-mode PE/ELF executable: entry point, command-line/config/file/network inputs.
- Shared library/plugin: every export and registered callback is a potential entry point.
- Daemon/service: socket accept/read loops, dispatchers, privilege-dropping, and restart behavior.
- Kernel driver: prefer `/driver-analysis`; if continuing here, focus on IOCTL/IRP buffers and user-pointer handling.
- Firmware/embedded: packet parsers, MMIO, custom allocators, reboot/DoS paths, debug interfaces.
- Parser/codec: length fields, decompression sizes, recursive parsing, table indices, integer overflow before allocation.

### Phase 2: Prove Source-to-Sink

For each suspected issue:

1. Pick a source: export, entry point, callback, network/file/IPC read, environment/config read, IOCTL buffer, or parser input.
2. Use `xrefs_to` and `function_xrefs` to map callers/callees and place the path in context.
3. Decompile the relevant function with `decompile_function` or `get_pseudocode`.
4. Identify buffers, arguments, local variables, and size values with `get_decompiler_variables` when available.
5. Trace attacker-controlled buffer and length variables through transformations and validators.
6. Identify the exact sink and call/address where unsafe use occurs.
7. Identify the exact missing, wrong, or bypassable check.
8. Verify reachability from a realistic attacker-controlled entry path.
9. Report only when the evidence supports a plausible vulnerability. Otherwise label the item `NEEDS VERIFICATION` or omit it.

### Phase 3: Apply Confidence and False-Positive Gates

Every reported finding must pass these gates:

- Attacker-controlled or realistic local input reaches the code path.
- The path is reachable from an entry point, export, callback, source API, or protocol handler.
- There is a concrete sink or unsafe operation at an address.
- A required bounds, type, lifetime, authorization, or state check is absent, incorrect, or bypassable.
- The impact is plausible under the target architecture and known mitigations.

Confidence levels:

- `CONFIRMED`: complete source-to-sink evidence.
- `LIKELY`: strong evidence with one unresolved assumption.
- `NEEDS VERIFICATION`: suspicious pattern, not a vulnerability claim.

### Phase 4: Report

Use this finding template:

```text
[SEVERITY][CONFIDENCE] CWE-NNN: Title

Location:      function_name at 0xADDRESS, sink/call at 0xADDRESS
Reachability:  entry/export/source -> call_chain -> vulnerable_function
Source:        attacker-controlled buffer/length/config/command at 0xADDRESS
Sink:          API or memory operation at 0xADDRESS
Missing check: exact absent or incorrect condition
Evidence:      short pseudocode/disassembly snippet with addresses
Exploitability: constraints, mitigations, reliability, required attacker position
False-positive checks performed: ...
Remediation:   concrete fix
```

End with a summary table sorted by severity and confidence. If no confirmed or likely vulnerabilities are found, say so explicitly and include residual risk / hardening notes instead of inventing findings.

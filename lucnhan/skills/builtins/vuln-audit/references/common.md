# Common Vulnerability Audit Reference

Use this reference for detailed vulnerability classes, false-positive controls, severity rules, and reporting details. Treat it as supporting material for the top-level `/vuln-audit` workflow.

## Source / Validator / Sink Model

Frame every finding around attacker-controlled data flow through three stages.

### Sources

| Category | APIs / Patterns |
|---|---|
| Command-line | `main(argc, argv)`, `GetCommandLineW`, `CommandLineToArgvW` |
| Network I/O | `recv`, `recvfrom`, `recvmsg`, `read` on socket, `WSARecv`, `InternetReadFile`, `SSL_read` |
| File I/O | `fread`, `fgets`, `ReadFile`, `read`, `pread`, file-backed `mmap`, `MapViewOfFile` |
| Shared memory / IPC | `shmat`, `MapViewOfFile`, D-Bus messages, COM/RPC method arguments, Mach messages |
| Environment / config | `getenv`, `GetEnvironmentVariable`, `RegQueryValueEx`, config file reads |
| IOCTL / kernel interface | `DeviceIoControl` input buffer, `SystemBuffer`, `Type3InputBuffer`, user pointers |
| Plugin / callback | Exported functions, caller-provided buffers, vtables, callback registrations |
| UI / input | `GetMessage`, `GetDlgItemText`, stdin/pipe reads, GUI text fields |
| Protocol parsing | Custom TLV/LV decoders, ASN.1/BER, XML/JSON, compressed streams |

### Validators

| Check Type | What to verify |
|---|---|
| Bounds | Every buffer/size argument is bounded; no truncation, signed/unsigned mismatch, or wrapping. |
| Integer limits | Arithmetic before allocation/copy/indexing has overflow-safe checks. |
| Null termination | String copies/cats explicitly terminate buffers on all paths. |
| Type / tag | Struct casts, tagged unions, and virtual dispatch are guarded by validated type/state. |
| Allocator size | Allocation sizes are clamped, nonzero behavior is understood, and multiplication/addition is checked. |
| Lifetime / state | Free/use order is safe, pointers are invalidated, refcounts/state flags are correct. |
| Authorization / access | Privileged operations have non-bypassable permission checks. |
| TOCTOU | Checked values cannot change before use, especially mapped files, shared memory, and user pointers. |

### Sinks

**Memory operations:**

| Sink | Dangerous pattern | Notes |
|---|---|---|
| `strcpy`, `wcscpy`, `lstrcpyA/W` | Fixed destination, unbounded source | Confirm source control and destination size. |
| `strcat`, `wcscat`, `lstrcatA/W` | Fixed destination, unbounded append | Check remaining-capacity calculation. |
| `sprintf`, `swprintf`, `vsprintf` | Unchecked output buffer or controlled format | Distinguish output overflow from format-string bugs. |
| `gets`, `_gets` | Unbounded read into stack buffer | Always suspicious if reachable. |
| `scanf("%s")`, `sscanf`, `fscanf` | Missing width specifier | Width must be less than destination capacity. |
| `memcpy`, `memmove`, `CopyMemory` | Tainted or unchecked length | Prove length can exceed destination capacity. |
| `alloca`, `_alloca` | Tainted size | Stack exhaustion or stack pivot only if attacker controls size. |
| `operator new[]`, `malloc`, `HeapAlloc` | Overflow before allocation | Check `count * element_size` and `base + length`. |

**Format string APIs:**

- `printf(user_str)`, `fprintf(f, user_str)`, `syslog(prio, user_str)` are vulnerabilities only when the format argument is attacker-controlled.
- `snprintf` is not inherently vulnerable; it becomes vulnerable when the format string is controlled or the size argument is wrong.
- Constant format strings are not findings.

**Command / process execution:**

| Sink | Attack surface |
|---|---|
| `system`, `_wsystem`, `popen`, `_popen` | Shell command injection via unsanitized concatenation. |
| `execve`, `execvp`, `execl`, `execle` | Controlled executable path, argument injection, environment injection. |
| `CreateProcessA/W`, `WinExec` | Command-line parsing ambiguity and controlled `lpCommandLine`. |
| `ShellExecuteA/W`, `ShellExecuteEx` | Controlled file/URL/verb launch. |
| `posix_spawn`, `posix_spawnp` | PATH search and controlled executable path. |
| `fexecve` | File descriptor points to attacker-written memfd or temp file. |

**Allocator / size calculations:**

- `malloc(user_val * sizeof(T))` can allocate too little if multiplication wraps.
- `calloc(nmemb, size)` performs multiplication but still requires upper-bound and DoS checks.
- `VirtualAlloc`/`mmap` with attacker-controlled sizes can be a resource exhaustion sink.
- `realloc(ptr, user_size)` requires careful handling on failure and size-zero behavior.

**Indirect calls / control flow:**

- Indirect calls/jumps through attacker-derived values.
- Virtual calls via corrupted object/vtable pointers.
- Callback invocation using attacker-controlled function pointers.
- `longjmp` with corrupted `jmp_buf`.

**Path and file operations:**

- `fopen`, `CreateFile`, `open`, `unlink`, `DeleteFile`, `rename`, archive extraction paths.
- Path traversal requires a controllable path plus inadequate canonicalization or sandboxing.

## Platform-Specific Audit Branches

**Windows user-mode:** exports, services, COM/RPC handlers, `ReadFile`, registry/env/config reads, `CreateProcess`, `ShellExecute`, `DeviceIoControl` callers.

**Linux/ELF:** `main(argc, argv)`, daemon socket handlers, `read`/`recv`, setuid/setgid paths, `mmap`/`mprotect`, `LD_PRELOAD`, seccomp/capability transitions.

**Shared libraries/plugins:** exported functions, callback registrations, host-provided buffers, ABI assumptions, caller-provided size fields.

**Kernel drivers:** prefer `/driver-analysis`; focus on IOCTL/IRP dispatch, user pointer probing, kernel pool sizes, integer overflow, lifetime and locking.

**Firmware/embedded:** packet parsers, MMIO handlers, custom allocators, unchecked reboot/DoS paths, watchdog interactions, debug backdoors.

**Parsers/codecs:** length fields, decompression sizes, recursive parsing depth, table indices, integer overflow before allocation/copy, malformed nested structures.

## False-Positive Discipline

Do not report:

- Constant format strings.
- Bounded copies where destination size and length are proven safe on all paths.
- Dead or unreachable code.
- Unused imports.
- Compiler/runtime/library wrappers.
- Caller-enforced size constraints that are checked on all paths.
- `strncpy`/`snprintf` calls with correct size arguments and proven post-call termination.

When evidence is incomplete, use `NEEDS VERIFICATION` rather than `CONFIRMED`.

## Confidence Levels

| Level | Criteria |
|---|---|
| CONFIRMED | Complete source-to-sink evidence, reachable attacker input, concrete sink, missing check, plausible impact. |
| LIKELY | Strong evidence with one unresolved but reasonable assumption. |
| NEEDS VERIFICATION | Suspicious pattern only; not a vulnerability claim. |

## Severity Rules

Severity is reachability × impact × exploit reliability:

| Severity | Criteria |
|---|---|
| CRITICAL | Unauthenticated remote code execution, wormable service bug, or privileged boundary crossing. |
| HIGH | Authenticated RCE, reliable local privilege escalation, arbitrary read/write, high-value info leak. |
| MEDIUM | Reachable DoS, constrained overwrite/read, limited info leak, command/path injection with constraints. |
| LOW | Hard-to-reach or low-impact issue with weak exploitability. |
| INFO | Suspicious pattern, hardening note, or mitigation recommendation without confirmed exploitability. |

Promote severity for unauthenticated reachability, privilege boundaries, weak mitigations, and reliable exploitation. Demote for strong required preconditions, local-only access, hard races, or missing exploit primitives.

## Report Template

```text
[SEVERITY][CONFIDENCE] CWE-NNN: Title

Location:      function_name at 0xADDRESS, sink/call at 0xADDRESS
Reachability:  entry/export/source -> call_chain -> vulnerable_function
Source:        attacker-controlled buffer/length/config/command at 0xADDRESS
Sink:          API or memory operation at 0xADDRESS
Missing check: exact absent or incorrect condition
Evidence:
   <short pseudocode/disassembly snippet with addresses>

Exploitability:
   Constraints:        required input shape and attacker control
   Mitigations:        only state mitigations supported by direct evidence
   Reliability:        practical exploitation assessment
   Required position:  unauthenticated remote / authenticated remote / local user / local admin / physical

False-positive checks performed: <checks verified>
Remediation: concrete fix
```

Finish with a table sorted by severity then confidence. If no confirmed or likely findings exist, explicitly say no confirmed vulnerabilities were found and list residual risk or hardening notes.

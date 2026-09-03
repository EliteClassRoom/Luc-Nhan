"""Shared security patterns and execution helper for Python script execution tools."""

from __future__ import annotations

import ast
import builtins
import contextlib
import io
from collections.abc import Callable
from typing import Any

# Modules that must never be imported (directly or via `from X import ...`).
# These provide the "control plane" of Python — process spawning, filesystem,
# network, dynamic code loading, native FFI, and known RCE deserialization
# vectors. Blocking them at import time means user code can still freely
# import pure-compute / data-plane libraries that are essential for malware
# analysis (Crypto.Cipher, struct, binascii, hashlib, math, re, numpy, ...).
_BLOCKED_MODULES = frozenset(
    {
        # Process / shell execution
        "subprocess",
        "shlex",
        "pty",
        "commands",
        "multiprocessing",
        # Filesystem / OS access (env vars, cwd, file IO via module attrs)
        "os",
        "sys",
        "shutil",
        "pathlib",
        "glob",
        "fnmatch",
        "tempfile",
        "fileinput",
        "filecmp",
        # Network access
        "socket",
        "ssl",
        "select",
        "selectors",
        "asyncio",
        "http",
        "urllib",
        "urllib2",
        "urllib3",
        "httplib",
        "ftplib",
        "telnetlib",
        "smtplib",
        "poplib",
        "imaplib",
        "xmlrpc",
        "xmlrpc.client",
        "xmlrpc.server",
        "socketserver",
        # Native FFI (can call C functions, bypass sandbox)
        "ctypes",
        "cffi",
        # Dynamic code loading (can import arbitrary code from anywhere)
        "importlib",
        "pkgutil",
        "zipimport",
        "runpy",
        "modulefinder",
        "code",
        "codeop",
        "idlelib",
        # Deserialization RCE vectors
        "pickle",
        "cPickle",
        "marshal",
        "shelve",
        # Process side effects (signals, resource limits, terminal control)
        "signal",
        "fcntl",
        "resource",
        "termios",
        "tty",
    }
)

# Built-in calls that must never appear.
#
# `__import__` is blocked because even though we restore it to builtins (so
# `import Crypto.Cipher` works), calling it directly is the canonical
# reflective bypass — agents have no reason to call it themselves.
#
# The reflective introspection primitives (`getattr`, `globals`, `vars`, …)
# are blocked because they enable sandbox escapes:
#   - `getattr(os, "system")` reaches os.system() through a name the
#     attribute-blocklist doesn't recognise.
#   - `vars()` / `globals()` / `locals()` return the actual builtins dict;
#     combined with dict mutation this restores removed builtins.
#   - `dir()` leaks attribute names that the agent then targets.
#   - `input()` blocks indefinitely and may exfiltrate via the prompt.
#   - `breakpoint()` drops into pdb in the host process.
_BLOCKED_CALLS = frozenset(
    {
        # Code execution
        "exec",
        "eval",
        "compile",
        # Module import (called as function)
        "__import__",
        # Reflective attribute access — used to bypass the attribute blocklist
        # (e.g. `getattr(os, "system")`, `getattr(__builtins__, "exec")`).
        "getattr",
        "setattr",
        "delattr",
        # Namespace introspection — return the live builtins dict or globals,
        # letting attackers restore removed builtins or walk the namespace.
        "globals",
        "locals",
        "vars",
        "dir",
        # Interactive I/O — `input()` blocks, `breakpoint()` drops into pdb.
        "input",
        "breakpoint",
    }
)

# Attribute calls that must never appear (module.func patterns).
#
# `__builtins__` pairs block dict methods that could restore removed
# builtins (`__builtins__.get("exec")`, `__builtins__.update({...})`,
# `__builtins__.__getitem__("exec")`). The subscript form
# (`__builtins__["exec"]`) is caught separately at ast.Subscript.
_BLOCKED_ATTRS = frozenset(
    {
        # os.* — process / file / env access
        ("os", "system"),
        ("os", "popen"),
        ("os", "execl"),
        ("os", "execle"),
        ("os", "execlp"),
        ("os", "execlpe"),
        ("os", "execv"),
        ("os", "execve"),
        ("os", "execvp"),
        ("os", "execvpe"),
        ("os", "spawnl"),
        ("os", "spawnle"),
        ("os", "spawnlp"),
        ("os", "spawnlpe"),
        ("os", "spawnv"),
        ("os", "spawnve"),
        ("os", "spawnvp"),
        ("os", "spawnvpe"),
        # __builtins__.* — restore removed builtins via dict methods
        ("__builtins__", "get"),
        ("__builtins__", "pop"),
        ("__builtins__", "setdefault"),
        ("__builtins__", "update"),
        ("__builtins__", "__getitem__"),
        ("__builtins__", "__setitem__"),
        ("__builtins__", "__delitem__"),
        ("__builtins__", "clear"),
    }
)

# Dunder attributes that enable class-hierarchy / code-object walks for
# sandbox escape. The classic example is:
#     ().__class__.__bases__[0].__subclasses__()
# which reaches every loaded class (including subprocess.Popen, file IO,
# etc.) without ever naming a blocked module. `__globals__` and `__code__`
# similarly let attackers reach the real `exec`/`os` from inside a function
# defined in a "safe" module.
_BLOCKED_DUNDER_ATTRS = frozenset(
    {
        "__class__",
        "__bases__",
        "__mro__",
        "__subclasses__",
        "__dict__",
        "__globals__",
        "__code__",
        "__builtins__",
    }
)

# Builtins that must be removed from the execution namespace to prevent
# reflective bypasses (e.g. `eval("os.system")`, `exec(compile(...))`).
# Note: `__import__` is intentionally kept here so user code can use
# `import` statements for safe modules. Direct `__import__("...")` calls
# are still rejected by the AST check via _BLOCKED_CALLS.
_REMOVED_BUILTINS = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "breakpoint",
        "exit",
        "quit",
    }
)


def safe_builtins() -> dict[str, Any]:
    """Return a restricted __builtins__ dict with dangerous names removed."""
    safe = {k: v for k, v in vars(builtins).items() if k not in _REMOVED_BUILTINS}
    return safe


def _check_ast(code: str) -> str | None:
    """Parse code and walk the AST for blocked constructs.

    Returns an error message if a violation is found, or None if safe.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "Blocked — code contains a syntax error and cannot be validated"

    for node in ast.walk(tree):
        # Block: import subprocess / from subprocess import ...
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _BLOCKED_MODULES:
                    return f"Blocked — import of disallowed module '{alias.name}'"
            continue

        if isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in _BLOCKED_MODULES:
                    return f"Blocked — import from disallowed module '{node.module}'"
            continue

        # Block: subscript access to __builtins__ (e.g. __builtins__['__import__'])
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and node.value.id == "__builtins__":
                return "Blocked — direct subscript access to __builtins__"
            continue

        # Block: dunder attribute access on any value
        # (e.g. `os.__class__`, `().__class__.__bases__`,
        #        `fn.__globals__`, `fn.__code__`)
        # This covers the class-hierarchy walk escape:
        #     ().__class__.__bases__[0].__subclasses__()
        if isinstance(node, ast.Attribute):
            if node.attr in _BLOCKED_DUNDER_ATTRS:
                return f"Blocked — access to disallowed dunder '{node.attr}'"
            # Don't continue — the Call-handling branch below may also apply
            # if this Attribute is the function of a Call.

        # Block: Call to disallowed built-ins and disallowed module.func pairs
        if isinstance(node, ast.Call):
            func = node.func

            # Call to disallowed built-in: exec(), getattr(), vars(), …
            if isinstance(func, ast.Name) and func.id in _BLOCKED_CALLS:
                return f"Blocked — call to disallowed built-in '{func.id}()'"

            if isinstance(func, ast.Attribute):
                # Call to disallowed module.func pair
                if isinstance(func.value, ast.Name):
                    pair = (func.value.id, func.attr)
                    if pair in _BLOCKED_ATTRS:
                        return f"Blocked — call to disallowed '{pair[0]}.{pair[1]}()'"
                    # Catch os.exec*/os.spawn* variants not explicitly listed
                    if func.value.id == "os" and (func.attr.startswith("exec") or func.attr.startswith("spawn")):
                        return f"Blocked — call to disallowed 'os.{func.attr}()'"

                # Call on a dunder attribute (e.g. obj.__class__(),
                # ().__class__.__bases__[0].__subclasses__()). This is the
                # primary class-hierarchy walk attack surface.
                if func.attr in _BLOCKED_DUNDER_ATTRS:
                    return f"Blocked — call to disallowed dunder '{func.attr}()'"

    return None


#: Public alias. Other security-sensitive surfaces that exec() agent- or
#: LLM-authored Python (e.g. the microcode optimizer compiler) validate
#: their code through the same AST blocklist instead of a private import.
check_ast = _check_ast


def run_guarded_script(code: str, namespace_factory: Callable[[], dict[str, Any]]) -> str:
    """Block dangerous patterns, exec code, and return captured stdout/stderr."""
    violation = _check_ast(code)
    if violation:
        return f"Error: {violation}"

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    namespace = namespace_factory()

    # Ensure __builtins__ is restricted even if the factory provided full access
    ns_builtins = namespace.get("__builtins__")
    if ns_builtins is builtins or ns_builtins is vars(builtins):
        namespace["__builtins__"] = safe_builtins()
    elif isinstance(ns_builtins, dict):
        for name in _REMOVED_BUILTINS:
            ns_builtins.pop(name, None)

    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        try:
            exec(code, namespace)
        except Exception as e:
            stderr_buf.write(f"{type(e).__name__}: {e}\n")

    stdout = stdout_buf.getvalue()
    stderr = stderr_buf.getvalue()
    parts = []
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    if not parts:
        parts.append("(no output)")
    return "\n".join(parts)

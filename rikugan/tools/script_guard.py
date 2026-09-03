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
        # Exec/getattr re-exports + introspection vectors (bypass review):
        # - builtins: the module re-exports exec/eval/getattr/__import__,
        #   so `import builtins; builtins.exec(...)` walked past every
        #   name-based check below.
        # - timeit: timeit.timeit(stmt) compiles and execs its string arg.
        # - pdb / doctest: debugger and docstring runners compile+execute
        #   code strings.
        # - operator: attrgetter reaches attributes by string name
        #   (attrgetter('__class__') mirrors getattr).
        # - inspect (fix round 4, ruling reversed): it transitively exposes
        #   os/sys/importlib as plain attributes (inspect.os, inspect.sys,
        #   inspect.importlib) — `inspect.sys.modules[...]` walks straight
        #   to any loaded module, defeating every name-based barrier. The
        #   f_* frame-attr block alone is not sufficient. The skill docs'
        #   inspect.stack()/signature() debug idioms were removed in favor
        #   of the docs tool and plain prints.
        "builtins",
        "timeit",
        "pdb",
        "doctest",
        "operator",
        "inspect",
        # - rikugan (fix round 3): the guard's own module exposes
        #   _REAL_IMPORT (the unguarded importer captured for the wrapper)
        #   — `import rikugan.tools.script_guard as sg; sg._REAL_IMPORT(...)
        #   re-opens every blocked module. No documented guarded flow
        #   imports the plugin package; rikugan APIs are out of scope for
        #   execute_python by design.
        "rikugan",
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
# defined in a "safe" module. The `f_*` frame-object attributes reach the
# caller's frames (`f_back`), the live builtins dict (`f_builtins`) and
# frame globals/locals/code — closing `inspect.currentframe().f_back
# .f_builtins` walks even when the frame comes from an introspection
# helper instead of a dunder chain.
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
        # Frame-object attrs — the inspect-style escape hatch
        "f_back",
        "f_builtins",
        "f_globals",
        "f_locals",
        "f_code",
        # Closure cells (fix round 3): __closure__ exposes captured cells
        # and cell_contents reads them — a walk like
        # fn.__closure__[0].cell_contents can reach the real import
        # machinery from any function object in reach. `cell_contents` is
        # not a dunder by name but is blocked here as an attribute name.
        "__closure__",
        "cell_contents",
    }
)

# Builtins that must be removed from the execution namespace to prevent
# reflective bypasses (e.g. `eval("os.system")`, `exec(compile(...))`).
# Note: `__import__` is intentionally kept here so user code can use
# `import` statements for safe modules. Direct `__import__("...")` calls
# are still rejected by the AST check via _BLOCKED_CALLS.
# The introspection primitives are stripped as well: the AST check rejects
# calling or binding them, and removing them means a hypothetical future
# AST bypass cannot reach attributes/dicts by string name at runtime.
_REMOVED_BUILTINS = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "breakpoint",
        "exit",
        "quit",
        # Reflective / namespace introspection (fix round 1)
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "dir",
        # Import-machinery handles: __loader__.load_module('sys') returns
        # the real sys module, bypassing the module blocklist (fix round 2)
        "__loader__",
        "__spec__",
    }
)


#: The real import machinery entry point, captured once at module load so
#: the guarded wrapper can delegate while remaining immune to later
#: monkey-patching of the builtins module.
_REAL_IMPORT = builtins.__import__


def _guarded_import(
    name: str,
    globals: dict[str, Any] | None = None,
    locals: dict[str, Any] | None = None,
    fromlist: Any = (),
    level: int = 0,
) -> Any:
    """__import__ replacement installed by :func:`safe_builtins`.

    The AST check rejects literal ``__import__("subprocess")`` calls, but
    the real import function remains reachable by reference — aliased
    namespace-dict lookups (``ns["__builtins__"]["__import__"]``), frame
    walks, etc. Wrapping it makes the module blocklist a runtime invariant
    for import statements and reflective aliases alike.
    """
    root = name.split(".")[0] if name else ""
    if root in _BLOCKED_MODULES:
        raise ImportError(f"Blocked — import of disallowed module '{name}'")
    return _REAL_IMPORT(name, globals, locals, fromlist, level)


def safe_builtins() -> dict[str, Any]:
    """Return a restricted __builtins__ dict with dangerous names removed."""
    safe = {k: v for k, v in vars(builtins).items() if k not in _REMOVED_BUILTINS}
    safe["__import__"] = _guarded_import
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

        # Block: binding or aliasing a blocked built-in name
        # (e.g. `h = __import__`, `g = getattr`, `exec = print`,
        # `def f(x=getattr)`). Aliasing sidesteps every call-site check:
        # `g = getattr; g(os, "system")` never names getattr at a call.
        # Direct calls still report the call message — the BFS walk yields
        # the Call node before its func Name child.
        if isinstance(node, ast.Name) and node.id in _BLOCKED_CALLS:
            return f"Blocked — reference to disallowed built-in '{node.id}'"

        # Block: dunder attribute access on any value
        # (e.g. `os.__class__`, `().__class__.__bases__`,
        #        `fn.__globals__`, `fn.__code__`)
        # This covers the class-hierarchy walk escape:
        #     ().__class__.__bases__[0].__subclasses__()
        if isinstance(node, ast.Attribute):
            if node.attr in _BLOCKED_DUNDER_ATTRS:
                return f"Blocked — access to disallowed dunder '{node.attr}'"

            # Attribute reads of blocked built-ins (e.g. `e = __builtins__.exec`,
            # `g = ns.getattr`). A bound method object is as dangerous as the
            # call, minus the call syntax. `compile` stays exempt: re.compile
            # is a documented data-plane idiom.
            if node.attr in _BLOCKED_CALLS and node.attr != "compile":
                return f"Blocked — access to disallowed built-in attribute '{node.attr}'"
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

                # Receiver-agnostic: any call whose attribute name is itself
                # a blocked built-in (e.g. builtins.exec(...),
                # __builtins__.eval(...), ns.getattr(...)). `compile` is
                # excluded: `re.compile` is a documented data-plane idiom and
                # compiling alone executes nothing — exec/eval of the result
                # stay blocked both as bare calls and as attribute names.
                if func.attr in _BLOCKED_CALLS and func.attr != "compile":
                    return f"Blocked — attribute call to disallowed built-in '{func.attr}()'"

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

    # Ensure __builtins__ is restricted no matter what the factory provided:
    # - the real module/dict is replaced wholesale (never mutated — it is
    #   the interpreter-wide builtins);
    # - a custom dict is stripped in place and always gets the guarded
    #   importer installed;
    # - an absent/unknown __builtins__ is pinned to safe_builtins(),
    #   because exec() would otherwise inject the LIVE builtins dict.
    ns_builtins = namespace.get("__builtins__")
    if ns_builtins is builtins or ns_builtins is builtins.__dict__:
        namespace["__builtins__"] = safe_builtins()
    elif isinstance(ns_builtins, dict):
        for name in _REMOVED_BUILTINS:
            ns_builtins.pop(name, None)
        ns_builtins["__import__"] = _guarded_import
    else:
        namespace["__builtins__"] = safe_builtins()

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

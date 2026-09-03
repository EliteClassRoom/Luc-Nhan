"""Tests for rikugan/tools/script_guard.py."""

from __future__ import annotations

import builtins
import os
import sys
import unittest

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from rikugan.ida.tools.microcode_optim import compile_optimizer
from rikugan.tools.script_guard import (
    GuardViolation,
    _check_ast,
    check_ast,
    run_guarded_code,
    run_guarded_script,
    safe_builtins,
)


def _empty_ns():
    return {}


class TestCheckAst(unittest.TestCase):
    def test_blocks_subprocess(self):
        assert _check_ast("import subprocess") is not None

    def test_blocks_os_system(self):
        assert _check_ast("os.system('ls')") is not None

    def test_blocks_os_popen(self):
        assert _check_ast("os.popen('ls')") is not None

    def test_blocks_import_subprocess_via_dunder(self):
        assert _check_ast("__import__('subprocess')") is not None

    def test_blocks_os_exec(self):
        assert _check_ast("os.execv('/bin/sh', [])") is not None

    def test_blocks_os_spawn(self):
        assert _check_ast("os.spawnl(0, '/bin/sh')") is not None

    def test_blocks_exec_call(self):
        assert _check_ast("exec('code')") is not None

    def test_blocks_eval_call(self):
        assert _check_ast("eval('1+1')") is not None

    def test_blocks_from_subprocess_import(self):
        assert _check_ast("from subprocess import Popen") is not None

    def test_blocks_syntax_error(self):
        assert _check_ast("def f(:\n    pass") is not None

    def test_allows_harmless_code(self):
        assert _check_ast("x = 1 + 2") is None

    def test_allows_print(self):
        assert _check_ast("print('hello')") is None

    def test_allows_os_path(self):
        assert _check_ast("os.path.join('a', 'b')") is None

    def test_allows_re_compile(self):
        # re.compile is a documented data-plane idiom (string-decryption
        # skill reference); the receiver-agnostic attribute rule must not
        # fire on it — compiling alone executes nothing.
        assert _check_ast("import re\nrule = re.compile(r'a+b')") is None

    def test_allows_dict_get(self):
        # dict.get has the same attribute name as the __builtins__.get
        # pair block, but 'get' is not a blocked CALL name.
        assert _check_ast("d = {}\nd.get('k')") is None

    def test_blocks_builtins_attr_call_ambient(self):
        # __builtins__ is ambient inside the exec namespace — no import
        # required — so the receiver-agnostic rule must catch the call
        # form directly.
        assert _check_ast('__builtins__.exec("x = 1")') is not None

    def test_blocks_frame_attr_walk(self):
        # Frame-object attrs are the inspect-free escape: any frame in
        # reach exposes the live builtins dict via f_builtins.
        assert _check_ast("frame.f_back.f_builtins") is not None

    # --- Guard self-import / closure escapes (fix round 3) ---------------
    # The guard module itself exposes _REAL_IMPORT (the unguarded importer);
    # importing rikugan.* from a guarded script must be rejected outright.

    def test_blocks_import_rikugan_guard_module(self):
        assert _check_ast("import rikugan.tools.script_guard as sg") is not None

    def test_blocks_rikugan_real_import_chain(self):
        code = "import rikugan.tools.script_guard as sg\nm = sg._REAL_IMPORT('subprocess')\nprint(m.run)"
        assert _check_ast(code) is not None

    def test_blocks_from_rikugan_import(self):
        assert _check_ast("from rikugan.tools import script_guard") is not None

    def test_blocks_closure_walk(self):
        # Function objects reachable from the sandbox (e.g. IDA module
        # callables) must not leak their captured cells.
        assert _check_ast("f.__closure__") is not None
        assert _check_ast("w.__closure__[0].cell_contents") is not None

    # --- Bare-name aliasing / binding bypasses (fix round 1) -------------
    # `h = __import__; h('subprocess')` and `g = getattr; g(os, 'system')`
    # never name the blocked builtin at a call site — binding or aliasing
    # the name itself must be rejected.

    def test_blocks_alias_of_dunder_import(self):
        assert _check_ast("h = __import__\nm = h('subprocess')") is not None

    def test_blocks_alias_of_getattr(self):
        assert _check_ast("g = getattr\ng(os, 'system')") is not None

    def test_blocks_binding_blocked_name(self):
        assert _check_ast("exec = print") is not None
        assert _check_ast("d = dir") is not None

    def test_blocks_blocked_name_as_default(self):
        # Keyword/default positions are bare references too.
        assert _check_ast("def f(x=getattr):\n    pass") is not None

    def test_blocks_attribute_read_of_blocked_builtin(self):
        # `e = __builtins__.exec` — an aliased method object is as
        # dangerous as a call, minus the call syntax.
        assert _check_ast("e = __builtins__.exec") is not None

    def test_blocks_import_inspect(self):
        # Fix round 4 (ruling reversed): inspect transitively exposes
        # os/sys/importlib as plain attributes (inspect.os, inspect.sys).
        assert _check_ast("import inspect") is not None

    def test_blocks_inspect_os_chain(self):
        assert _check_ast("import inspect\ninspect.os.system('id')") is not None

    def test_blocks_inspect_sys_modules_chain(self):
        code = (
            "import inspect\n"
            "sg = inspect.sys.modules['rikugan.tools.script_guard']\n"
            "m = sg._REAL_IMPORT('subprocess')\n"
            "print(m.run)"
        )
        assert _check_ast(code) is not None

    def test_blocks_from_inspect_import(self):
        assert _check_ast("from inspect import currentframe") is not None

    def test_blocks_frame_locals_reach_via_inspect(self):
        # inspect.getargvalues(...).locals — the attribute name 'locals'
        # is itself a blocked built-in, so the attribute rule fires.
        code = "import inspect\ninspect.getargvalues(inspect.currentframe()).locals"
        assert _check_ast(code) is not None

    def test_allows_compile_attribute_reference(self):
        # The compile carve-out covers attribute reads too.
        assert _check_ast("import re\nr = re.compile") is None

    def test_safe_builtins_strips_introspection(self):
        from rikugan.tools.script_guard import safe_builtins

        ns = safe_builtins()
        for name in ("getattr", "setattr", "delattr", "vars", "dir"):
            self.assertNotIn(name, ns)

    # --- Allowlist: safe data-plane / pure-compute modules ---------------
    # These are the whole point of the policy change: agents need Crypto.Cipher
    # and friends to decode malware algorithms without reimplementing them.

    def test_allows_import_struct(self):
        assert _check_ast("import struct") is None

    def test_allows_import_hashlib(self):
        assert _check_ast("import hashlib") is None

    def test_allows_import_math(self):
        assert _check_ast("import math") is None

    def test_allows_import_binascii(self):
        assert _check_ast("import binascii") is None

    def test_allows_import_collections(self):
        assert _check_ast("import collections") is None

    def test_allows_import_re(self):
        assert _check_ast("import re") is None

    def test_allows_import_numpy(self):
        assert _check_ast("import numpy") is None

    def test_allows_import_zlib(self):
        assert _check_ast("import zlib") is None

    def test_allows_import_base64(self):
        assert _check_ast("import base64") is None

    def test_allows_import_crypto_cipher(self):
        assert _check_ast("import Crypto.Cipher") is None

    def test_allows_from_crypto_cipher(self):
        assert _check_ast("from Crypto.Cipher import AES") is None

    def test_allows_nested_dotted_import(self):
        # Dotted imports of safe top-level packages should also be allowed
        assert _check_ast("import xml.etree.ElementTree") is None

    # --- Blocklist: control-plane modules --------------------------------

    def test_blocks_import_os(self):
        assert _check_ast("import os") is not None

    def test_blocks_import_sys(self):
        assert _check_ast("import sys") is not None

    def test_blocks_import_shutil(self):
        assert _check_ast("import shutil") is not None

    def test_blocks_import_pathlib(self):
        assert _check_ast("import pathlib") is not None

    def test_blocks_import_socket(self):
        assert _check_ast("import socket") is not None

    def test_blocks_import_ssl(self):
        assert _check_ast("import ssl") is not None

    def test_blocks_import_asyncio(self):
        assert _check_ast("import asyncio") is not None

    def test_blocks_import_urllib(self):
        assert _check_ast("import urllib.request") is not None

    def test_blocks_import_pickle(self):
        assert _check_ast("import pickle") is not None

    def test_blocks_import_marshal(self):
        assert _check_ast("import marshal") is not None

    def test_blocks_import_ctypes(self):
        assert _check_ast("import ctypes") is not None

    def test_blocks_import_cffi(self):
        assert _check_ast("import cffi") is not None

    def test_blocks_import_importlib(self):
        assert _check_ast("import importlib") is not None

    def test_blocks_import_multiprocessing(self):
        assert _check_ast("import multiprocessing") is not None

    def test_blocks_import_signal(self):
        assert _check_ast("import signal") is not None

    def test_blocks_from_os(self):
        assert _check_ast("from os import path") is not None

    def test_blocks_from_socket(self):
        assert _check_ast("from socket import socket") is not None

    def test_blocks_from_pickle(self):
        assert _check_ast("from pickle import loads") is not None

    def test_blocks_from_importlib(self):
        assert _check_ast("from importlib import import_module") is not None

    # --- __import__() reflective bypass ----------------------------------
    # Even though we restore __import__ to builtins (so `import` statements
    # work), calling it as a function is the canonical bypass attempt and
    # must still be caught by the AST check.

    def test_blocks_dunder_import_struct(self):
        assert _check_ast("__import__('struct')") is not None

    def test_blocks_dunder_import_crypto(self):
        assert _check_ast("__import__('Crypto.Cipher')") is not None


# --- Known review bypasses -----------------------------------------------
# Each entry is a vector the review identified as walking past the AST
# blocklist. All must be rejected by check_ast().

BYPASSES = [
    "import builtins\nbuiltins.exec(\"import subprocess; subprocess.run('calc')\")",
    "import builtins\nbuiltins.__import__('subprocess')",
    "import timeit\ntimeit.timeit(\"import os; os.system('id')\", number=1)",
    "import pdb\npdb.set_trace()",
    "import inspect\ninspect.currentframe().f_back.f_builtins",
    "import operator\nf = operator.attrgetter('__class__')",
    "import builtins\nbuiltins.getattr(builtins, 'eval')",
]


@pytest.mark.parametrize("code", BYPASSES)
def test_known_bypasses_blocked(code):
    from rikugan.tools.script_guard import check_ast

    assert check_ast(code) is not None


class TestRunGuardedScript(unittest.TestCase):
    def test_blocked_subprocess(self):
        result = run_guarded_script("import subprocess", _empty_ns)
        assert result.startswith("Error: Blocked")
        assert "subprocess" in result

    def test_blocked_os_system(self):
        result = run_guarded_script("os.system('ls')", _empty_ns)
        assert "Blocked" in result

    def test_stdout_captured(self):
        result = run_guarded_script("print('hello')", _empty_ns)
        assert "hello" in result
        assert "stdout" in result

    def test_stderr_on_exception(self):
        result = run_guarded_script("raise ValueError('oops')", _empty_ns)
        assert "ValueError" in result
        assert "oops" in result
        assert "stderr" in result

    def test_no_output_placeholder(self):
        result = run_guarded_script("x = 1 + 2", _empty_ns)
        assert result == "(no output)"

    def test_namespace_provided_to_exec(self):
        ns_calls = []

        def ns_factory():
            d = {"captured": ns_calls}
            ns_calls.append("called")
            return d

        result = run_guarded_script("captured.append('exec')", ns_factory)
        assert "exec" in ns_calls
        assert result == "(no output)"

    def test_stdout_and_stderr_combined(self):
        code = "print('out'); raise RuntimeError('err')"
        result = run_guarded_script(code, _empty_ns)
        assert "stdout" in result
        assert "out" in result
        assert "stderr" in result
        assert "RuntimeError" in result

    def test_syntax_error_in_code(self):
        result = run_guarded_script("def f(:\n    pass", _empty_ns)
        assert "Error" in result

    def test_namespace_factory_called_fresh_each_time(self):
        calls = []

        def factory():
            calls.append(1)
            return {}

        run_guarded_script("x = 1", factory)
        run_guarded_script("y = 2", factory)
        assert len(calls) == 2

    # --- Reflective bypass defenses -------------------------------------
    # These close the sandbox-escape chains documented in the module:
    #   - getattr/setattr/delattr reach blocked attrs by string name.
    #   - globals()/locals()/vars()/dir() return the live builtins dict.
    #   - input()/breakpoint() pause for interactive I/O.
    #   - Class-hierarchy walks via __class__/__bases__/__subclasses__.
    #   - Function-introspection walks via __globals__/__code__/__dict__.
    #   - __builtins__ dict-method restoration of removed builtins.

    def test_blocks_getattr_call(self):
        assert _check_ast("getattr(x, 'y')") is not None

    def test_blocks_setattr_call(self):
        assert _check_ast("setattr(x, 'y', 1)") is not None

    def test_blocks_delattr_call(self):
        assert _check_ast("delattr(x, 'y')") is not None

    def test_blocks_globals_call(self):
        assert _check_ast("globals()") is not None

    def test_blocks_locals_call(self):
        assert _check_ast("locals()") is not None

    def test_blocks_vars_call(self):
        assert _check_ast("vars()") is not None

    def test_blocks_dir_call(self):
        assert _check_ast("dir()") is not None

    def test_blocks_input_call(self):
        assert _check_ast("input('prompt')") is not None

    def test_blocks_breakpoint_call(self):
        assert _check_ast("breakpoint()") is not None

    def test_blocks_getattr_to_reach_blocked_method(self):
        # Realistic exploit: get os.system via getattr, bypassing the
        # (os, system) attribute pair check.
        assert _check_ast("getattr(os, 'system')('cmd')") is not None

    def test_blocks_setattr_on_builtins(self):
        assert _check_ast("setattr(__builtins__, 'exec', exec)") is not None

    def test_blocks_builtins_get_dict_method(self):
        # __builtins__.get('exec') would restore the removed built-in.
        assert _check_ast("__builtins__.get('exec')") is not None

    def test_blocks_builtins_pop_dict_method(self):
        assert _check_ast("__builtins__.pop('exec')") is not None

    def test_blocks_builtins_update_dict_method(self):
        assert _check_ast("__builtins__.update({'exec': exec})") is not None

    def test_blocks_builtins_setdefault(self):
        assert _check_ast("__builtins__.setdefault('exec', exec)") is not None

    def test_blocks_builtins_clear(self):
        assert _check_ast("__builtins__.clear()") is not None

    def test_blocks_class_hierarchy_walk(self):
        # The classic Python sandbox escape: tuple -> __class__ -> __bases__
        # -> __subclasses__ -> loaded classes (Popen, file IO, etc.).
        attack = "().__class__.__bases__[0].__subclasses__()"
        assert _check_ast(attack) is not None

    def test_blocks_dunder_class_attr(self):
        assert _check_ast("x.__class__") is not None

    def test_blocks_dunder_bases_attr(self):
        assert _check_ast("x.__bases__") is not None

    def test_blocks_dunder_mro_attr(self):
        assert _check_ast("x.__mro__") is not None

    def test_blocks_dunder_subclasses_call(self):
        assert _check_ast("object.__subclasses__()") is not None

    def test_blocks_dunder_dict_attr(self):
        assert _check_ast("x.__dict__") is not None

    def test_blocks_function_globals_via_dunder(self):
        # Inside a real exploit, a function defined in a "safe" module
        # exposes its globals dict via __globals__, which contains the real
        # builtins.
        assert _check_ast("fn.__globals__") is not None

    def test_blocks_function_code_via_dunder(self):
        assert _check_ast("fn.__code__") is not None

    def test_blocks_dunder_builtins_attr(self):
        assert _check_ast("x.__builtins__") is not None

    def test_blocks_via_getattr_then_call(self):
        # The chained-call form: getattr(os, "system")("cmd").
        attack = "imp = __import__\nos = imp('os')\nsystem = getattr(os, 'system')\nsystem('echo pwned')"
        assert _check_ast(attack) is not None

    def test_blocks_vars_to_reach_builtins(self):
        attack = "v = vars()\nv['__builtins__']['exec'] = exec\nexec('print(1)')"
        assert _check_ast(attack) is not None

    def test_blocks_globals_to_reach_builtins(self):
        attack = "g = globals()\ng['__builtins__'].update({'exec': exec})\nexec('print(1)')"
        assert _check_ast(attack) is not None

    # --- Runtime verification of the new allowlist ----------------------
    # These prove that the policy change actually delivers what the user
    # asked for: import statements for safe modules execute at runtime,
    # and control-plane imports are rejected before exec().

    def test_import_struct_works_at_runtime(self):
        # struct.pack of 'ABCD' as little-endian uint32 → bytes 44 43 42 41
        result = run_guarded_script(
            "import struct\nprint(struct.pack('<I', 0x41424344).hex())",
            _empty_ns,
        )
        assert "stdout" in result
        assert "44434241" in result

    def test_import_math_works_at_runtime(self):
        result = run_guarded_script(
            "import math\nprint(f'{math.pi:.2f}')",
            _empty_ns,
        )
        assert "stdout" in result
        assert "3.14" in result

    def test_import_hashlib_works_at_runtime(self):
        # MD5 of empty string is the canonical constant d41d8cd9...
        result = run_guarded_script(
            "import hashlib\nprint(hashlib.md5(b'').hexdigest())",
            _empty_ns,
        )
        assert "stdout" in result
        assert "d41d8cd98f00b204e9800998ecf8427e" in result

    def test_import_base64_works_at_runtime(self):
        result = run_guarded_script(
            "import base64\nprint(base64.b64encode(b'AB').decode())",
            _empty_ns,
        )
        assert "stdout" in result
        assert "QUI=" in result

    def test_blocks_os_import_at_runtime(self):
        result = run_guarded_script("import os", _empty_ns)
        assert result.startswith("Error: Blocked")
        assert "os" in result

    def test_blocks_socket_import_at_runtime(self):
        result = run_guarded_script("import socket", _empty_ns)
        assert result.startswith("Error: Blocked")
        assert "socket" in result

    def test_blocks_pickle_import_at_runtime(self):
        result = run_guarded_script("import pickle", _empty_ns)
        assert result.startswith("Error: Blocked")
        assert "pickle" in result

    def test_blocks_dunder_import_call_at_runtime(self):
        # Even with __import__ restored to builtins, calling it is blocked
        result = run_guarded_script("__import__('struct')", _empty_ns)
        assert result.startswith("Error: Blocked")

    # --- Runtime module-blocklist enforcement (fix round 2) --------------
    # The AST check rejects literal __import__ calls, but the real import
    # function stays reachable by reference (aliased namespace-dict lookups).
    # safe_builtins() must therefore install a guarded importer.

    def test_runtime_blocks_import_via_builtins_dict_alias(self):
        # Exact review repro: walk frame locals to the namespace dict, grab
        # __builtins__, fetch __import__, import subprocess. Since round 4
        # the `import inspect` entry is itself statically blocked, so the
        # chain dies before exec(); the wrapper layer underneath stays
        # covered by test_safe_builtins_import_is_guarded.
        code = (
            "import inspect\n"
            "ns = inspect.getargvalues(inspect.currentframe())[3]\n"
            "h = ns['__builtins__']['__import__']\n"
            "m = h('subprocess')\n"
            "print(m.run)"
        )
        result = run_guarded_script(code, _empty_ns)
        assert result.startswith("Error: Blocked")
        assert "inspect" in result

    def test_run_guarded_script_pins_restricted_builtins(self):
        # A factory without __builtins__ gets safe_builtins() pinned —
        # never the live interpreter builtins dict injected by exec().
        result = run_guarded_script("print('exec' in __builtins__, 'dir' in __builtins__)", _empty_ns)
        assert "False False" in result
        # ... and the interpreter-wide builtins are never mutated.
        assert "exec" in vars(builtins)
        assert "dir" in vars(builtins)
        assert "__loader__" in vars(builtins)

    def test_safe_builtins_import_is_guarded(self):
        ns = safe_builtins()
        importer = ns["__import__"]
        assert importer is not builtins.__import__
        for mod in ("subprocess", "os", "os.path", "ctypes"):
            with self.assertRaises(ImportError):
                importer(mod)

    def test_safe_builtins_import_still_allows_data_plane(self):
        ns = safe_builtins()
        importer = ns["__import__"]
        struct_mod = importer("struct")
        self.assertTrue(hasattr(struct_mod, "pack"))
        # Dotted safe imports keep working (root-module check only).
        element_tree = importer("xml.etree.ElementTree", None, None, ("ElementTree",))
        self.assertTrue(hasattr(element_tree, "parse"))

    def test_runtime_nested_dotted_import_of_safe_module(self):
        result = run_guarded_script("import xml.etree.ElementTree\nprint('ok')", _empty_ns)
        assert "ok" in result

    def test_runtime_blocks_rikugan_import(self):
        result = run_guarded_script("import rikugan", _empty_ns)
        assert result.startswith("Error: Blocked")
        assert "rikugan" in result


class TestRunGuardedCode(unittest.TestCase):
    """run_guarded_code is the second exec sink: AST check, then exec into a
    caller-supplied namespace (no stdout capture). The microcode optimizer
    compiler goes through it instead of calling exec() directly."""

    def test_executes_into_supplied_namespace_and_returns_it(self):
        ns: dict = {}
        result = run_guarded_code("x = 1 + 2", ns)
        assert result is ns
        assert ns["x"] == 3

    def test_blocked_code_raises_guard_violation_without_executing(self):
        ns = {"executed": False}
        with pytest.raises(GuardViolation) as excinfo:
            run_guarded_code("executed = True\nimport subprocess", ns)
        assert "subprocess" in str(excinfo.value)
        assert ns["executed"] is False

    def test_guard_violation_is_a_value_error(self):
        # Callers (compile_optimizer) keep their ValueError-on-blocked contract.
        assert issubclass(GuardViolation, ValueError)
        with pytest.raises(ValueError):
            run_guarded_code("os.system('ls')", {})

    def test_pins_guarded_builtins_when_namespace_lacks_them(self):
        # exec() would otherwise inject the LIVE builtins dict — the same
        # pinning run_guarded_script applies must hold for this sink too.
        ns = run_guarded_code("leaked = 'exec' in __builtins__ or 'dir' in __builtins__", {})
        assert ns["leaked"] is False
        # ... and the interpreter-wide builtins are never mutated.
        assert "exec" in vars(builtins)
        assert "dir" in vars(builtins)

    def test_does_not_redirect_stdout(self):
        # Unlike run_guarded_script, this sink must let stdout through:
        # the optimizer compiler's host owns output, not a tool-result string.
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_guarded_code("print('visible')", {})
        assert "visible" in buf.getvalue()


def test_compile_optimizer_executes_via_guarded_sink(monkeypatch):
    """Single-sink invariant: compile_optimizer must delegate execution to
    run_guarded_code instead of calling exec() itself."""
    import rikugan.ida.tools.microcode_optim as microcode_optim

    real_run = microcode_optim.run_guarded_code
    seen: dict = {}

    def spy(code, namespace, **kwargs):
        seen["code"] = code
        return real_run(code, namespace, **kwargs)

    monkeypatch.setattr(microcode_optim, "run_guarded_code", spy)
    fn = microcode_optim.compile_optimizer("sink", "def optimize(mbi, ins):\n    return 0\n")
    assert callable(fn)
    assert seen["code"] == "def optimize(mbi, ins):\n    return 0\n"


class TestCompileOptimizerGuard(unittest.TestCase):
    """install_microcode_optimizer exec()s LLM-authored code — its compile
    path must run through the same AST blocklist as execute_python."""

    def test_compile_optimizer_rejects_subprocess_import(self):
        code = "def optimize(mbi, ins): return 0\nimport subprocess\nsubprocess.run(['calc'])"
        try:
            compile_optimizer("evil", code)
        except ValueError as e:
            self.assertTrue("disallowed module" in str(e) or "Blocked" in str(e))
        else:
            self.fail("compile_optimizer accepted blocked code")

    def test_compile_optimizer_rejection_message_keeps_prefix(self):
        # The ValueError must stay prefixed so the LLM sees which layer
        # rejected the code (microcode.py returns str(e) as the tool result).
        code = "def optimize(mbi, ins): return 0\nimport subprocess\n"
        with pytest.raises(ValueError) as excinfo:
            compile_optimizer("evil", code)
        assert str(excinfo.value).startswith("Optimizer code rejected by script guard:")

    def test_compile_optimizer_accepts_pure_code(self):
        code = "def optimize(mbi, ins):\n    return 0\n"
        fn = compile_optimizer("ok", code)
        self.assertTrue(callable(fn))

    def test_public_check_ast_alias_matches_private(self):
        self.assertIs(check_ast, _check_ast)
        self.assertIsNone(check_ast("x = 1"))


if __name__ == "__main__":
    unittest.main()

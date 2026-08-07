"""Mock IDA API modules for testing outside IDA Pro."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

# Constants
BADADDR = 0xFFFFFFFFFFFFFFFF


def _create_mock_module(name: str, attrs: dict = None) -> ModuleType:
    mod = ModuleType(name)
    mod.__dict__.update(attrs or {})
    return mod


_NETNODE_STORE: dict = {}


class _PersistentNetnode:
    """Simulates an IDA netnode with in-memory persistent supstr/supset storage."""

    def __init__(self, name: str):
        self._name = name

    def supstr(self, idx):
        return _NETNODE_STORE.get((self._name, idx), "")

    def supset(self, idx, value):
        _NETNODE_STORE[(self._name, idx)] = value
        return True


def install_ida_mocks() -> None:
    """Install mock IDA modules into sys.modules for testing."""

    # Reset persistent netnode storage so each install_ida_mocks() call is clean.
    _NETNODE_STORE.clear()

    # idaapi
    idaapi = MagicMock()
    idaapi.PLUGIN_MULTI = 0x0001
    idaapi.PLUGIN_FIX = 0x0002
    idaapi.get_user_idadir.return_value = "/tmp/ida_test"
    idaapi.get_inf_structure.return_value = MagicMock(
        procname="ARM",
        start_ea=0x1000,
        min_ea=0x1000,
        max_ea=0x10000,
        is_16bit=lambda: False,
        is_32bit=lambda: False,
    )
    idaapi.get_file_type_name.return_value = "ELF"
    idaapi.PATH_TYPE_IDB = 0
    idaapi.get_path.return_value = "/tmp/ida_test/test.idb"
    idaapi.get_input_file_path.return_value = "/tmp/ida_test/test.bin"
    idaapi.BWN_DISASM = 1
    idaapi.BWN_PSEUDOCODE = 2
    idaapi.PluginForm = type(
        "PluginForm",
        (),
        {
            "WOPN_TAB": 1,
            "WOPN_PERSIST": 2,
            "Show": lambda self, *a, **k: None,
            "Close": lambda self, *a, **k: None,
            # Real IDA ≥ 9.0 ``idaapi.PluginForm`` exposes BOTH conversion
            # methods; ``FormToPyQtWidget`` exists even though IDA's PyQt5 is
            # a shim. Mocking both lets tests assert which one OnCreate picks.
            "FormToPySideWidget": staticmethod(lambda form: MagicMock()),
            "FormToPyQtWidget": staticmethod(lambda form: MagicMock()),
        },
    )
    # Make netnode("$ rikugan", ...) return a persistent-storage node so that
    # db_instance_id survives across multiple SessionController instances in tests.
    _BADNODE_SENTINEL = object()
    idaapi.BADNODE = _BADNODE_SENTINEL

    def _netnode_factory(name, *args, **kwargs):
        if name == "$ rikugan":
            return _PersistentNetnode(name)
        return MagicMock()

    idaapi.netnode.side_effect = _netnode_factory

    sys.modules["idaapi"] = idaapi

    # idc
    idc = MagicMock()
    idc.BADADDR = BADADDR
    idc.get_screen_ea.return_value = 0x1000
    idc.get_strlit_contents.return_value = b"test string"
    idc.get_segm_name.return_value = ".text"
    idc.get_segm_end.return_value = 0x2000
    idc.print_insn_mnem.return_value = "MOV"
    idc.print_operand.return_value = ""
    idc.get_cmt.return_value = ""
    idc.get_item_size.return_value = 4
    idc.get_wide_byte.return_value = 0x90
    idc.next_head.return_value = BADADDR
    idc.set_cmt.return_value = True
    idc.SetType.return_value = True
    sys.modules["idc"] = idc

    # idautils
    idautils = MagicMock()
    idautils.Functions.return_value = [0x1000, 0x1100, 0x1200]
    idautils.Segments.return_value = [0x1000]
    idautils.Strings.return_value = []
    idautils.Entries.return_value = []
    idautils.XrefsTo.return_value = []
    idautils.XrefsFrom.return_value = []
    idautils.CodeRefsTo.return_value = []
    idautils.CodeRefsFrom.return_value = []
    idautils.FuncItems.return_value = []
    sys.modules["idautils"] = idautils

    # ida_* modules
    for mod_name in (
        "ida_funcs",
        "ida_name",
        "ida_bytes",
        "ida_segment",
        "ida_struct",
        "ida_enum",
        "ida_typeinf",
        "ida_nalt",
        "ida_xref",
        "ida_kernwin",
        "ida_gdl",
        "ida_hexrays",
        "ida_lines",
        "ida_auto",
        "ida_ida",
        "ida_range",
    ):
        sys.modules[mod_name] = MagicMock()

    # Provide real base classes for Hex-Rays optimizer types so subclasses
    # defined in rikugan.ida.tools.microcode_optim can override func() properly.
    class _OptInsnStub:
        def remove(self):
            pass

        def install(self):
            pass

    class _OptBlockStub:
        def remove(self):
            pass

        def install(self):
            pass

    class _HexraysHooksStub:
        pass

    sys.modules["ida_hexrays"].optinsn_t = _OptInsnStub
    sys.modules["ida_hexrays"].optblock_t = _OptBlockStub
    sys.modules["ida_hexrays"].Hexrays_Hooks = _HexraysHooksStub

    # Configure specific behaviors
    sys.modules["ida_name"].get_name.return_value = "sub_1000"
    sys.modules["ida_name"].get_name_ea.return_value = 0x1000
    sys.modules["ida_name"].set_name.return_value = True
    sys.modules["ida_name"].SN_NOWARN = 0
    sys.modules["ida_name"].SN_NOCHECK = 0
    sys.modules["ida_funcs"].get_func.return_value = MagicMock(
        start_ea=0x1000,
        end_ea=0x1100,
        flags=0,
    )
    sys.modules["ida_kernwin"].MFF_WRITE = 0
    sys.modules["ida_kernwin"].execute_sync = lambda fn, _: fn()

    sys.modules["ida_nalt"].get_root_filename.return_value = "test_binary"
    sys.modules["ida_nalt"].get_import_module_qty.return_value = 0

    sys.modules["ida_segment"].SFL_READ = 1
    sys.modules["ida_segment"].SFL_WRITE = 2
    sys.modules["ida_segment"].SFL_EXEC = 4
    sys.modules["ida_segment"].getseg.return_value = MagicMock(perm=7)  # R|W|X

    # ida_ida (IDA 9.x info accessors)
    sys.modules["ida_ida"].inf_get_procname.return_value = "ARM"
    sys.modules["ida_ida"].inf_is_64bit.return_value = True
    sys.modules["ida_ida"].inf_is_32bit.return_value = False
    # inf_get_app_bitness() is the canonical bitness accessor (IDA >=7.6);
    # it returns 16/32/64 directly. Default mock DB is ARM64.
    sys.modules["ida_ida"].inf_get_app_bitness.return_value = 64
    sys.modules["ida_ida"].inf_get_start_ea.return_value = 0x1000
    sys.modules["ida_ida"].inf_get_min_ea.return_value = 0x1000
    sys.modules["ida_ida"].inf_get_max_ea.return_value = 0x10000

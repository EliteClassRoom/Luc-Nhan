"""Tests for rikugan.ida.tools.types_tools — IDA type engineering tools."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

import rikugan.ida.tools.types_tools as types_tools
from rikugan.core.errors import ToolError

# ---------------------------------------------------------------------------
# _require_ida_enum
# ---------------------------------------------------------------------------


class TestRequireIdaEnum(unittest.TestCase):
    def test_raises_when_ida_enum_and_idc_none(self):
        orig_enum = types_tools.ida_enum
        orig_idc = types_tools.idc
        try:
            types_tools.ida_enum = None
            types_tools.idc = None
            with self.assertRaises(ToolError):
                types_tools._require_ida_enum()
        finally:
            types_tools.ida_enum = orig_enum
            types_tools.idc = orig_idc

    def test_no_raise_when_available(self):
        orig = types_tools.ida_enum
        try:
            types_tools.ida_enum = MagicMock()
            types_tools._require_ida_enum()  # must not raise
        finally:
            types_tools.ida_enum = orig


# ---------------------------------------------------------------------------
# create_typedef
# ---------------------------------------------------------------------------


class TestCreateTypedef(unittest.TestCase):
    def test_idc_not_available_returns_error(self):
        orig = types_tools.idc
        try:
            types_tools.idc = None
            result = types_tools.create_typedef("MyType", "unsigned int")
            self.assertIn("not available", result)
        finally:
            types_tools.idc = orig

    def test_success_path(self):
        orig = types_tools.idc
        try:
            mock_idc = MagicMock()
            mock_idc.parse_decls.return_value = 1
            types_tools.idc = mock_idc
            result = types_tools.create_typedef("MyType", "unsigned int")
            self.assertIn("typedef", result)
            self.assertIn("MyType", result)
        finally:
            types_tools.idc = orig

    def test_failure_path(self):
        orig = types_tools.idc
        try:
            mock_idc = MagicMock()
            mock_idc.parse_decls.return_value = -1
            types_tools.idc = mock_idc
            result = types_tools.create_typedef("BadType", "???")
            self.assertIn("Failed", result)
        finally:
            types_tools.idc = orig


# ---------------------------------------------------------------------------
# import_c_header
# ---------------------------------------------------------------------------


class TestImportCHeader(unittest.TestCase):
    def test_idc_not_available(self):
        orig = types_tools.idc
        try:
            types_tools.idc = None
            result = types_tools.import_c_header("struct Foo { int x; };")
            self.assertIn("not available", result)
        finally:
            types_tools.idc = orig

    def test_parse_success(self):
        orig = types_tools.idc
        try:
            mock_idc = MagicMock()
            mock_idc.parse_decls.return_value = 3
            types_tools.idc = mock_idc
            result = types_tools.import_c_header("struct Foo { int x; };")
            self.assertIn("Successfully", result)
            self.assertIn("3", result)
        finally:
            types_tools.idc = orig

    def test_parse_failure(self):
        orig = types_tools.idc
        try:
            mock_idc = MagicMock()
            mock_idc.parse_decls.return_value = -1
            types_tools.idc = mock_idc
            result = types_tools.import_c_header("bad syntax")
            self.assertIn("Failed", result)
        finally:
            types_tools.idc = orig


# ---------------------------------------------------------------------------
# set_function_prototype
# ---------------------------------------------------------------------------


class TestSetFunctionPrototype(unittest.TestCase):
    def test_idc_not_available(self):
        orig = types_tools.idc
        try:
            types_tools.idc = None
            result = types_tools.set_function_prototype("0x1000", "void foo(void)")
            self.assertIn("not available", result)
        finally:
            types_tools.idc = orig

    def test_success(self):
        orig = types_tools.idc
        try:
            mock_idc = MagicMock()
            mock_idc.SetType.return_value = True
            types_tools.idc = mock_idc
            result = types_tools.set_function_prototype("0x1000", "void foo(void)")
            self.assertIn("Set prototype", result)
            self.assertIn("0x1000", result)
        finally:
            types_tools.idc = orig

    def test_failure(self):
        orig = types_tools.idc
        try:
            mock_idc = MagicMock()
            mock_idc.SetType.return_value = False
            types_tools.idc = mock_idc
            result = types_tools.set_function_prototype("0x1000", "bad proto")
            self.assertIn("Failed", result)
        finally:
            types_tools.idc = orig


# ---------------------------------------------------------------------------
# apply_type_to_variable
# ---------------------------------------------------------------------------


class TestApplyTypeToVariable(unittest.TestCase):
    def test_hexrays_not_available(self):
        orig = types_tools._HAS_HEXRAYS
        try:
            types_tools._HAS_HEXRAYS = False
            result = types_tools.apply_type_to_variable("0x1000", "v1", "int *")
            self.assertIn("not available", result)
        finally:
            types_tools._HAS_HEXRAYS = orig

    def test_ida_typeinf_not_available(self):
        orig_hr = types_tools._HAS_HEXRAYS
        orig_ti = types_tools.ida_typeinf
        try:
            types_tools._HAS_HEXRAYS = True
            types_tools.ida_typeinf = None
            result = types_tools.apply_type_to_variable("0x1000", "v1", "int *")
            self.assertIn("not available", result)
        finally:
            types_tools._HAS_HEXRAYS = orig_hr
            types_tools.ida_typeinf = orig_ti


# ---------------------------------------------------------------------------
# create_struct (IDA 9.x path — when _HAS_IDA_STRUCT is False)
# ---------------------------------------------------------------------------


class TestCreateStructIda9Path(unittest.TestCase):
    def test_ida_typeinf_not_available(self):
        orig_t = types_tools.ida_typeinf
        try:
            types_tools.ida_typeinf = None
            result = types_tools.create_struct("Foo", '[{"name": "x", "type": "int"}]')
            self.assertIn("not available", result)
        finally:
            types_tools.ida_typeinf = orig_t

    def test_offsetless_fields_append_sequentially_and_pass_may_destroy(self):
        """Regression: fields without an offset must not pile up at bit 0,
        and ETF_MAY_DESTROY must be forwarded to add_udm (else TERR_OVERLAP)."""
        calls = []

        class _FakeTif:
            def __init__(self):
                self._size = 0

            def create_udt(self, union):
                return True

            def get_size(self):
                return self._size

            def add_udm(self, *args):
                # 2nd form: (name, type, offset_bits, etf_flags)
                calls.append(args)
                self._size += 4
                return 0

            def set_named_type(self, til, name):
                return 0

            def is_udt(self):
                return True

            def get_named_type(self, til, name):
                return False

        fake_ti = MagicMock()
        fake_ti.TERR_OK = 0
        fake_ti.ETF_MAY_DESTROY = 4
        fake_ti.tinfo_t = _FakeTif
        # _parse_type fallback path helpers (not reached when str path works)
        fake_ti.PT_SIL = 0
        fake_ti.parse_decl.return_value = False

        orig_t = types_tools.ida_typeinf
        try:
            types_tools.ida_typeinf = fake_ti
            result = types_tools.create_struct("Foo", '[{"name": "a", "type": "int"}, {"name": "b", "type": "int"}]')
            self.assertIn("Created", result)
            # two add_udm calls recorded
            self.assertEqual(len(calls), 2)
            # field a at bit 0
            self.assertEqual(calls[0][0], "a")
            self.assertEqual(calls[0][2], 0)
            # ETF_MAY_DESTROY forwarded on both
            self.assertEqual(calls[0][3], 4)
            self.assertEqual(calls[1][3], 4)
            # field b appended after a (offset 32 bits, not 0)
            self.assertEqual(calls[1][0], "b")
            self.assertEqual(calls[1][2], 32)
        finally:
            types_tools.ida_typeinf = orig_t

    def test_overlap_explicit_offset_returns_message_not_raw_traceback(self):
        """Regression: a TERR_OVERLAP must surface as a message, not an exception."""

        class _OverlapTif:
            def __init__(self):
                self._size = 0

            def create_udt(self, union):
                return True

            def get_size(self):
                return self._size

            def set_named_type(self, til, name):
                return 0

            def is_udt(self):
                return True

            def get_named_type(self, til, name):
                return False

            def add_udm(self, *args):
                raise ValueError("Invalid input data: the member overlaps ...")

        def _raising_udm_t(*a, **k):
            raise ValueError("Invalid input data: the member overlaps ...")

        fake_ti = MagicMock()
        fake_ti.TERR_OK = 0
        fake_ti.ETF_MAY_DESTROY = 4
        fake_ti.PT_SIL = 0
        fake_ti.tinfo_t = _OverlapTif
        fake_ti.udm_t = _raising_udm_t

        orig_t = types_tools.ida_typeinf
        orig_parse = types_tools._parse_type
        try:
            types_tools.ida_typeinf = fake_ti
            types_tools._parse_type = lambda s: MagicMock()
            result = types_tools.create_struct(
                "Foo",
                '[{"name": "a", "type": "int"}, {"name": "b", "type": "int", "offset": 0}]',
            )
            self.assertIn("Failed to add field", result)
        finally:
            types_tools.ida_typeinf = orig_t
            types_tools._parse_type = orig_parse


# ---------------------------------------------------------------------------
# modify_struct (IDA 9.x path)
# ---------------------------------------------------------------------------


class TestModifyStructIda9Path(unittest.TestCase):
    def test_ida_typeinf_not_available(self):
        orig_t = types_tools.ida_typeinf
        try:
            types_tools.ida_typeinf = None
            result = types_tools.modify_struct("Foo", "add_field")
            self.assertIn("not available", result)
        finally:
            types_tools.ida_typeinf = orig_t


# ---------------------------------------------------------------------------
# get_struct_info (IDA 9.x path)
# ---------------------------------------------------------------------------


class TestGetStructInfoIda9Path(unittest.TestCase):
    def test_ida_typeinf_not_available(self):
        orig_t = types_tools.ida_typeinf
        try:
            types_tools.ida_typeinf = None
            result = types_tools.get_struct_info("Foo")
            self.assertIn("not available", result)
        finally:
            types_tools.ida_typeinf = orig_t


# ---------------------------------------------------------------------------
# list_structs (IDA 9.x path)
# ---------------------------------------------------------------------------


class TestListStructsIda9Path(unittest.TestCase):
    def test_ida_typeinf_not_available(self):
        orig_t = types_tools.ida_typeinf
        try:
            types_tools.ida_typeinf = None
            result = types_tools.list_structs()
            self.assertIn("not available", result)
        finally:
            types_tools.ida_typeinf = orig_t


# ---------------------------------------------------------------------------
# apply_struct_to_address (IDA 9.x path)
# ---------------------------------------------------------------------------


class TestApplyStructToAddressIda9Path(unittest.TestCase):
    def test_ida_typeinf_not_available(self):
        orig_t = types_tools.ida_typeinf
        try:
            types_tools.ida_typeinf = None
            result = types_tools.apply_struct_to_address("Foo", "0x1000")
            self.assertIn("not available", result)
        finally:
            types_tools.ida_typeinf = orig_t


# ---------------------------------------------------------------------------
# propagate_type (IDA 9.x path)
# ---------------------------------------------------------------------------


class TestPropagateTypeIda9Path(unittest.TestCase):
    def test_ida_typeinf_not_available(self):
        orig_t = types_tools.ida_typeinf
        try:
            types_tools.ida_typeinf = None
            result = types_tools.propagate_type("Foo")
            self.assertIn("not available", result)
        finally:
            types_tools.ida_typeinf = orig_t


# ---------------------------------------------------------------------------
# get_type_libraries
# ---------------------------------------------------------------------------


class TestGetTypeLibraries(unittest.TestCase):
    def test_idati_returns_none(self):
        orig = types_tools.ida_typeinf
        try:
            mock_ti = MagicMock()
            mock_ti.get_idati.return_value = None
            types_tools.ida_typeinf = mock_ti
            result = types_tools.get_type_libraries()
            self.assertIn("unavailable", result)
        finally:
            types_tools.ida_typeinf = orig

    def test_returns_library_list_header(self):
        orig = types_tools.ida_typeinf
        try:
            mock_ti = MagicMock()
            mock_idati = MagicMock()
            mock_idati.nbases = 0
            mock_ti.get_idati.return_value = mock_idati
            types_tools.ida_typeinf = mock_ti
            result = types_tools.get_type_libraries()
            self.assertIn("Type libraries", result)
        finally:
            types_tools.ida_typeinf = orig


# ---------------------------------------------------------------------------
# import_type_from_library
# ---------------------------------------------------------------------------


class TestImportTypeFromLibrary(unittest.TestCase):
    def test_type_already_exists_locally(self):
        orig = types_tools.ida_typeinf
        try:
            mock_ti = MagicMock()
            mock_tif = MagicMock()
            mock_tif.get_named_type.return_value = True
            mock_ti.tinfo_t.return_value = mock_tif
            types_tools.ida_typeinf = mock_ti
            result = types_tools.import_type_from_library("windows", "HANDLE")
            self.assertIn("already exists", result)
        finally:
            types_tools.ida_typeinf = orig

    def test_til_not_found(self):
        orig = types_tools.ida_typeinf
        try:
            mock_ti = MagicMock()
            mock_tif = MagicMock()
            mock_tif.get_named_type.return_value = False
            mock_ti.tinfo_t.return_value = mock_tif
            mock_ti.load_til.return_value = None
            types_tools.ida_typeinf = mock_ti
            result = types_tools.import_type_from_library("windows", "HANDLE")
            self.assertIn("not found", result)
        finally:
            types_tools.ida_typeinf = orig


# ---------------------------------------------------------------------------
# suggest_struct_from_accesses
# ---------------------------------------------------------------------------


class TestSuggestStructFromAccesses(unittest.TestCase):
    def test_hexrays_not_available(self):
        orig = types_tools._HAS_HEXRAYS
        try:
            types_tools._HAS_HEXRAYS = False
            result = types_tools.suggest_struct_from_accesses("0x1000")
            self.assertIn("not available", result)
        finally:
            types_tools._HAS_HEXRAYS = orig


# ---------------------------------------------------------------------------
# get_function_prototype
#
# Regression: previously called `idc.get_tinfo(tif, ea)` which raised
# "get_tinfo() takes 1 positional argument but 2 were given" — idc.get_tinfo
# takes only (ea), while the 2-arg (tif, ea) signature belongs to ida_nalt.
# As a getter used by mutation.py to capture pre-state for undo, the bug
# silently produced empty `old_prototype` records, breaking undo.
# ---------------------------------------------------------------------------


class TestGetFunctionPrototype(unittest.TestCase):
    def test_ida_nalt_not_available_returns_empty(self):
        orig = types_tools.ida_nalt
        try:
            types_tools.ida_nalt = None
            result = types_tools.get_function_prototype("0x1000")
            self.assertEqual(result, "")
        finally:
            types_tools.ida_nalt = orig

    def test_ida_typeinf_not_available_returns_empty(self):
        orig = types_tools.ida_typeinf
        try:
            types_tools.ida_typeinf = None
            result = types_tools.get_function_prototype("0x1000")
            self.assertEqual(result, "")
        finally:
            types_tools.ida_typeinf = orig

    def test_uses_ida_nalt_two_arg_signature(self):
        orig_ti = types_tools.ida_typeinf
        orig_nalt = types_tools.ida_nalt
        orig_idc = types_tools.idc
        try:
            mock_tif = MagicMock()
            mock_tif.__str__ = MagicMock(return_value="void __cdecl foo(int)")
            mock_ti = MagicMock()
            mock_ti.tinfo_t.return_value = mock_tif

            mock_nalt = MagicMock()
            mock_nalt.get_tinfo.return_value = True

            mock_idc = MagicMock()

            types_tools.ida_typeinf = mock_ti
            types_tools.ida_nalt = mock_nalt
            types_tools.idc = mock_idc

            result = types_tools.get_function_prototype("0x1000")

            # Must call ida_nalt.get_tinfo(tif, ea) — 2-arg signature
            mock_nalt.get_tinfo.assert_called_once()
            call_args = mock_nalt.get_tinfo.call_args
            self.assertEqual(len(call_args.args), 2)
            self.assertIs(call_args.args[0], mock_tif)
            # idc.get_tinfo must NOT be used (regression guard)
            mock_idc.get_tinfo.assert_not_called()
            # Output is str(tif) — raw C declaration per contract
            self.assertEqual(result, "void __cdecl foo(int)")
        finally:
            types_tools.ida_typeinf = orig_ti
            types_tools.ida_nalt = orig_nalt
            types_tools.idc = orig_idc

    def test_returns_empty_when_tinfo_unavailable(self):
        orig_nalt = types_tools.ida_nalt
        try:
            mock_nalt = MagicMock()
            mock_nalt.get_tinfo.return_value = False
            types_tools.ida_nalt = mock_nalt
            result = types_tools.get_function_prototype("0x1000")
            self.assertEqual(result, "")
        finally:
            types_tools.ida_nalt = orig_nalt


if __name__ == "__main__":
    unittest.main()

<!-- Source: https://github.com/cellebrite-labs/ida-docs/blob/main/skills/ida-docs/references/idapython-porting-guide-ida-9.md -->
<!-- Original content © Hex-Rays SA. Bundled here under the public Hex-Rays IDA SDK documentation. -->

# Porting Guide from IDA 8.x to 9.0

IDA 9.0 IDAPython changes and porting guide


> **How to use this Porting Guide?** This guide provides a comprehensive list of all changes in IDAPython API between IDA 8.4 and 9.0. Here’s how you can make the most of it:

> * Explore by module: navigate directly to a specific module and review the changes affecting it
> * Jump to [alternative examples](#alternative-examples) demonstrating how to port removed functions
> * Check [how-to examples](#how-to-examples) that demonstrate the usage of new/updated APIs

## Introduction

This guide provides information about what has been changed in the IDAPython API between IDA 8.4 and 9.0.

The largest change is due to the removal of two modules:

* `ida_struct`
* `ida_enum`

For years now, those 2 modules have been superseded by the `ida_typeinf` module, which offers similar functionality.

In case you are not familiar with [`ida_typeinf`'s main concepts](https://python.docs.hex-rays.com/namespaceida__typeinf.html), we recommend having a look at them first.

## ida_struct

1. `ida_struct` structures were accessed mostly through their index (or ID), while `ida_typeinf` adopts another approach using type names (or ordinals). Consequently, the notion of "structure index" bears less importance, and doesn't have a direct alternative.
2. many `ida_struct.get_struc_*` operations were accepting a `tid_t`. While the notion of `tid_t` is still present in IDA 9.0, it is not part of identifying a type anymore (a type is now identified either by its name, or its ordinal). The notion of `tid_t` is mostly used to "bind" types to data & functions in the IDB. For example, calling `ida_nalt.get_strid(address)` will return you such a `tid_t`. From a `tid_t`, you can load the corresponding `tinfo_t` object by using `tinfo_t(tid=id)`.

### Removed functions:

The table below provides alternatives to the functions that have been removed in IDA 9.0.

| 8.4                         | 9.0                                                                          |
| --------------------------- | ---------------------------------------------------------------------------- |
| `add_struc`                 | `tinfo_t.create_udt`                                                         |
| `add_struc_member`          | `tinfo_t.add_udm`                                                            |
| `del_struc`                 | `del_numbered_type`, `del_named_type`                                        |
| `del_struc_member`          | `tinfo_t.del_udm`                                                            |
| `del_struc_members`         | see [example](#del_struct_members)                                           |
| `expand_struc`              | `tinfo_t.expand_udt`                                                         |
| `get_best_fit_member`       | `udt_type_data_t.get_best_fit_member`                                        |
| `get_first_struc_idx`       | `til_t.numbered_types` (see [notes](#ida_struct))                            |
| `get_innermost_member`      | `tinfo_t.get_innermost_udm`                                                  |
| `get_last_struc_idx`        | see [notes](#ida_struct)                                                     |
| `get_max_offset`            | `tinfo_t.get_size` (structures), or `tinfo_t.get_udt_nmembers` (unions)      |
| `get_member`                | `tinfo_t.get_udm` / `tinfo_t.get_udm_by_offset`                              |
| `get_member_by_fullname`    | `get_udm_by_fullname`                                                        |
| `get_member_by_id`          | `tinfo_t.get_udm_by_tid`                                                     |
| `get_member_by_name`        | `tinfo_t.get_udm`                                                            |
| `get_member_cmt`            | `tinfo_t.get_udm` + `udm_t.cmt`                                              |
| `get_member_fullname`       | `get_tif_name`                                                               |
| `get_member_id`             | `tinfo_t(tid=...)` + `tinfo_t.get_udm_tid`                                   |
| `get_member_name`           | `tinfo_t(tid=...)` + `tinfo_t.get_udm` + `udm_t.name`                        |
| `get_member_size`           | `tinfo_t(tid=...)` + `tinfo_t.get_udm` + `udm_t.size`                        |
| `get_member_struc`          | `get_udm_by_fullname`                                                        |
| `get_member_tinfo`          | `udm_t.type`                                                                 |
| `get_next_member_idx`       | see [notes](#ida_struct)                                                     |
| `get_next_struc_idx`        | see [notes](#ida_struct)                                                     |
| `get_or_guess_member_tinfo` |                                                                              |
| `get_prev_member_idx`       | see [notes](#ida_struct)                                                     |
| `get_prev_struc_idx`        | see [notes](#ida_struct)                                                     |
| `get_sptr`                  | see [example](#get_sptr)                                                     |
| `get_struc`                 | `tinfo_t(tid=...)`                                                           |
| `get_struc_by_idx`          | see [notes](#ida_struct)                                                     |
| `get_struc_cmt`             | `tinfo_t.get_type_cmt`                                                       |
| `get_struc_first_offset`    |                                                                              |
| `get_struc_id`              | `get_named_type_tid`                                                         |
| `get_struc_idx`             | see [notes](#ida_struct)                                                     |
| `get_struc_last_offset`     | `tinfo_t.get_udt_details` + `udm_t.offset`                                   |
| `get_struc_name`            | `get_tid_name`                                                               |
| `get_struc_next_offset`     |                                                                              |
| `get_struc_prev_offset`     |                                                                              |
| `get_struc_qty`             | see [example](#get_struc_qty)                                                |
| `get_struc_size`            | `tinfo_t(tid=...)` + `tinfo_t.get_size`                                      |
| `is_anonymous_member_name`  | `ida_frame.is_anonymous_member_name`                                         |
| `is_dummy_member_name`      | `ida_frame.is_dummy_member_name`                                             |
| `is_member_id`              | `idc.is_member_id`                                                           |
| `is_special_member`         | see [example](#is_special_member)                                            |
| `is_union`                  | `tinfo_t(tid=...)` + `tinfo_t.is_union`                                      |
| `is_varmember`              | `udm_t.is_varmember`                                                         |
| `is_varstr`                 | `tinfo_t(tid=...)` + `tinfo_t.is_varstruct`                                  |
| `retrieve_member_info`      |                                                                              |
| `save_struc`                | `tinfo_t.save_type` / `tinfo_t.set_named_type` / `tinfo_t.set_numbered_type` |
| `set_member_cmt`            | `tinfo_t(tid=...)` + `tinfo_t.set_udm_cmt`                                   |
| `set_member_name`           | `tinfo_t(tid=...)` + `tinfo_t.rename_udm`                                    |
| `set_member_tinfo`          |                                                                              |
| `set_member_type`           | `tinfo_t(tid=...)` + `tinfo_t.set_udm_type`                                  |
| `set_struc_align`           |                                                                              |
| `set_struc_cmt`             | `tinfo_t(tid=...)` + `tinfo_t.set_type_cmt`                                  |
| `set_struc_hidden`          |                                                                              |
| `set_struc_idx`             |                                                                              |
| `set_struc_listed`          | `set_type_choosable`                                                         |
| `set_struc_name`            | `tinfo_t(tid=...)` + `tinfo_t.rename_type`                                   |
| `stroff_as_size`            | `ida_typeinf.stroff_as_size`                                                 |
| `struct_field_visitor_t`    | `ida_typeinf.tinfo_visitor_t`                                                |
| `unsync_and_delete_struc`   |                                                                              |
| `visit_stroff_fields`       |                                                                              |
| `visit_stroff_udms`         | `ida_typeinf.visit_stroff_udms`                                              |

### Removed methods and members

#### member_t

* `by_til` see `ida_typeinf.udm_t.is_by_til`
* `eoff`
* `flag`
* `get_size` use `ida_typeinf.udm_t.size // 8` instead.
* `get_soff` see `soff` below.
* `has_ti`
* `has_union`
* `id`
* `is_baseclass` see `ida_typeinf.udm_t.is_baseclass`
* `is_destructor` see `ida_typeinf.udm_t.can_be_dtor`
* `is_dupname`
* `props`
* `soff` use `ida_typeinf.udm_t.offset // 8` instead.
* `this`
* `thisown`
* `unimem`

#### struct_t

* `age`
* `from_til`
* `get_alignment`
* `get_last_member`
* `get_member`
* `has_union` see `ida_typeinf.tinfo_t.has_union`
* `id` see `ida_typeinf.tinfo_t.get_tid`
* `is_choosable`
* `is_copyof`
* `is_frame` see `ida_typeinf.tinfo_t.is_frame`
* `is_ghost`
* `is_hidden`
* `is_mappedto`
* `is_synced`
* `is_union` see `ida_typeinf.tinfo_t.is_union`
* `is_varstr` see `ida_typeinf.tinfo_t.is_varstruct`
* `like_union`
* `members`
* `memqty` see `ida_typeinf.tinfo_t.get_udt_nmembers`
* `ordinal` see `ida_typeinf.tinfo_t.get_ordinal`
* `props`
* `set_alignment`
* `thisown`

#### struct_field_visitor_t

* `visit_field`

#### udm_visitor_t

* `visit_udm`

## ida_enum

### Removed functions

The functions below 8.4 are removed those under 9.0 are alternatives.

The idc alternatives are based on:

* `ida_typeinf` module
* `ida_typeinf.tinfo_t`, the type info class
* `ida_typeinf.enum_type_data_t`, the enumeration type class
* `ida_typeinf.edm_t`, the enumeration member class

| 8.4                            | 9.0                           |
| ------------------------------ | ----------------------------- |
| `add_enum`                     | `idc.add_enum`                |
| `add_enum_member`              | `idc.add_enum_member`         |
| `del_enum`                     | `idc.del_enum`                |
| `del_enum_member`              | `idc.del_enum_member`         |
| `for_all_enum_members`         |                               |
| `get_bmask_cmt`                | `idc.get_bmask_cmt`           |
| `get_bmask_name`               | `idc.get_bmask_name`          |
| `get_enum`                     | `idc.get_enum`                |
| `get_enum_cmt`                 | `idc.get_enum_cmt`            |
| `get_enum_flag`                | `idc.get_enum_flag`           |
| `get_enum_idx`                 |                               |
| `get_enum_member`              | `idc.get_enum_member`         |
| `get_enum_member_bmask`        | `idc.get_enum_member_bmask`   |
| `get_enum_member_by_name`      | `idc.get_enum_member_by_name` |
| `get_enum_member_cmt`          | `idc.get_enum_member_cmt`     |
| `get_enum_member_enum`         | `idc.get_enum_member_enum`    |
| `get_enum_member_name`         | `idc.get_enum_member_name`    |
| `get_enum_member_serial`       |                               |
| `get_enum_member_value`        | `idc.get_enum_member_value`   |
| `get_enum_name`                | `idc.get_enum_name`           |
| `get_enum_name2`               |                               |
| `get_enum_qty`                 |                               |
| `get_enum_size`                | `idc.get_enum_size`           |
| `get_enum_type_ordinal`        |                               |
| `get_enum_width`               | `idc.get_enum_width`          |
| `get_first_bmask`              | `idc.get_first_bmask`         |
| `get_first_enum_member`        | `idc.get_first_enum_member`   |
| `get_first_serial_enum_member` |                               |
| `get_last_bmask`               | `idc.get_last_bmask`          |
| `get_last_enum_member`         | `idc.get_last_enum_member`    |
| `get_last_serial_enum_member`  |                               |
| `get_next_bmask`               | `idc.get_next_bmask`          |
| `get_next_enum_member`         | `idc.get_next_enum_member`    |
| `get_next_serial_enum_member`  |                               |
| `get_prev_bmask`               | `idc.get_prev_bmask`          |
| `get_prev_enum_member`         | `idc.get_prev_enum_member`    |
| `get_prev_serial_enum_member`  |                               |
| `getn_enum`                    |                               |
| `is_bf`                        | `idc.is_bf`                   |
| `is_enum_fromtil`              |                               |
| `is_enum_hidden`               |                               |
| `is_ghost_enum`                |                               |
| `is_one_bit_mask`              |                               |
| `set_bmask_cmt`                | `idc.set_bmask_cmt`           |
| `set_bmask_name`               | `idc.set_bmask_name`          |
| `set_enum_bf`                  | `idc.set_enum_bf`             |
| `set_enum_cmt`                 | `idc.set_enum_cmt`            |
| `set_enum_flag`                | `idc.set_enum_flag`           |
| `set_enum_fromtil`             |                               |
| `set_enum_ghost`               |                               |
| `set_enum_hidden`              |                               |
| `set_enum_idx`                 |                               |
| `set_enum_member_cmt`          | `idc.set_enum_member_cmt`     |
| `set_enum_member_name`         | `idc.set_enum_member_name`    |
| `set_enum_name`                | `idc.set_enum_name`           |
| `set_enum_type_ordinal`        |                               |
| `set_enum_width`               | `idc.set_enum_width`          |

### enum_member_visitor_t

* `visit_enum_member`

## ida_typeinf

### Removed functions

* `callregs_t_regcount`
* `get_ordinal_from_idb_type`
* `is_autosync`
* `get_udm_tid`: use `tinfo_t.get_udm_tid` as an alternative.
* `get_tinfo_tid`: use `tinfo_t.get_tid` as an alternative.
* `tinfo_t_get_stock`
* `get_ordinal_qty`: use `ida_typeinf.get_ordinal_count` or `ida_typeinf.get_ordinal_limit` as alternatives.
* `import_type`: use `idc.import_type` as an alternative.

### Added functions

* `detach_tinfo_t(_this: "tinfo_t") -> "bool"`
* `get_tinfo_by_edm_name(tif: "tinfo_t", til: "til_t", mname: "char const *") -> "ssize_t"`
* `stroff_as_size(plen: "int", tif: "tinfo_t", value: "asize_t") -> "bool"`
* `visit_stroff_udms(sfv: "udm_visitor_t", path: "tid_t const *", disp: "adiff_t *", appzero: "bool") -> "adiff_t *"`
* `is_one_bit_mask(mask: "uval_t") -> "bool"`
* `get_idainfo_by_udm(flags: "flags64_t *", ti: "opinfo_t", set_lzero: "bool *", ap: "array_parameters_t", udm: "udm_t") -> "bool"`

### Added class

#### udm_visitor_t

* `visit_udm`

### Removed methods

#### enum_type_data_t

* `get_constant_group`

### Added methods

#### callregs_t

* `set_registers(self, kind: "callregs_t::reg_kind_t", first_reg: "int", last_reg: "int") -> "void"`

#### enum_type_data_t

* `all_constants(self)`
* `all_groups(self, skip_trivial=False)`
* `get_constant_group(self, *args) -> "PyObject *"`
* `get_max_serial(self, value: "uint64") -> "uchar"`
* `get_serial(self, index: "size_t") -> "uchar"`

#### func_type_data_t.

* `find_argument(self, *args) -> "ssize_t"`

#### til_t

* `find_base(self, n: "char const *") -> "til_t *"`
* `get_type_names(self) -> "const char *"`

#### tinfo_t

* `detach(self) -> "bool"`
* `is_punknown(self) -> "bool"`
* `get_enum_nmembers(self) -> "size_t"`
* `is_empty_enum(self) -> "bool"`
* `get_enum_width(self) -> "int"`
* `calc_enum_mask(self) -> "uint64"`
* `get_edm_tid(self, idx: "size_t") -> "tid_t"`
* `is_udm_by_til(self, idx: "size_t") -> "bool"`
* `set_udm_by_til(self, idx: "size_t", on: "bool"=True, etf_flags: "uint"=0) -> "tinfo_code_t"`
* `set_fixed_struct(self, on: "bool"=True) -> "tinfo_code_t"`
* `set_struct_size(self, new_size: "size_t") -> "tinfo_code_t"`
* `is_fixed_struct(self) -> "bool"`
* `get_func_frame(self, pfn: "func_t const *") -> "bool"`
* `is_frame(self) -> "bool"`
* `get_frame_func(self) -> "ea_t"`
* `set_enum_radix(self, radix: "int", sign: "bool", etf_flags: "uint"=0) -> "tinfo_code_t"`
* `rename_funcarg(self, index: "size_t", name: "char const *", etf_flags: "uint"=0) -> "tinfo_code_t"`
* `set_funcarg_type(self, index: "size_t", tif: "tinfo_t", etf_flags: "uint"=0) -> "tinfo_code_t"`
* `set_func_rettype(self, tif: "tinfo_t", etf_flags: "uint"=0) -> "tinfo_code_t"`
* `del_funcargs(self, idx1: "size_t", idx2: "size_t", etf_flags: "uint"=0) -> "tinfo_code_t"`
* `del_funcarg(self, idx: "size_t", etf_flags: "uint"=0) -> "tinfo_code_t"`
* `add_funcarg(self, farg: "funcarg_t", etf_flags: "uint"=0, idx: "ssize_t"=-1) -> "tinfo_code_t"`
* `set_func_cc(self, cc: "cm_t", etf_flags: "uint"=0) -> "tinfo_code_t"`
* `set_funcarg_loc(self, index: "size_t", argloc: "argloc_t", etf_flags: "uint"=0) -> "tinfo_code_t"`
* `set_func_retloc(self, argloc: "argloc_t", etf_flags: "uint"=0) -> "tinfo_code_t"`
* `get_stkvar(self, insn: "insn_t const &", x: "op_t const", v: "sval_t") -> "ssize_t"`

#### udm_t

* `is_retaddr(self) -> "bool"`
* `is_savregs(self) -> "bool"`
* `is_special_member(self) -> "bool"`
* `is_by_til(self) -> "bool"`
* `set_retaddr(self, on: "bool"=True) -> "void"`
* `set_savregs(self, on: "bool"=True) -> "void`
* `set_by_til(self, on: "bool"=True) -> "void"`

#### udtmembervec_t

* `set_fixed(self, on: "bool"=True) -> "void"`

### Modified methods:

#### tinfo_t

| 8.4                                                                              | 9.0                                                                            |
| -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `find_udm(self, udm: "udmt_t *", strmem_flags: "int") -> "int"`                  | `find_udm(self, udm: "udmt_t *", strmem_flags: "int") -> "int"`                |
|                                                                                  | `find_udm(self, name: "char const *", strmem_flags: "int") -> "int"`           |
| `get_type_by_edm_name(self, mname: "const char *", til: "til_t"=None) -> "bool"` | `get_edm_by_name(self, mname: "char const *", til: "til_t"=None) -> "ssize_t"` |

## ida_frame

8.4 To access the structure of a function frame, use:

* `get_struc() (use func_t::frame as structure ID)`
* `get_frame(const func_t *pfn)`
* `get_frame(ea_t ea)`

9.0 To access the structure of a function frame, use:

* `tinfo_t::get_func_frame(const func_t *pfn)` as the preferred way.
* `get_func_frame(tinfo_t *out, const func_t *pfn)`

### Removed functions

* `get_stkvar`: see [tinfo_t](#tinfo_t)
* `get_frame`: see [tinfo_t.get-func_frame](#tinfo_t)
* `get_frame_member_by_id`
* `get_min_spd_ea`
* `delete_unreferenced_stkvars`
* `delete_wrong_stkvar_ops`

### Added functions

* `get_func_frame(out: "tinfo_t",pfn: "func_t const *") -> "bool"`
* `add_frame_member(pfn: "func_t const *", name: "char const *", offset: "uval_t", tif: "tinfo_t", repr: "value_repr_t"=None, etf_flags: "uint"=0) -> "bool"`
* `is_anonymous_member_name(name: "char const *") -> "bool"`
* `is_dummy_member_name(name: "char const *") -> "bool"`
* `is_special_frame_member(tid: "tid_t") -> "bool"`
* `set_frame_member_type(pfn: "func_t const *",offset: "uval_t", tif: "tinfo_t", repr: "value_repr_t"=None, etf_flags: "uint"=0) -> "bool"`
* `delete_frame_members(pfn: "func_t const *",start_offset: "uval_t", end_offset: "uval_t") -> "bool"`
* `calc_frame_offset(pfn: "func_t *", off: "sval_t", insn: "insn_t const *"=None, op: "op_t const *"=None) -> "sval_t"`

### Modified functions

| 8.4                                                                                                                                          | 9.0                                                                                                                        |
| -------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `define_stkvar(pfn: "func_t *", name: "const char *", off: "sval_t", flags: "flags64_t", ti: "const opinfo_t *", nbytes: "asize_t") -> bool` | `define_stkvar(pfn: "func_t *", name: "char const *", off: "sval_t", tif: "tinfo_t", repr: "value_repr_t"=None) -> "bool"` |

## ida_bytes

### Removed functions

* `free_chunck`
* `get_8bit`

### Added functions

* `find_bytes(bs: typing.Union[bytes, bytearray, str], range_start: int, range_size: typing.Optional[int] = None, range_end: typing.Optional[int] = ida_idaapi.BADADDR, mask: typing.Optional[typing.Union[bytes, bytearray]] = None, flags: typing.Optional[int] = BIN_SEARCH_FORWARD | BIN_SEARCH_NOSHOW, radix: typing.Optional[int] = 16, strlit_encoding: typing.Optional[typing.Union[int, str]] = PBSENC_DEF1BPU) -> int`
* `find_string(_str: str, range_start: int, range_end: typing.Optional[int] = ida_idaapi.BADADDR, range_size: typing.Optional[int] = None, strlit_encoding: typing.Optional[typing.Union[int, str]] = PBSENC_DEF1BPU, flags: typing.Optional[int] = BIN_SEARCH_FORWARD | BIN_SEARCH_NOSHOW) -> int`

### Modified functions

| 8.4                                                                                                                                 | 9.0                                                                                                                                  |
| ----------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `op_enum(ea: "ea_t", n: "int", id: "enum_t", serial: "uchar"=0) -> "bool"`                                                          | `op_enum(ea: "ea_t", n: "int", id: "tid_t", serial: "uchar"=0) -> "bool"`                                                            |
| `get_enum_id(ea: "ea_t", n: "int") -> "tid_t"`                                                                                      | `get_enum_id(ea: "ea_t", n: "int") -> "enum_t"`                                                                                      |
| `parse_binpat_str(out: "compiled_binpat_vec_t", ea: "ea_t", _in: "char const *", radix: "int", strlits_encoding: "int"=0) -> "str"` | `parse_binpat_str(out: "compiled_binpat_vec_t", ea: "ea_t", _in: "char const *", radix: "int", strlits_encoding: "int"=0) -> "bool"` |
| `bin_search3(start_ea: "ea_t", end_ea: "ea_t", data: "compiled_binpat_vec_t", flags: "int) -> ea_t`                                 | `bin_search(start_ea: "ea_t", end_ea: "ea_t", data: "compiled_binpat_vec_t const &", flags: "int") -> (ea_t, size_t)`                |
|                                                                                                                                     | `bin_search(start_ea: "ea_t", end_ea: "ea_t", image: "uchar const *", mask: "uchar const *", len: "size_t", flags: "int") -> ea_t`   |
| `get_octet2(ogen: "octet_generator_t") -> "uchar_t*"`                                                                               | `get_octet(ogen: "octet_generator_t") -> "uchar_t*"`                                                                                 |

## idc

### Removed functions

* `find_text` see [ida_bytes](#ida_bytes)
* `find_binary` see [ida_bytes](#ida_bytes)

## ida_dirtree

### Removed functions

* `dirtree_cursor_root_cursor`
* `dirtree_t_errstr`

## ida_diskio

### Removed functions

* `enumerate_files2`
* `eclose`

## ida_fpro

### Added functions

* `qflcose(fp: "FILE *") -> "int"`

## ida_funcs

### Added methods

#### func_item_iterator_t

* `set_ea(self, _ea: "ea_t") -> "bool"`

## ida_gdl

### Added classes

#### edge_t

#### edgevec_t

#### node_ordering_t

* `clear(self)`
* `resize(self, n: "int") -> "void"`
* `size(self) -> "size_t"`
* `set(self, _node: "int", num: "int") -> "void"`
* `clr(self, _node: "int") -> "bool"`
* `node(self, _order: "size_t") -> "int"`
* `order(self, _node: "int") -> "int"`

## ida_graph

### Removed classes

#### node_ordering_t

See [ida-gdl](#ida_gdl) `node_ordering_t` has been made an alias of `ida_gdl.node_ordering_t`

#### edge_t

See [ida-gdl](#ida_gdl) `edge_t` has been made an alias of `ida_gdl.edge_t`

### Renamed classes

| 8.4                | 9.0                   |
| ------------------ | --------------------- |
| `abstract_graph_t` | `drawable_graph_t`    |
| `mutable_graph_t`  | `interactive_graph_t` |

`abstract_graph_t` has been made an alias of `drawable_graph_t` `mutbale_graph_t` has been made an alias of `interactive_graph_t`

### Renamed functions

| 8.4                           | 9.0                               |
| ----------------------------- | --------------------------------- |
| `create_mutable_graph`        | `create_interactive_graph`        |
| `delete_mutable_graph`        | `delete_interactive_graph`        |
| `grcode_create_mutable_graph` | `grcode_create_interactive_graph` |

`create_mutable_graph` has been made an alias of `create_interactive_graph` `delete_mutable_graph` has been made an alias of `delete_interactive_graph` `grcode_create_mutable_graph` has been made an alias of `grcode_create_interactive_graph`

## ida_hexrays

### Removed functions

* `get_member_type`
* `checkout_hexrays_license`
* `cinsn_t_insn_is_epilog`

### Modified functions

| 8.4                                                                                                | 9.0                                                                                               |
| -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `save_user_labels2(func_ea: "ea_t", user_labels: "user_labels_t", func: "cfunc_t"=None) -> "void"` | `save_user_labels(func_ea: "ea_t", user_labels: "user_labels_t", func: "cfunc_t"=None) -> "void"` |
| `restore_user_labels2(func_ea: "ea_t", func: "cfunc_t"=None) -> "user_labels_t *"`                 | `restore_user_labels(func_ea: "ea_t", func: "cfunc_t"=None) -> "user_labels_t *"`                 |

### Added functions

* `max_vlr_value(size: "int") -> "uvlr_t"`
* `min_vlr_svalue(size: "int") -> "uvlr_t"`
* `max_vlr_svalue(size: "int") -> "uvlr_t"`
* `is_unsigned_cmpop(cmpop: "cmpop_t") -> "bool"`
* `is_signed_cmpop(cmpop: "cmpop_t") -> "bool"`
* `is_cmpop_with_eq(cmpop: "cmpop_t") -> "bool"`
* `is_cmpop_without_eq(cmpop: "cmpop_t") -> "bool"`

### Added classes

* `catchexpr_t`
* `ccatch_t`
* `ctry_t`
* `cthrow_t`
* `cblock_pos_t`

### Removed methods

#### vdui_t

* `set_strmem_type`
* `rename_strmem`

### Added methods

#### cinsn_list_t

* `splice(self, pos: "qlist< cinsn_t >::iterator", other: "cinsn_list_t", first: "qlist< cinsn_t >::iterator", last: "qlist< cinsn_t >::iterator") -> "void"`

#### Hexrays_Hooks

* `pre_structural(self, ct: "control_graph_t *", cfunc: "cfunc_t", g: "simple_graph_t") -> "int"`
* `begin_inlining(self, cdg: "codegen_t", decomp_flags: "int") -> "int"`
* `inlining_func(self, cdg: "codegen_t", blk: "int", mbr: "mba_ranges_t") -> "int"`
* `inlined_func(self, cdg: "codegen_t", blk: "int", mbr: "mba_ranges_t", i1: "int", i2: "int") -> "int"`
* `collect_warnings(self, warnings: "qstrvec_t *", cfunc: "cfunc_t") -> "int"`

#### lvar_t

* `was_scattered_arg(self) -> "bool"`
* `set_scattered_arg(self) -> "void"`
* `clr_scattered_arg(self) -> "void"`

#### lvars_t

* `find_input_reg(self, reg: "int", _size: "int"=1) -> "int"`

#### simple_graph_t

* `compute_dominators(self, domin: "array_of_node_bitset_t &", post: "bool"=False) -> "void"`
* `compute_immediate_dominators(self, domin: "array_of_node_bitset_t const &", idomin: "intvec_t", post: "bool"=False) -> "void"`
* `depth_first_preorder(self, pre: "node_ordering_t") -> "int"`
* `depth_first_postorder(self, post: "node_ordering_t") -> "int"`
* `begin(self) -> "simple_graph_t::iterator"`
* `end(self) -> "simple_graph_t::iterator"`
* `front(self) -> "int"`
* `inc(self, p: "simple_graph_t::iterator &", n: "int"=1) -> "void"`
* `goup(self, node: "int") -> "int"`

#### fnumber_t

* `calc_max_exp(self) -> "int"`
* `is_nan(self) -> "bool"`

#### minsn_t

* `was_unpaired(self) -> "bool"`

#### mba_t

* `split_block(self, blk: "mblock_t", start_insn: "minsn_t") -> "mblock_t *"`
* `inline_func(self, cdg: "codegen_t", blknum: "int", ranges: "mba_ranges_t", decomp_flags: "int"=0, inline_flags: "int"=0) -> "merror_t"`
* `locate_stkpnt(self, ea: "ea_t") -> "stkpnt_t const *"`

#### codegen_t

* `clear(self) -> "void"`

### Modified methods

#### Hexrays_Hooks

| 8.4                                             | 9.0                                                           |
| ----------------------------------------------- | ------------------------------------------------------------- |
| `flowchart(self, fc: "qflow_chart_t") -> "int"` | `flowchart(self, fc: "qflow_chart_t", mba: "mba_t") -> "int"` |

#### valrng_t

| 8.4                                          | 9.0                            |
| -------------------------------------------- | ------------------------------ |
| `cvt_to_cmp(self, strict: "bool") -> "bool"` | `cvt_to_cmp(self) -> "bool"`   |
| `max_value(self, size_ : "int") -> "uvlr_t"` | `max_value(self) -> "uvlr_t"`  |
| `min_svalue(self, size_: "int") -> "uvlr_t"` | `min_svalue(self) -> "uvlr_t"` |
| `max_svalue(self, size_: "int") -> "uvlr_t"` | `max_svalue(self) -> "uvlr_t"` |

#### stkvar_ref_t

| 8.4                                                        | 9.0                                                                        |
| ---------------------------------------------------------- | -------------------------------------------------------------------------- |
| `get_stkvar(self, p_off=None: "uval_t *") -> "member_t *"` | `get_stkvar(self, udm: "udm_t"=None, p_off: "uval_t *"=None) -> "ssize_t"` |

#### mop_t

| 8.4                                                   | 9.0                                                                        |
| ----------------------------------------------------- | -------------------------------------------------------------------------- |
| `get_stkvar(self, p_off: "uval_t *") -> "member_t *"` | `get_stkvar(self, udm: "udm_t"=None, p_off: "uval_t *"=None) -> "ssize_t"` |

## ida_ida

### Added classes

#### idbattr_valmap_t

#### idbattr_info_t

* `is_node_altval(self) -> "bool"`
* `is_node_supval(self) -> "bool"`
* `is_node_valobj(self) -> "bool"`
* `is_node_blob(self) -> "bool"`
* `is_node_var(self) -> "bool"`
* `is_struc_field(self) -> "bool"`
* `is_cstr(self) -> "bool"`
* `is_qstring(self) -> "bool"`
* `is_bytearray(self) -> "bool"`
* `is_buf_var(self) -> "bool"`
* `is_decimal(self) -> "bool"`
* `is_hexadecimal(self) -> "bool"`
* `is_readonly_var(self) -> "bool"`
* `is_incremented(self) -> "bool"`
* `is_val_mapped(self) -> "bool"`
* `is_hash(self) -> "bool"`
* `use_hlpstruc(self) -> "bool"`
* `is_bitmap(self) -> "bool"`
* `is_onoff(self) -> "bool"`
* `is_scalar_var(self) -> "bool"`
* `is_bitfield(self) -> "bool"`
* `is_boolean(self) -> "bool"`
* `has_individual_node(self) -> "bool"`
* `str_true(self) -> "char const *"`
* `str_false(self) -> "char const *"`
* `ridx(self) -> "size_t"`
* `hashname(self) -> "char const *"`

### inf_structure accessors

As will be shown in [ida_idaapi Removed functions](#removed-functions-9) `get_inf_structure` has been removed. It has been replaced by the following accessors.

#### Replacement examples:

| In 8.4                                      | In 9.0                           |
| ------------------------------------------- | -------------------------------- |
| `ida_idaapi.get_inf_structure().procname`   | `ida_ida.inf_get_procname()`     |
| `ida_idaapi.get_inf_structure().max_ea`     | `ida_ida.inf_get_max_ea()`       |
| `ida_idaapi.get_inf_structure().is_32bit()` | `ida_ida.inf_is_32bit_exactly()` |

The list of getters and setters is given below.

#### inf_structure getters

* `inf_get_version() -> "ushort"`
* `inf_get_genflags() -> "ushort"`
* `inf_get_lflags() -> "uint32"`
* `inf_get_app_bitness() -> "uint"`
* `inf_get_database_change_count() -> "uint32"`
* `inf_get_filetype() -> "filetype_t"`
* `inf_get_ostype() -> "ushort"`
* `inf_get_apptype() -> "ushort"`
* `inf_get_asmtype() -> "uchar"`
* `inf_get_specsegs() -> "uchar"`
* `inf_get_af() -> "uint32"`
* `inf_get_af2() -> "uint32"`
* `inf_get_baseaddr() -> "uval_t"`
* `inf_get_start_ss() -> "sel_t"`
* `inf_get_start_cs() -> "sel_t"`
* `inf_get_start_ip() -> "ea_t"`
* `inf_get_start_ea() -> "ea_t"`
* `inf_get_start_sp() -> "ea_t"`
* `inf_get_main() -> "ea_t"`
* `inf_get_min_ea() -> "ea_t"`
* `inf_get_max_ea() -> "ea_t"`
* `inf_get_omin_ea() -> "ea_t"`
* `inf_get_omax_ea() -> "ea_t"`
* `inf_get_lowoff() -> "ea_t"`
* `inf_get_highoff() -> "ea_t"`
* `inf_get_maxref() -> "uval_t"`
* `inf_get_netdelta() -> "sval_t"`
* `inf_get_xrefnum() -> "uchar"`
* `inf_get_type_xrefnum() -> "uchar"`
* `inf_get_refcmtnum() -> "uchar"`
* `inf_get_xrefflag() -> "uchar"`
* `inf_get_max_autoname_len() -> "ushort"`
* `inf_get_nametype() -> "char"`
* `inf_get_short_demnames() -> "uint32"`
* `inf_get_long_demnames() -> "uint32"`
* `inf_get_demnames() -> "uchar"`
* `inf_get_listnames() -> "uchar"`
* `inf_get_indent() -> "uchar"`
* `inf_get_cmt_indent() -> "uchar"`
* `inf_get_margin() -> "ushort"`
* `inf_get_lenxref() -> "ushort"`
* `inf_get_outflags() -> "uint32"`
* `inf_get_cmtflg() -> "uchar"`
* `inf_get_limiter() -> "uchar"`
* `inf_get_bin_prefix_size() -> "short"`
* `inf_get_prefflag() -> "uchar"`
* `inf_get_strlit_flags() -> "uchar"`
* `inf_get_strlit_break() -> "uchar"`
* `inf_get_strlit_zeroes() -> "char"`
* `inf_get_strtype() -> "int32"`
* `inf_get_strlit_sernum() -> "uval_t"`
* `inf_get_datatypes() -> "uval_t"`
* `inf_get_abibits() -> "uint32"`
* `inf_get_appcall_options() -> "uint32"`
* `inf_get_privrange_start_ea() -> "ea_t"`
* `inf_get_privrange_end_ea() -> "ea_t"`
* `inf_get_cc_id() -> "comp_t"`
* `inf_get_cc_cm() -> "cm_t"`
* `inf_get_cc_size_i() -> "uchar"`
* `inf_get_cc_size_b() -> "uchar"`
* `inf_get_cc_size_e() -> "uchar"`
* `inf_get_cc_defalign() -> "uchar"`
* `inf_get_cc_size_s() -> "uchar"`
* `inf_get_cc_size_l() -> "uchar"`
* `inf_get_cc_size_ll() -> "uchar"`
* `inf_get_cc_size_ldbl() -> "uchar"`
* `inf_get_procname() -> "size_t"`
* `inf_get_strlit_pref() -> "size_t"`
* `inf_get_cc(out: "compiler_info_t") -> "bool"`
* `inf_get_privrange(*args) -> "range_t"`
* `inf_get_af_low() -> "ushort"`
* `inf_get_af_high() -> "ushort"`
* `inf_get_af2_low() -> "ushort"`
* `inf_get_pack_mode() -> "int"`
* `inf_get_demname_form() -> "uchar"`
* `inf_is_auto_enabled() -> "bool"`
* `inf_is_graph_view() -> "bool"`
* `inf_is_32bit_or_higher() -> "bool"`
* `inf_is_32bit_exactly() -> "bool"`
* `inf_is_16bit() -> "bool"`
* `inf_is_64bit() -> "bool"`
* `inf_is_dll() -> "bool"`
* `inf_is_flat_off32() -> "bool"`
* `inf_is_be() -> "bool"`
* `inf_is_wide_high_byte_first() -> "bool"`
* `inf_is_snapshot() -> "bool"`
* `inf_is_kernel_mode() -> "bool"`
* `inf_is_limiter_thin() -> "bool"`
* `inf_is_limiter_thick() -> "bool"`
* `inf_is_limiter_empty() -> "bool"`
* `inf_is_mem_aligned4() -> "bool"`
* `inf_is_hard_float() -> "bool"`
* `inf_abi_set_by_user() -> "bool"`
* `inf_allow_non_matched_ops() -> "bool"`
* `inf_allow_sigmulti() -> "bool"`
* `inf_append_sigcmt() -> "bool"`
* `inf_big_arg_align(*args) -> "bool"`
* `inf_check_manual_ops() -> "bool"`
* `inf_check_unicode_strlits() -> "bool"`
* `inf_coagulate_code() -> "bool"`
* `inf_coagulate_data() -> "bool"`
* `inf_compress_idb() -> "bool"`
* `inf_create_all_xrefs() -> "bool"`
* `inf_create_func_from_call() -> "bool"`
* `inf_create_func_from_ptr() -> "bool"`
* `inf_create_func_tails() -> "bool"`
* `inf_create_jump_tables() -> "bool"`
* `inf_create_off_on_dref() -> "bool"`
* `inf_create_off_using_fixup() -> "bool"`
* `inf_create_strlit_on_xref() -> "bool"`
* `inf_data_offset() -> "bool"`
* `inf_dbg_no_store_path() -> "bool"`
* `inf_decode_fpp() -> "bool"`
* `inf_del_no_xref_insns() -> "bool"`
* `inf_final_pass() -> "bool"`
* `inf_full_sp_ana() -> "bool"`
* `inf_gen_assume() -> "bool"`
* `inf_gen_lzero() -> "bool"`
* `inf_gen_null() -> "bool"`
* `inf_gen_org() -> "bool"`
* `inf_huge_arg_align(cc: cm_t) -> "bool"`
* `inf_like_binary() -> "bool":`
* `inf_line_pref_with_seg() -> "bool"`
* `inf_loading_idc() -> "bool"`
* `inf_macros_enabled() -> "bool"`
* `inf_map_stkargs() -> "bool"`
* `inf_mark_code() -> "bool"`
* `inf_merge_strlits() -> "bool"`
* `inf_no_store_user_info() -> "bool"`
* `inf_noflow_to_data() -> "bool"`
* `inf_noret_ana() -> "bool"`
* `inf_op_offset() -> "bool"`
* `inf_pack_idb() -> "bool"`
* `inf_pack_stkargs(*args) -> "bool"`
* `inf_prefix_show_funcoff() -> "bool"`
* `inf_prefix_show_segaddr() -> "bool"`
* `inf_prefix_show_stack() -> "bool"`
* `inf_prefix_truncate_opcode_bytes() -> "bool"`
* `inf_propagate_regargs() -> "bool"`
* `inf_propagate_stkargs() -> "bool"`
* `inf_readonly_idb() -> "bool"`
* `inf_rename_jumpfunc() -> "bool"`
* `inf_rename_nullsub() -> "bool"`
* `inf_should_create_stkvars() -> "bool"`
* `inf_should_trace_sp() -> "bool"`
* `inf_show_all_comments() -> "bool"`
* `inf_show_auto() -> "bool"`
* `inf_show_hidden_funcs() -> "bool"`
* `inf_show_hidden_insns() -> "bool"`
* `inf_show_hidden_segms() -> "bool"`
* `inf_show_line_pref() -> "bool"`
* `inf_show_repeatables() -> "bool"`
* `inf_show_src_linnum() -> "bool"`
* `inf_show_void() -> "bool"`
* `inf_show_xref_fncoff() -> "bool"`
* `inf_show_xref_seg() -> "bool"`
* `inf_show_xref_tmarks() -> "bool"`
* `inf_show_xref_val() -> "bool"`
* `inf_stack_ldbl() -> "bool"`
* `inf_stack_varargs() -> "bool"`
* `inf_strlit_autocmt() -> "bool"`
* `inf_strlit_name_bit() -> "bool"`
* `inf_strlit_names() -> "bool"`
* `inf_strlit_savecase() -> "bool"`
* `inf_strlit_serial_names() -> "bool"`
* `inf_test_mode() -> "bool"`
* `inf_trace_flow() -> "bool"`
* `inf_truncate_on_del() -> "bool"`
* `inf_unicode_strlits() -> "bool"`
* `inf_use_allasm() -> "bool"`
* `inf_use_flirt() -> "bool"`
* `inf_use_gcc_layout() -> "bool"`

#### inf_structure setters

* `inf_set_allow_non_matched_ops(_v: "bool"=True) -> "bool"`
* `inf_set_graph_view(_v: "bool"=True) -> "bool"`
* `inf_set_lflags(_v: "uint32") -> "bool"`
* `inf_set_decode_fpp(_v: "bool"=True) -> "bool"`
* `inf_set_32bit(_v: "bool"=True) -> "bool"`
* `inf_set_64bit(_v: "bool"=True) -> "bool"`
* `inf_set_dll(_v: "bool"=True) -> "bool"`
* `inf_set_flat_off32(_v: "bool"=True) -> "bool"`
* `inf_set_be(_v: "bool"=True) -> "bool"`
* `inf_set_wide_high_byte_first(_v: "bool"=True) -> "bool"`
* `inf_set_dbg_no_store_path(_v: "bool"=True) -> "bool"`
* `inf_set_snapshot(_v: "bool"=True) -> "bool"`
* `inf_set_pack_idb(_v: "bool"=True) -> "bool"`
* `inf_set_compress_idb(_v: "bool"=True) -> "bool"`
* `inf_set_kernel_mode(_v: "bool"=True) -> "bool"`
* `inf_set_app_bitness(bitness: "uint") -> "void"`
* `inf_set_database_change_count(_v: "uint32") -> "bool"`
* `inf_set_filetype(_v: "filetype_t") -> "bool"`
* `inf_set_ostype(_v: "ushort") -> "bool"`
* `inf_set_apptype(_v: "ushort") -> "bool"`
* `inf_set_asmtype(_v: "uchar") -> "bool"`
* `inf_set_specsegs(_v: "uchar") -> "bool"`
* `inf_set_af(_v: "uint32") -> "bool"`
* `inf_set_trace_flow(_v: "bool"=True) -> "bool"`
* `inf_set_mark_code(_v: "bool"=True) -> "bool"`
* `inf_set_create_jump_tables(_v: "bool"=True) -> "bool"`
* `inf_set_noflow_to_data(_v: "bool"=True) -> "bool"`
* `inf_set_create_all_xrefs(_v: "bool"=True) -> "bool"`
* `inf_set_del_no_xref_insns(_v: "bool"=True) -> "bool"`
* `inf_set_create_func_from_ptr(_v: "bool"=True) -> "bool"`
* `inf_set_create_func_from_call(_v: "bool"=True) -> "bool"`
* `inf_set_create_func_tails(_v: "bool"=True) -> "bool"`
* `inf_set_should_create_stkvars(_v: "bool"=True) -> "bool"`
* `inf_set_propagate_stkargs(_v: "bool"=True) -> "bool"`
* `inf_set_propagate_regargs(_v: "bool"=True) -> "bool"`
* `inf_set_should_trace_sp(_v: "bool"=True) -> "bool"`
* `inf_set_full_sp_ana(_v: "bool"=True) -> "bool"`
* `inf_set_noret_ana(_v: "bool"=True) -> "bool"`
* `inf_set_guess_func_type(_v: "bool"=True) -> "bool"`
* `inf_set_truncate_on_del(_v: "bool"=True) -> "bool"`
* `inf_set_create_strlit_on_xref(_v: "bool"=True) -> "bool"`
* `inf_set_check_unicode_strlits(_v: "bool"=True) -> "bool"`
* `inf_set_create_off_using_fixup(_v: "bool"=True) -> "bool"`
* `inf_set_create_off_on_dref(_v: "bool"=True) -> "bool"`
* `inf_set_op_offset(_v: "bool"=True) -> "bool"`
* `inf_set_data_offset(_v: "bool"=True) -> "bool"`
* `inf_set_use_flirt(_v: "bool"=True) -> "bool"`
* `inf_set_append_sigcmt(_v: "bool"=True) -> "bool"`
* `inf_set_allow_sigmulti(_v: "bool"=True) -> "bool"`
* `inf_set_hide_libfuncs(_v: "bool"=True) -> "bool"`
* `inf_set_rename_jumpfunc(_v: "bool"=True) -> "bool"`
* `inf_set_rename_nullsub(_v: "bool"=True) -> "bool"`
* `inf_set_coagulate_data(_v: "bool"=True) -> "bool"`
* `inf_set_coagulate_code(_v: "bool"=True) -> "bool"`
* `inf_set_final_pass(_v: "bool"=True) -> "bool"`
* `inf_set_af2(_v: "uint32") -> "bool"`
* `inf_set_handle_eh(_v: "bool"=True) -> "bool"`
* `inf_set_handle_rtti(_v: "bool"=True) -> "bool"`
* `inf_set_macros_enabled(_v: "bool"=True) -> "bool"`
* `inf_set_merge_strlits(_v: "bool"=True) -> "bool"`
* `inf_set_baseaddr(_v: "uval_t") -> "bool"`
* `inf_set_start_ss(_v: "sel_t") -> "bool"`
* `inf_set_start_cs(_v: "sel_t") -> "bool"`
* `inf_set_start_ip(_v: "ea_t") -> "bool"`
* `inf_set_start_ea(_v: "ea_t") -> "bool"`
* `inf_set_start_sp(_v: "ea_t") -> "bool"`
* `inf_set_main(_v: "ea_t") -> "bool"`
* `inf_set_min_ea(_v: "ea_t") -> "bool"`
* `inf_set_max_ea(_v: "ea_t") -> "bool"`
* `inf_set_omin_ea(_v: "ea_t") -> "bool"`
* `inf_set_omax_ea(_v: "ea_t") -> "bool"`
* `inf_set_lowoff(_v: "ea_t") -> "bool"`
* `inf_set_highoff(_v: "ea_t") -> "bool"`
* `inf_set_maxref(_v: "uval_t") -> "bool"`
* `inf_set_netdelta(_v: "sval_t") -> "bool"`
* `inf_set_xrefnum(_v: "uchar") -> "bool"`
* `inf_set_type_xrefnum(_v: "uchar") -> "bool"`
* `inf_set_refcmtnum(_v: "uchar") -> "bool"`
* `inf_set_xrefflag(_v: "uchar") -> "bool"`
* `inf_set_show_xref_seg(_v: "bool"=True) -> "bool"`
* `inf_set_show_xref_tmarks(_v: "bool"=True) -> "bool"`
* `inf_set_show_xref_fncoff(_v: "bool"=True) -> "bool"`
* `inf_set_show_xref_val(_v: "bool"=True) -> "bool"`
* `inf_set_max_autoname_len(_v: "ushort") -> "bool"`
* `inf_set_nametype(_v: "char") -> "bool"`
* `inf_set_short_demnames(_v: "uint32") -> "bool"`
* `inf_set_long_demnames(_v: "uint32") -> "bool"`
* `inf_set_demnames(_v: "uchar") -> "bool"`
* `inf_set_listnames(_v: "uchar") -> "bool"`
* `inf_set_indent(_v: "uchar") -> "bool"`
* `inf_set_cmt_indent(_v: "uchar") -> "bool"`
* `inf_set_margin(_v: "ushort") -> "bool"`
* `inf_set_lenxref(_v: "ushort") -> "bool"`
* `inf_set_outflags(_v: "uint32") -> "bool"`
* `inf_set_show_void(_v: "bool"=True) -> "bool"`
* `inf_set_show_auto(_v: "bool"=True) -> "bool"`
* `inf_set_gen_null(_v: "bool"=True) -> "bool"`
* `inf_set_show_line_pref(_v: "bool"=True) -> "bool"`
* `inf_set_line_pref_with_seg(_v: "bool"=True) -> "bool"`
* `inf_set_gen_lzero(_v: "bool"=True) -> "bool"`
* `inf_set_gen_org(_v: "bool"=True) -> "bool"`
* `inf_set_gen_assume(_v: "bool"=True) -> "bool"`
* `inf_set_gen_tryblks(_v: "bool"=True) -> "bool"`
* `inf_set_cmtflg(_v: "uchar") -> "bool"`
* `inf_set_show_repeatables(_v: "bool"=True) -> "bool"`
* `inf_set_show_all_comments(_v: "bool"=True) -> "bool"`
* `inf_set_hide_comments(_v: "bool"=True) -> "bool"`
* `inf_set_show_src_linnum(_v: "bool"=True) -> "bool"`
* `inf_set_show_hidden_insns(_v: "bool"=True) -> "bool"`
* `inf_set_show_hidden_funcs(_v: "bool"=True) -> "bool"`
* `inf_set_show_hidden_segms(_v: "bool"=True) -> "bool"`
* `inf_set_limiter(_v: "uchar") -> "bool"`
* `inf_set_limiter_thin(_v: "bool"=True) -> "bool"`
* `inf_set_limiter_thick(_v: "bool"=True) -> "bool"`
* `inf_set_limiter_empty(_v: "bool"=True) -> "bool"`
* `inf_set_bin_prefix_size(_v: "short") -> "bool"`
* `inf_set_prefflag(_v: "uchar") -> "bool"`
* `inf_set_prefix_show_segaddr(_v: "bool"=True) -> "bool"`
* `inf_set_prefix_show_funcoff(_v: "bool"=True) -> "bool"`
* `inf_set_prefix_show_stack(_v: "bool"=True) -> "bool"`
* `inf_set_prefix_truncate_opcode_bytes(_v: "bool"=True) -> "bool"`
* `inf_set_strlit_flags(_v: "uchar") -> "bool"`
* `inf_set_strlit_names(_v: "bool"=True) -> "bool"`
* `inf_set_strlit_name_bit(_v: "bool"=True) -> "bool"`
* `inf_set_strlit_serial_names(_v: "bool"=True) -> "bool"`
* `inf_set_unicode_strlits(_v: "bool"=True) -> "bool"`
* `inf_set_strlit_autocmt(_v: "bool"=True) -> "bool"`
* `inf_set_strlit_savecase(_v: "bool"=True) -> "bool"`
* `inf_set_strlit_break(_v: "uchar") -> "bool"`
* `inf_set_strlit_zeroes(_v: "char") -> "bool"`
* `inf_set_strtype(_v: "int32") -> "bool"`
* `inf_set_strlit_sernum(_v: "uval_t") -> "bool"`
* `inf_set_datatypes(_v: "uval_t") -> "bool"`
* `inf_set_abibits(_v: "uint32") -> "bool"`
* `inf_set_mem_aligned4(_v: "bool"=True) -> "bool"`
* `inf_set_pack_stkargs(_v: "bool"=True) -> "bool"`
* `inf_set_big_arg_align(_v: "bool"=True) -> "bool"`
* `inf_set_stack_ldbl(_v: "bool"=True) -> "bool"`
* `inf_set_stack_varargs(_v: "bool"=True) -> "bool"`
* `inf_set_hard_float(_v: "bool"=True) -> "bool"`
* `inf_set_abi_set_by_user(_v: "bool"=True) -> "bool"`
* `inf_set_use_gcc_layout(_v: "bool"=True) -> "bool"`
* `inf_set_map_stkargs(_v: "bool"=True) -> "bool"`
* `inf_set_huge_arg_align(_v: "bool"=True) -> "bool"`
* `inf_set_appcall_options(_v: "uint32") -> "bool"`
* `inf_set_privrange_start_ea(_v: "ea_t") -> "bool"`
* `inf_set_privrange_end_ea(_v: "ea_t") -> "bool"`
* `inf_set_cc_id(_v: "comp_t") -> "bool"`
* `inf_set_cc_cm(_v: "cm_t") -> "bool"`
* `inf_set_cc_size_i(_v: "uchar") -> "bool"`
* `inf_set_cc_size_b(_v: "uchar") -> "bool"`
* `inf_set_cc_size_e(_v: "uchar") -> "bool"`
* `inf_set_cc_defalign(_v: "uchar") -> "bool"`
* `inf_set_cc_size_s(_v: "uchar") -> "bool"`
* `inf_set_cc_size_l(_v: "uchar") -> "bool"`
* `inf_set_cc_size_ll(_v: "uchar") -> "bool"`
* `inf_set_cc_size_ldbl(_v: "uchar") -> "bool"`
* `inf_set_procname(*args) -> "bool"`
* `inf_set_strlit_pref(*args) -> "bool"`
* `inf_set_cc(_v: "compiler_info_t") -> "bool"`
* `inf_set_privrange(_v: "range_t") -> "bool"`
* `inf_set_af_low(saf: "ushort") -> "void"`
* `inf_set_af_high(saf2: "ushort") -> "void"`
* `inf_set_af2_low(saf: "ushort") -> "void"`
* `inf_set_pack_mode(pack_mode: "int") -> "int"`
* `inf_inc_database_change_count(cnt: "int"=1) -> "void"`

## ida_idaapi

### Removed functions

* `get_inf_structure` see [inf_structure getters](#getters) and [inf_structure setters](#setters)
* `loader_input_t_from_linput`
* `loader_input_t_from_capsule`
* `loader_input_t_from_fp`

## ida_idd

### Added functions

* `cpu2ieee(ieee_out: "fpvalue_t *", cpu_fpval: "void const *", size: "int") -> "int"`
* `ieee2cpu(cpu_fpval: "void *", ieee_out: "fpvalue_t const &", size: "int") -> "int"`

## ida_idp

See also [IDB events](#idb-events) below.

### Removed methods

#### _processor_t

* `has_realcvt`

### processor_t

* `get_uFlag`

### Modified methods

#### _processor_t

| 8.4                                                                                     | 9.0                                                                                        |
| --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `gen_stkvar_def(ctx: "outctx_t &", mptr: "member_t const *", v: : "sval_t") -> ssize_t` | `gen_stkvar_def(ctx: "outctx_t &", mptr: "udm_t", v: "sval_t", tid: "tid_t") -> "ssize_t"` |

#### IDP_Hooks

| 8.4                                       | 9.0                                                                                                  |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `ev_gen_stkvar_def(self, *args) -> "int"` | `ev_gen_stkvar_def(self, outctx: "outctx_t *", stkvar: "udm_t", v: "sval_t", tid: "tid_t") -> "int"` |

### Added methods

#### IDB_Hooks

* `lt_udm_created(self, udtname: "char const *", udm: "udm_t") -> "void"`
* `lt_udm_deleted(self, udtname: "char const *", udm_tid: "tid_t", udm: "udm_t") -> "void"`
* `lt_udm_renamed(self, udtname: "char const *", udm: "udm_t", oldname: "char const *") -> "void"`
* `lt_udm_changed(self, udtname: "char const *", udm_tid: "tid_t", udmold: "udm_t", udmnew: "udm_t") -> "void"`
* `lt_udt_expanded(self, udtname: "char const *", udm_tid: "tid_t", delta: "adiff_t") -> "void"`
* `frame_created(self, func_ea: "ea_t") -> "void"`
* `frame_udm_created(self, func_ea: "ea_t", udm: "udm_t") -> "void"`
* `frame_udm_deleted(self, func_ea: "ea_t", udm_tid: "tid_t", udm: "udm_t") -> "void"`
* `frame_udm_renamed(self, func_ea: "ea_t", udm: "udm_t", oldname: "char const *") -> "void"`
* `frame_udm_changed(self, func_ea: "ea_t", udm_tid: "tid_t", udmold: "udm_t", udmnew: "udm_t") -> "void"`
* `frame_expanded(self, func_ea: "ea_t", udm_tid: "tid_t", delta: "adiff_t") -> "void"`

### Removed functions

All the _processor_t functions have been removed from ida_idp.

## ida_ieee

### Removed methods

#### fpvalue_t

* `_get_10bytes`
* `_set_10bytes`

## ida_kernwin

### Removed functions

* `place_t_as_enumplace_t`
* `place_t_as_structplace_t`
* `open_enums_window`
* `open_structs_window`
* `choose_struc`
* `choose_enum(title, default_id) -> "enum_t"`
* `choose_enum_by_value(title, default_id, value, nbytes) -> "enum_t"`

### Modified function

* `place_t_as_idaplace_t` has been made an alias of `place_t.as_idaplace_t`
* `place_t_as_simpleline_place_t` has been made an alias of `place_t.as_simpleline_place_t`
* `place_t_as_tiplace_t` has been made an alias of `place_t.as_tiplace_t`

### Removed classes

* `enumplace_t`
* `structplace_t`

### Removed methods

#### place_t

* `as_enumplace_t`
* `as_structplace_t`

#### twinpos_t

* `place_as_enumplace_t`
* `place_as_structplace_t`

#### tagged_line_sections_t

* `find_in`

### Added methods

#### tagged_line_sections_t

* `nearest_before(self, range: "tagged_line_section_t", start: "cpidx_t", tag: "color_t"=0) -> "tagged_line_section_t const *"`
* `nearest_after(self, range: "tagged_line_section_t", start: "cpidx_t", tag: "color_t"=0) -> "tagged_line_section_t const *"`

#### chooser_base_t

* `has_widget_lifecycle(self) -> "bool"`

### Added functions

* `is_ida_library(path: "char *", pathsize: "size_t", handle: "void **") -> "bool"`

## ida_lines

### Removed functions

* `set_user_defined_prefix`

## ida_moved

### Modified functions

* `bookmarks_t_mark` has been made an alias of `bookmarks_t.mark`
* `bookmarks_t_get_desc` has been made an alias of `bookmarks_t.get_desc`
* `bookmarks_t_find_index` has been made an alias of `bookmarks_t.find_index`
* `bookmarks_t_size` has been made an alias of `bookmarks_t.size`
* `bookmarks_t_erase` has been made an alias of `bookmarks_t.erase`
* `bookmarks_t_get_dirtree_id` has been made an alias of `bookmarks_t.get_dirtree_id`
* `bookmarks_t_get` has been made an alias of `bookmarks_t.get`

## ida_nalt

### Removed functions

* `validate_idb_names`

## ida_netnode

### Modified functions

* `netnode.exist has been made an alias of netnode.exist`

## ida_pro

### Removed functions

* `uchar_array_frompointer`
* `tid_array_frompointer`
* `ea_array_frompointer`
* `sel_array_frompointer`
* `int_pointer_frompointer`
* `sel_pointer_frompointer`
* `ea_pointer_frompointer`

See [Added classes](#added-classes-2) below

### Added classes

#### plugin_options_t

* `erase(self, name: "char const *") -> "bool"`

#### uchar_pointer

* `assign(self, value: "uchar") -> "void"`
* `value(self) -> "uchar"`
* `cast(self) -> "uchar *"`
* `frompointer(t: "uchar *") -> "uchar_pointer *"`

#### ushort_pointer

* `assign(self, value: "ushort") -> "void"`
* `value(self) -> "ushort"`
* `cast(self) -> "ushort *"`
* `frompointer(t: "ushort *") -> "ushort_pointer *"`

#### uint_pointer

* `assign(self, value: "uint") -> "void"`
* `value(self) -> "uint"`
* `cast(self) -> "uint *"`
* `frompointer(t: "uint *") -> "uint_pointer *"`

#### sint8_pointer

* `assign(self, value: "sint8") -> "void"`
* `value(self) -> "sint8"`
* `cast(self) -> "sint8 *"`
* `frompointer(t: "sint8 *") -> "sint8_pointer *"`

#### int8_pointer

* `assign(self, value: "int8") -> "void"`
* `value(self) -> "int8"`
* `cast(self) -> "int8 *"`
* `frompointer(t: "int8 *") -> "int8_pointer *"`

#### uint8_pointer

* `assign(self, value: "uint8") -> "void"`
* `value(self) -> "uint8"`
* `cast(self) -> "uint8 *"`
* `frompointer(t: "uint8 *") -> "uint8_pointer *"`

#### int16_pointer

* `assign(self, value: "int16") -> "void"`
* `value(self) -> "int16"`
* `cast(self) -> "int16 *"`
* `frompointer(t: "int16 *") -> "int16_pointer *"`

#### uint16_pointer

* `assign(self, value: "uint16") -> "void"`
* `value(self) -> "uint16"`
* `cast(self) -> "uint16 *"`
* `frompointer(t: "uint16 *") -> "uint16_pointer *"`

#### int32_pointer

* `assign(self, value: "int32") -> "void"`
* `value(self) -> "int32"`
* `cast(self) -> "int32 *"`
* `frompointer(t: "int32 *") -> "int32_pointer *"`

#### uint32_pointer

* `assign(self, value: "uint32") -> "void"`
* `value(self) -> "uint32"`
* `cast(self) -> "uint32 *"`
* `frompointer(t: "uint32 *") -> "uint32_pointer *"`

#### int64_pointer

* `assign(self, value: "int64") -> "void"`
* `value(self) -> "int64"`
* `cast(self) -> "int64 *"`
* `frompointer(t: "int64 *") -> "int64_pointer *"`

#### uint64_pointer

* `assign(self, value: "uint64") -> "void"`
* `value(self) -> "uint64"`
* `cast(self) -> "uint64 *"`
* `frompointer(t: "uint64 *") -> "uint64_pointer *"`

#### ssize_pointer

* `assign(self, value: "ssize_t") -> "void"`
* `value(self) -> "ssize_t"`
* `cast(self) -> "ssize_t *"`
* `frompointer(t: "ssize_t *") -> "ssize_pointer *"`

#### bool_pointer

* `assign(self, value: "bool") -> "void"`
* `value(self) -> "bool"`
* `cast(self) -> "bool *"`
* `frompointer(t: "bool *") -> "bool_pointer *"`

#### short_pointer

* `assign(self, value: "short") -> "void"`
* `value(self) -> "short"`
* `cast(self) -> "short *"`
* `frompointer(t: "short *") -> "short_pointer *"`

#### char_pointer

* `assign(self, value: "char") -> "void"`
* `value(self) -> "char"`
* `cast(self) -> "char *"`
* `frompointer(t: "char *") -> "char_pointer *"`

#### sel_pointer

* `assign(self, value: "sel_t") -> "void"`
* `value(self) -> "sel_t"`
* `cast(self) -> "sel_t *"`
* `frompointer(t: "sel_t *") -> "sel_pointer *"`

#### asize_pointer

* `assign(self, value: "asize_t") -> "void"`
* `value(self) -> "asize_t"`
* `cast(self) -> "asize_t *"`
* `frompointer(t: "asize_t *") -> "asize_pointer *"`

#### adiff_pointer

* `assign(self, value: "adiff_t") -> "void"`
* `value(self) -> "adiff_t"`
* `cast(self) -> "adiff_t *"`
* `from_pointer(t: "adiff_t*") -> "adiff_pointer *"`

#### uval_pointer

* `assign(self, value: "uval_t") -> "void"`
* `value(self) -> "uval_t"`
* `cast(self) -> "uval_t *"`
* `frompointer(t: "uval_t *") -> "uval_pointer *"`

#### ea32_pointer

* `assign(self, value: "ea32_t") -> "void"`
* `value(self) -> "ea32_t"`
* `cast(self) -> "ea32_t *"`
* `frompointer(t: "ea32_t *") -> "ea32_pointer *"`

#### ea64_pointer

* `assign(self, value: "ea64_t") -> "void"`
* `value(self) -> "ea64_t"`
* `cast(self) -> "ea64_t *"`
* `frompointer(t: "ea64_t *") -> "ea64_pointer *"`

#### flags_pointer

* `assign(self, value: "flags_t") -> "void"`
* `value(self) -> "flags_t"`
* `cast(self) -> "flags_t *"`
* `frompointer(t: "flags_t *") -> "flags_pointer *"`

#### flags64_pointer

* `assign(self, value: "flags64_t") -> "void"`
* `value(self) -> "flags64_t"`
* `cast(self) -> "flags64_t *"`
* `frompointer(t: "flags64_t *") -> "flags64_pointer *"`

#### tid_pointer

* `assign(self, value: "tid_t") -> "void"`
* `value(self) -> "tid_t"`
* `cast(self) -> "tid_t *"`
* `frompointer(t: "tid_t *") -> "tid_pointer *"`

### Added functions

* `get_login_name() -> "qstring *"`

## ida_regfinder

### Removed functions

* `reg_value_info_t_make_dead_end`
* `reg_value_info_t_make_aborted`
* `reg_value_info_t_make_badinsn`
* `reg_value_info_t_make_unkinsn`
* `reg_value_info_t_make_unkfunc`
* `reg_value_info_t_make_unkloop`
* `reg_value_info_t_make_unkmult`
* `reg_value_info_t_make_num`
* `reg_value_info_t_make_initial_sp`

### Modified functions

| 8.4                                                | 9.0                                                                              |
| -------------------------------------------------- | -------------------------------------------------------------------------------- |
| `invalidate_regfinder_cache(ea: "ea_t") -> "void"` | `invalidate_regfinder_cache(from=BADADDR: "ea_t", to=BADADDR: "ea_t") -> "void"` |

### Added methods

#### reg_value_info_t

* `movt(self, r: "reg_value_info_t", insn: "insn_t const &") -> "void"`

## ida_registry

### Removed functions

* `reg_load`
* `reg_flush`

## ida_search

### Removed functions

* `find_binary`

## ida_ua

### Removed Function

* `construct_macro(insn: "insn_t *", enable: "bool", build_macro: "PyObject *") -> bool (See [Modified functions](#modified-functions-4))`

### Modified functions

| 8.4                                                                                            | 9.0                                                                                           |
| ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `construct_macro2(_this: "macro_constructor_t *", insn: "insn_t *", enable: "bool") -> "bool"` | `construct_macro(_this: "macro_constructor_t *", insn: "insn_t *", enable: "bool") -> "bool"` |

### Added methods

#### macro_constructor_t

* `construct_macro(self, insn: "insn_t", enable: "bool") -> "bool"`

## idautils

### Modified functions

| 8.4                                            | 9.0                                                              |
| ---------------------------------------------- | ---------------------------------------------------------------- |
| `Structs() -> [(idx, sid, name)]`              | `Structs() -> [(ordinal, sid, name)]`                            |
| `StructMembers(sid) -> [(offset, name, size)]` | `StructMembers(sid) -> [(offset_in_bytes, name, size_in_bytes)]` |

## IDB events

The following table provide a list of IDB events that have been replaced or, in some cases, removed.

| Since 7                 | In 9.0                                                   |
| ----------------------- | -------------------------------------------------------- |
| `truc_created`          | `local_types_changed`                                    |
| `deleting_struc`        | **none**                                                 |
| `struc_deleted`         | `local_types_changed`                                    |
| `changing_struc_align`  | **none**                                                 |
| `struc_align_changed`   | `local_types_changed`                                    |
| `renaming_struc`        | **none**                                                 |
| `struc_renamed`         | `local_types_changed`                                    |
| `expanding_struc`       | **none**                                                 |
| `struc_expanded`        | `lt_udt_expanded, frame_expanded, local_types_changed`   |
| `struc_member_created`  | `lt_udm_created, frame_udm_created, local_types_changed` |
| `deleting_struc_member` | **none**                                                 |
| `struc_member_deleted`  | `lt_udm_deleted, frame_udm_deleted, local_types_changed` |
| `renaming_struc_member` | **none**                                                 |
| `struc_member_renamed`  | `lt_udm_renamed, frame_udm_renamed, local_types_changed` |
| `changing_struc_member` | **none**                                                 |
| `struc_member_changed`  | `lt_udm_changed, frame_udm_changed, local_types_changed` |
| `changing_struc_cmt`    | **none**                                                 |
| `struc_cmt_changed`     | `local_types_changed`                                    |
| `enum_created`          | `local_types_changed`                                    |
| `deleting_enum`         | **none**                                                 |
| `enum_deleted`          | `local_types_changed`                                    |
| `renaming_enum`         | **none**                                                 |
| `enum_renamed`          | `local_types_changed`                                    |
| `changing_enum_bf`      | `local_types_changed`                                    |
| `enum_bf_changed`       | `local_types_changed`                                    |
| `changing_enum_cmt`     | **none**                                                 |
| `enum_cmt_changed`      | `local_types_changed`                                    |
| `enum_member_created`   | `local_types_changed`                                    |
| `deleting_enum_member`  | **none**                                                 |
| `enum_member_deleted`   | `local_types_changed`                                    |
| `enum_width_changed`    | `local_types_changed`                                    |
| `enum_flag_changed`     | `local_types_changed`                                    |
| `enum_ordinal_changed`  | \`**none**                                               |

## Type information error codes

Following is the list of error values returned by the type info module. It can also be found in `typeinf.hpp` in the IDASDK:

| Error name              | Val. | Meaning                                                                                                                         |
| ----------------------- | ---- | ------------------------------------------------------------------------------------------------------------------------------- |
| **TERR_OK**            | 0    | ok                                                                                                                              |
| **TERR_STABLE**        | 1    | it means no errors occurred but nothing has changed (this code is internal: should never be returned to caller) -*             |
| **TERR_SAVE_ERROR**   | -1   | failed to save                                                                                                                  |
| **TERR_SERIALIZE**     | -2   | failed to serialize                                                                                                             |
| **TERR_BAD_NAME**     | -3   | name is not acceptable                                                                                                          |
| **TERR_BAD_ARG**      | -4   | bad argument                                                                                                                    |
| **TERR_BAD_TYPE**     | -5   | bad type                                                                                                                        |
| **TERR_BAD_SIZE**     | -6   | bad size                                                                                                                        |
| **TERR_BAD_INDEX**    | -7   | bad index                                                                                                                       |
| **TERR_BAD_ARRAY**    | -8   | arrays are forbidden as function arguments                                                                                      |
| **TERR_BAD_BF**       | -9   | bitfields are forbidden as function arguments                                                                                   |
| **TERR_BAD_OFFSET**   | -10  | bad member offset                                                                                                               |
| **TERR_BAD_UNIVAR**   | -11  | unions cannot have variable sized members                                                                                       |
| **TERR_BAD_VARLAST**  | -12  | variable sized member must be the last member in the structure                                                                  |
| **TERR_OVERLAP**       | -13  | the member overlaps with other members that cannot be deleted                                                                   |
| **TERR_BAD_SUBTYPE**  | -14  | recursive structure nesting is forbidden                                                                                        |
| **TERR_BAD_VALUE**    | -15  | value is not acceptable                                                                                                         |
| **TERR_NO_BMASK**     | -16  | bitmask is not found                                                                                                            |
| **TERR_BAD_BMASK**    | -17  | Bad enum member mask. The specified mask should not intersect with any existing mask in the enum. Zero masks are prohibited too |
| **TERR_BAD_MSKVAL**   | -18  | bad bmask and value combination                                                                                                 |
| **TERR_BAD_REPR**     | -19  | bad or incompatible field representation                                                                                        |
| **TERR_GRP_NOEMPTY**  | -20  | could not delete group mask for not empty group                                                                                 |
| **TERR_DUPNAME**       | -21  | duplicate name                                                                                                                  |
| **TERR_UNION_BF**     | -22  | unions cannot have bitfields                                                                                                    |
| **TERR_BAD_TAH**      | -23  | bad bits in the type attributes (TAH bits)                                                                                      |
| **TERR_BAD_BASE**     | -24  | bad base class                                                                                                                  |
| **TERR_BAD_GAP**      | -25  | bad gap                                                                                                                         |
| **TERR_NESTED**        | -26  | recursive structure nesting is forbidden                                                                                        |
| **TERR_NOT_COMPAT**   | -27  | the new type is not compatible with the old type                                                                                |
| **TERR_BAD_LAYOUT**   | -28  | failed to calculate the structure/union layout                                                                                  |
| **TERR_BAD_GROUPS**   | -29  | bad group sizes for bitmask enum                                                                                                |
| **TERR_BAD_SERIAL**   | -30  | enum value has too many serials                                                                                                 |
| **TERR_ALIEN_NAME**   | -31  | enum member name is used in another enum                                                                                        |
| **TERR_STOCK**         | -32  | stock type info cannot be modified                                                                                              |
| **TERR_ENUM_SIZE**    | -33  | bad enum size                                                                                                                   |
| **TERR_NOT_IMPL**     | -34  | not implemented                                                                                                                 |
| **TERR_TYPE_WORSE**   | -35  | the new type is worse than the old type                                                                                         |
| **TERR_BAD_FX_SIZE** | -36  | cannot extend struct beyond fixed size                                                                                          |

## Alternative examples

This section gives examples of how to port some ida_struct and ida_enum functions using ida_typeinf.

### del_struct_members

The following code can be used as an example of how to replace ida_struct.del_struct_members.

```python
def del_struct_members(sid, offset1, offset2):
    tif = ida_typeinf.tinfo_t()
    if tif.get_type_by_tid(sid) and tif.is_udt():
        udm = ida_typeinf.udm_t()
        udm.offset = offset1 * 8
        idx1 = tif.find_udm(udm, ida_typeinf.STRMEM_OFFSET)
        udm = ida_typeinf.udm_t()
        udm.offset = offset2 * 8
        idx2 = tif.find_udm(udm, ida_typeinf.STRMEM_OFFSET)
        return tif.del_udms(idx1, idx2)
```

### get_member_by_fullname

The following code can be used as an example of how to replace ida_struct.get_member_by_fullname.

```python
def get_member_by_fullname(fullname):
    udm = ida_typeinf.udm_t()
    idx = ida_typeinf.get_udm_by_fullname(udm, fullname)
    if  idx == -1:
        return None
    else:
        return udm
```

### get_struc_qty

The following code can be used as an example of how to replace ida_struct.get_struc_qty.

```python
def get_struc_qty():
    count = 0
    limit = ida_typeinf.get_ordinal_limit()
    for i in range(1, limit):
        tif = ida_typeinf.tinfo_t()
        if not tif.get_numbered_type(i, ida_typeinf.BTF_STRUCT):
            continue
        else:
            count += 1
    return count
```

### is_special_member

The following code can be used as an example of how to replace ida_struct.is_special_member.

```python
def is_special_member(member_id):
    tif = ida_typeing.tinfo_t()
    udm = ida_typeinf.udm_t()
    if tif.get_udm_by_tid(udm, member_id) != -1:
        return udm.is_special_member()
    return False
```

### get_sptr

The following code can be used as an example of how to replace ida_struct.get_sptr.

```python
def get_sptr(udm):
    tif = udm.type
    if tif.is_udt() and tif.is_struct():
        return tif
    else:
        return None
```

## How to examples

### List structure members

#### Example 1

```python
def list_enum_members(name)
    tid = idc.get_struc_id(name)
    if not tid == ida_idaapi.BADADDR:
        for (offset, name, size) in idautils.StructMembers(tid):
            print(f'Member {name} at offset {offset} of size {size}')
```

#### Example 2

```python
def list_struct_members2(name):
    til = ida_typeinf.get_idati()
    tif = ida_typeinf.tinfo_t()
    if not tif.get_named_type(til, name, ida_typeinf.BTF_STRUCT, True, False):
        print(f"'{name}' is not a structure")
    elif  tif.is_typedef():
        print(f"'{name}' is not a (non typedefed) structure.")
    else:
        udt = ida_typeinf.udt_type_data_t()
        if tif.get_udt_details(udt):
            idx = 0
            print(f'Listing the {name} structure {udt.size()} field names:')
            for udm in udt:
                print(f'Field {idx}: {udm.name}')
                idx += 1
        else:
            print(f"Unable to get udt details for structure '{name}'")
```

### List enum members

```python
def list_enum_members(name):
    til = ida_typeinf.get_idati()
    tif = ida_typeinf.tinfo_t()
    if not tif.get_named_type(til, name, ida_typeinf.BTF_ENUM, True, False):
        print(f"'{name}' is not an enum")
    elif tif.is_typedef():
        print(f"'{name}' is not a (non typedefed) enum.")
    else:
        edt = ida_typeinf.enum_type_data_t()
        if tif.get_enum_details(edt):
            idx = 0
            bitfield = ''
            if edt.is_bf():
                bitfield = '(bitfield)'
            print(f"Listing the '{name}' {bitfield} enum {edt.size()} field names:")
            for edm in edt:
                print(f'Field {idx}: {edm.name} = {edm.value}')
                idx += 1
        else:
            print(f"Unable to get udt details for enum '{name}'")
```

### List frame information

```python
func = ida_funcs.get_func(here())
if func:
    func_name = ida_funcs.get_func_name(func.start_ea)
    frame_tif = ida_typeinf.tinfo_t()
    if ida_frame.get_func_frame(frame_tif, func):
        frame_udt = ida_typeinf.udt_type_data_t()
        if frame_tif.get_udt_details(frame_udt):
            print('List frame information:')
            print('-----------------------')
            print(f'{func_name} @ {func.start_ea:x} framesize {frame_tif.get_size():x}')
            print(f'Local variable size: {func.frsize:x}')
            print(f'Saved registers: {func.frregs:x}')
            print(f'Argument size: {func.argsize:x}')
            print('{')
            idx = 0
            for udm in frame_udt:
                print(f'\t[{idx}] {udm.name}: soff={udm.offset//8:x} eof={udm.end()//8:x} {udm.type.dstr()}')
                idx += 1
            print('}')
else:
    print(f'{here():x} is not inside a function.')
```

### List stack variables xrefs

```python
func = ida_funcs.get_func(here())
if func:
    print(f'Function @ {func.start_ea:x}')

    frame_tif = ida_typeinf.tinfo_t()
    if ida_frame.get_func_frame(frame_tif, func):
        print('Frame found')
        nmembers = frame_tif.get_udt_nmembers()
        print(f'Frame has {nmembers} members')

        if nmembers > 0:
            frame_udt = ida_typeinf.udt_type_data_t()
            if frame_tif.get_udt_details(frame_udt):

                for frame_udm in frame_udt:
                    start_off = frame_udm.begin() // 8
                    end_off = frame_udm.end() // 8
                    xreflist = ida_frame.xreflist_t()
                    ida_frame.build_stkvar_xrefs(xreflist, func, start_off, end_off)
                    size = xreflist.size()
                    print(f'{frame_udm.name} stack variable starts @ {start_off:x}, ends @ {end_off:x}, xref size: {size}')

                    for idx in range(size):
                        match xreflist[idx].type:
                            case ida_xref.dr_R:
                                type = 'READ'
                            case ida_xref.dr_W:
                                type = 'WRITE'
                            case _:
                                type = 'UNK'
                        print(f'\t[{idx}]: xref @ {xreflist[idx].ea:x} of type {type}')
            else:
                print('Unable to get the frame details.')
        else:
            print('No members found.')
else:
    print('No function under the cursor')
```

### Create a structure with parsing

```python
struct_str = """struct pcap_hdr_s {
        uint32_t magic_number;   /* magic number */
        uint16_t version_major;  /* major version number */
        uint16_t version_minor;  /* minor version number */
        int32_t  thiszone;       /* GMT to local correction */
        uint32_t sigfigs;        /* accuracy of timestamps */
        uint32_t snaplen;        /* max length of captured packets, in octets */
        uint32_t network;        /* data link type */
};"""
tif = ida_typeinf.tinfo_t()
if tif.get_named_type(None, 'pcap_hdr_s'):
    ida_typeinf.del_named_type(None, 'pcap_hdr_s', ida_typeinf.NTF_TYPE)
ida_typeinf.idc_parse_types(struct_str, 0)
if not tif.get_named_type(None, 'pcap_hdr_s'):
    print('Unable to retrieve pcap_hdr_s structure')
```

### Create a structure member by member

```python
tif = ida_typeinf.tinfo_t()
if tif.get_named_type(None, 'pcaprec_hdr_s'):
    ida_typeinf.del_named_type(None, 'pcaprec_hdr_s', ida_typeinf.NTF_TYPE)
field_list = [('ts_sec', ida_typeinf.BTF_UINT32),
             ('ts_usec', ida_typeinf.BTF_UINT32),
             ('incl_len', ida_typeinf.BTF_UINT32),
             ('orig_len', ida_typeinf.BTF_UINT32)]
udt = ida_typeinf.udt_type_data_t()
udm = ida_typeinf.udm_t()
for (name, type) in field_list:
    udm.name = name
    udm.type = ida_typeinf.tinfo_t(type)
    udt.push_back(udm)
if tif.create_udt(udt):
    tif.set_named_type(None, 'pcaprec_hdr_s')

```

### Create a union member by member

```python
tif = ida_typeinf.tinfo_t()
if tif.get_named_type(None, 'my_union'):
    ida_typeinf.del_named_type(None, 'my_union', ida_typeinf.NTF_TYPE)
tif = ida_typeinf.tinfo_t()
udt = ida_typeinf.udt_type_data_t()
field_list = [('member1', ida_typeinf.BTF_INT32),
              ('member2', ida_typeinf.BTF_CHAR),
              ('member3', ida_typeinf.BTF_FLOAT)]
udt.is_union = True
udm = ida_typeinf.udm_t()
for (name, type) in field_list:
    udm.name = name
    udm.type = ida_typeinf.tinfo_t(type)
    udt.push_back(udm)
tif.get_named_type(None, 'pcap_hdr_s')
if tif.create_ptr(tif):
    udm.name = 'header_ptr'
    udm.type = tif
    udt.push_back(udm)
    tif.clear()
    tif.create_udt(udt, ida_typeinf.BTF_UNION)
    tif.set_named_type(None, 'my_union')
```

### Create a bitmask enum

```python
edt = ida_typeinf.enum_type_data_t()
edm = ida_typeinf.edm_t()
for name, value in [('field1', 1), ('field2', 2), ('field3', 4)]:
    edm.name = name
    edm.value = value
    edt.push_back(edm)

tif = ida_typeinf.tinfo_t()
if tif.create_enum(edt):
    tif.set_enum_is_bitmask(ida_typeinf.tinfo_t.ENUMBM_ON)
    tif.set_named_type(None, 'bmenum')
```

### Create an array

#### Example 1

```python
tif = ida_typeinf.tinfo_t(ida_typeinf.BTF_INT)
if tif.create_array(tif, 5, 0):
    type = tif._print()
    tif.set_named_type(None, 'my_int_array1')
```

#### Example 2

```python
atd = ida_typeinf.array_type_data_t()
atd.base = 0
atd.nelems = 5
atd.elem_type = ida_typeinf.tinfo_t(ida_typeinf.BTF_INT)
tif = ida_typeinf.tinfo_t()
if tif.create_array(atd):
    type = tif._print()
    tif.set_named_type(None, 'my_int_array2')
```

### Log local type events

```python
class lt_logger_hooks_t(ida_idp.IDB_Hooks):
    def __init__(self):
        ida_idp.IDB_Hooks.__init__(self)
        self.inhibit_log = 0

    def _log(self, msg=''):
        if self.inhibit_log <= 0:
            print(f'>>> lt_logger_hooks_t: {msg}' if msg else '>>> lt_logger_hooks_t event')
        return 0

    def lt_udm_created(self, udtname, udm):
        msg = f'UDM {udm.name} has been created in UDT {udtname}'
        return self._log(msg)

    def lt_udm_deleted(self, udtname, udm_tid):
        msg = f'UDM tid {udm_tid:x} has been deleted from {udtname}'
        return self._log(msg)

    def lt_udm_renamed(self, udtname, udm, oldname):
        msg = f'UDM {oldname} from UDT {udtname} has been renamed to {udm.name}'
        return self._log(msg)

    def lt_udm_changed(self, udtname, udm_tid, udmold, udmnew):
        return self._log()

# Remove an existing hook on second run
try:
    idp_hook_stat = "un"
    print("Local type IDB hook: checking for hook...")
    lthook
    print("Local type IDB hook: unhooking....")
    idp_hook_stat2 = ""
    lthook.unhook()
    del lthook
except:
    print("local type IDB hook: not installed, installing now....")
    idp_hook_stat = ""
    idp_hook_stat2 = "un"
    lthook = lt_logger_hooks_t()
    lthook.hook()

print(f'Local type IDB hook {idp_hook_stat}installed. Run the script again to {idp_hook_stat2}install')
```

### Log frame events

```python
class frame_logger_hooks_t(ida_idp.IDB_Hooks):
    def __init__(self):
        ida_idp.IDB_Hooks.__init__(self)
        self.inhibit_log = 0

    def _log(self, msg=''):
        if self.inhibit_log <= 0:
            print(f'>>> frame_logger_hooks_t: {msg}' if msg else '>>> frame_logger_hooks_t event')
        return 0

    def frame_udm_created(self, func_ea, udm):
        return self._log(f'UDM {udm.name} created in frame at {func_ea:x}')

    def frame_udm_deleted(self, func_ea, udm_tid, udm):
        return self._log(f'UDM tid {udm_tid:x} deleted from frame at {func_ea:x}')

    def frame_udm_renamed(self, func_ea, udm, oldname):
        return self._log(f'UDM {oldname} renamed to {udm.name} in frame at {func_ea:x}')

    def frame_udm_changed(self, func_ea, udm_tid, udmold, udmnew):
        return self._log(f'UDM changed in frame at {func_ea:x}')

# Remove an existing hook on second run
try:
    frame_idp_hook_stat = "un"
    print("Frame IDP hook: checking for hook...")
    framehook
    print("Frame IDP hook: unhooking....")
    frame_idp_hook_stat2 = ""
    framehook.unhook()
    del framehook
except:
    print("Frame IDP hook: not installed, installing now....")
    frame_idp_hook_stat = ""
    frame_idp_hook_stat2 = "un"
    framehook = frame_logger_hooks_t()
    framehook.hook()

print(f'Frame IDB hook {frame_idp_hook_stat}installed. Run the script again to {frame_idp_hook_stat2}install')
```
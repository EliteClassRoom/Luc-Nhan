"""Host/runtime detection and context utilities.

This module centralizes runtime integration points so Luc Nhan can run inside
IDA Pro or standalone Python.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

HOST_IDA = "ida"
HOST_STANDALONE = "standalone"

_HOST = HOST_STANDALONE
_idc = None
_idaapi = None
_ida_kernwin = None
_is_headless: bool = False
try:
    _idaapi = importlib.import_module("idaapi")
    _HOST = HOST_IDA
    # Cache frequently-used IDA modules to avoid repeated importlib lookups.
    # Both are optional — headless/batch IDA may not expose them.
    try:
        _idc = importlib.import_module("idc")
    except ImportError:
        _idc = None  # optional — absent in some IDA headless configurations
    try:
        _ida_kernwin = importlib.import_module("ida_kernwin")
    except ImportError:
        _ida_kernwin = None  # optional — absent in some IDA headless configurations

    # --- Headless detection ---
    # LUCNHAN_HEADLESS=1 is a user-visible override for headless mode.
    if os.environ.get("LUCNHAN_HEADLESS", "") in ("1", "yes", "true"):
        _is_headless = True
    elif _ida_kernwin is not None:
        # If ida_kernwin is present, check IDA batch flags.  A partially
        # initialized IDA runtime can raise more than ``AttributeError``
        # here (e.g. ``RuntimeError`` from a half-built ``ida_kernwin``),
        # so we treat every exception as "headless/batch = unknown ->
        # default False" instead of letting it bubble up at import time.
        try:
            batch = getattr(_ida_kernwin.cvar, "batch", False)
        except Exception:
            batch = False
        _is_headless = bool(batch)
    else:
        # No ida_kernwin at all → definitely headless/batch
        _is_headless = True
except ImportError:
    _HOST = HOST_STANDALONE


def host_kind() -> str:
    """Return the active runtime host: ida or standalone."""
    return _HOST


def is_ida() -> bool:
    return _HOST == HOST_IDA


def has_ida_kernwin() -> bool:
    """Return True when the ida_kernwin module is importable.

    In batch/headless IDA sessions ida_kernwin may be absent or
    degraded — callers should use this instead of a bare ``import``.
    """
    return _ida_kernwin is not None


def has_ida_ui() -> bool:
    """Return True when IDA has a usable interactive UI.

    False when ida_kernwin is absent or IDA is running in batch mode.
    """
    return is_ida() and not is_ida_headless() and has_ida_kernwin()


def is_ida_headless() -> bool:
    """Return True when IDA is running headless (no UI).

    True when:
      - ``LUCNHAN_HEADLESS=1`` is set.
      - ``ida_kernwin.cvar.batch`` is true (IDA batch mode).
      - ``ida_kernwin`` is absent entirely.

    Returns False in standalone Python.
    """
    if not is_ida():
        return False
    return _is_headless


# Convenience module-level flag — importers that just need a bool
# can use ``from lucnhan.core.host import IDA_AVAILABLE`` instead of
# calling ``is_ida()`` repeatedly.
IDA_AVAILABLE: bool = is_ida()

# Whether the Hex-Rays decompiler SDK is importable.
if IDA_AVAILABLE:
    try:
        importlib.import_module("ida_hexrays")
        HAS_HEXRAYS: bool = True
    except ImportError:
        HAS_HEXRAYS = False
else:
    HAS_HEXRAYS = False


def host_display_name() -> str:
    if _HOST == HOST_IDA:
        return "IDA Pro"
    return "Standalone Python"


def get_current_address() -> int | None:
    """Return current cursor/address from host context if available.

    Returns None in headless mode (no active UI cursor).
    """
    if is_ida_headless():
        return None
    if is_ida():
        try:
            return int(_idc.get_screen_ea()) if _idc else None
        except Exception:
            return None

    return None


def resolve_symbol(name: str) -> int | None:
    """Return the address of a symbol name, or None when it is unknown.

    Resolution is lazy so host-agnostic callers (e.g. ``parse_addr``) can
    accept a symbol name for an address parameter without importing any
    ``ida_*`` module outside an IDA host.
    """
    if is_ida():
        try:
            ida_name = importlib.import_module("ida_name")
            idc = importlib.import_module("idc")
            bad = 0xFFFFFFFFFFFFFFFF
            try:
                bad = int(idc.BADADDR)
            except (AttributeError, TypeError, ValueError):
                pass
            ea = ida_name.get_name_ea(bad, name)
        except Exception:
            return None
        if ea is not None and int(ea) != bad:
            return int(ea)
        return None

    return None


def navigate_to(address: int) -> bool:
    """Navigate UI to an address when the host supports it.

    Returns False in headless mode (no UI navigation possible).
    """
    if is_ida_headless():
        return False
    ea = int(address)

    if is_ida():
        try:
            return bool(_ida_kernwin.jumpto(ea)) if _ida_kernwin else False
        except Exception:
            return False

    return False


def get_user_config_base_dir() -> str:
    """Return host-specific user base directory for Luc Nhan config/log files."""
    if is_ida():
        try:
            return _idaapi.get_user_idadir() if _idaapi else os.path.join(str(Path.home()), ".idapro")
        except Exception:
            return os.path.join(str(Path.home()), ".idapro")

    return os.path.join(str(Path.home()), ".idapro")


def get_database_path() -> str:
    """Return the loaded database/binary path for the active host."""
    if is_ida():
        try:
            if _idaapi is None:
                return ""
            idb = _idaapi.get_path(_idaapi.PATH_TYPE_IDB)
            if idb:
                return idb
            return _idaapi.get_input_file_path() or ""
        except Exception:
            return ""

    return ""


def get_database_instance_id() -> str:
    """Read the Luc Nhan instance UUID stored in the current IDB.

    Returns '' if none is stored yet.
    """
    if is_ida():
        try:
            idaapi = _idaapi
            if idaapi is None:
                return ""
            node = idaapi.netnode("$ lucnhan", 0, False)
            if node == idaapi.BADNODE:
                return ""
            val = node.supstr(0)
            return val if isinstance(val, str) and val else ""
        except Exception:
            return ""

    return ""


def set_database_instance_id(instance_id: str) -> bool:
    """Store a Luc Nhan instance UUID in the current IDB.

    Returns True on success.
    """
    if is_ida():
        try:
            idaapi = _idaapi
            if idaapi is None:
                return False
            node = idaapi.netnode("$ lucnhan", 0, True)
            node.supset(0, instance_id)
            return True
        except Exception as e:
            sys.stderr.write(f"[Lục nhãn] set_database_instance_id IDA failed: {e}\n")
            return False

    return False

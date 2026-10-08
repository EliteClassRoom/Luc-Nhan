"""Phân loại traceback của execute_python để quyết định có spawn reviewer không.

Pure function — không import IDA, không gọi LLM, không touch globals.
Operate trên traceback string + script source.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

#: Exception types được xem là "API-shaped" — rõ ràng là hallucinated/wrong API.
#: Các exception khác (ValueError, TypeError, KeyError...) là logic bug → main agent tự sửa.
_API_SHAPED_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "AttributeError",
        "ImportError",
        "ModuleNotFoundError",  # subclass của ImportError nhưng tường minh
        "NameError",
    }
)

#: Prefix của module IDA trong namespace execute_python. ``idautils``,
#: ``idc``, ``idaapi`` không có prefix ``ida_`` nhưng vẫn là module IDA.
_IDA_MODULE_NAMES: frozenset[str] = frozenset({"idautils", "idc", "idaapi"})


@dataclass(frozen=True)
class TracebackClassification:
    """Kết quả phân loại traceback.

    *is_api_shaped* True nếu exception type thuộc ``_API_SHAPED_EXCEPTIONS``.
    *modules_referenced* là tuple tên module IDA được import trong script,
    dùng cho reference injection sau khi reviewer chạy.
    """

    is_api_shaped: bool
    exception_type: str = ""
    exception_message: str = ""
    modules_referenced: tuple[str, ...] = ()


def _parse_exception_type(traceback_text: str) -> str:
    """Extract exception type từ dòng cuối của traceback.

    Traceback Python kết thúc bằng: ``AttributeError: module 'x' has no attribute 'y'``
    Trả về tên type (vd "AttributeError") hoặc "" nếu không parse được.
    """
    lines = [ln.strip() for ln in traceback_text.strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    last = lines[-1]
    if ":" in last:
        return last.split(":", 1)[0].strip()
    return last


def _parse_exception_message(traceback_text: str) -> str:
    """Extract message (phần sau dấu ':') từ dòng cuối traceback."""
    lines = [ln.strip() for ln in traceback_text.strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    last = lines[-1]
    if ":" in last:
        return last.split(":", 1)[1].strip()
    return ""


def _extract_modules_from_code(code: str) -> tuple[str, ...]:
    """Extract tên module IDA được import trong script.

    Dùng để quyết định ``lookup_idapython_doc(module=...)`` cho module nào
    khi inject reference. Trả về tuple theo thứ tự xuất hiện, dedup.
    """
    modules: list[str] = []
    seen: set[str] = set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _IDA_MODULE_NAMES or top.startswith("ida_"):
                    if top not in seen:
                        modules.append(top)
                        seen.add(top)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            if top in _IDA_MODULE_NAMES or top.startswith("ida_"):
                if top not in seen:
                    modules.append(top)
                    seen.add(top)
    return tuple(modules)


def classify_traceback(
    traceback_text: str,
    script_code: str = "",
) -> TracebackClassification:
    """Phân loại traceback + extract context cho reviewer.

    Args:
        traceback_text: Output stderr của execute_python (chứa traceback).
        script_code: Script body (để extract modules cho reference injection).

    Returns:
        TracebackClassification với is_api_shaped, exception_type, message, modules.
    """
    exc_type = _parse_exception_type(traceback_text)
    is_api_shaped = exc_type in _API_SHAPED_EXCEPTIONS
    exc_message = _parse_exception_message(traceback_text)
    modules = _extract_modules_from_code(script_code) if script_code else ()
    return TracebackClassification(
        is_api_shaped=is_api_shaped,
        exception_type=exc_type,
        exception_message=exc_message,
        modules_referenced=modules,
    )


__all__ = [
    "TracebackClassification",
    "classify_traceback",
]

"""Tool framework: @tool decorator, ToolDefinition, JSON schema generation."""

from __future__ import annotations

import functools
import inspect
import traceback
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, get_type_hints

from ..core.errors import ToolError, ToolValidationError
from ..core.logging import log_error as _log_error
from ..core.logging import log_trace


def parse_addr(value: Any) -> int:
    """Parse an address that may arrive as hex string or int from the LLM."""
    if isinstance(value, int):
        return value
    return int(value, 0)


# Python type -> JSON Schema type
_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass
class ParameterSchema:
    name: str
    type: str
    description: str = ""
    required: bool = True
    default: Any = None
    enum: list[Any] | None = None
    items: dict[str, Any] | None = None


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: list[ParameterSchema] = field(default_factory=list)
    category: str = "general"
    requires_decompiler: bool = False
    mutating: bool = False
    timeout: float | None = None  # per-tool timeout in seconds (None = use default)
    handler: Callable | None = field(default=None, repr=False)
    requires: list[str] = field(default_factory=list)

    def to_json_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []

        for param in self.parameters:
            prop: dict[str, Any] = {"type": param.type}
            if param.description:
                prop["description"] = param.description
            if param.enum:
                prop["enum"] = param.enum
            if param.items:
                prop["items"] = param.items
            properties[param.name] = prop
            if param.required:
                required.append(param.name)

        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            schema["required"] = required
        return schema

    def to_provider_format(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.to_json_schema(),
            },
        }


def _extract_annotation_metadata(annotation: Any) -> dict[str, Any]:
    """Extract description, enum, etc. from typing.Annotated metadata."""
    metadata: dict[str, Any] = {}
    if hasattr(annotation, "__metadata__"):
        for m in annotation.__metadata__:
            if isinstance(m, str):
                metadata["description"] = m
            elif isinstance(m, dict):
                metadata.update(m)
    return metadata


def _resolve_type(annotation: Any) -> tuple:
    """Resolve a type annotation to (json_type, extra_schema_props, base_type)."""
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())

    # Handle Annotated — use typing.get_origin() because on Python 3.14
    # Annotated[int, ...].__origin__ is `int`, not `typing.Annotated`.
    if typing.get_origin(annotation) is typing.Annotated:
        real_args = typing.get_args(annotation)
        base = real_args[0] if real_args else str
        return _resolve_type(base)

    # Handle Optional
    if origin is typing.Union and len(args) == 2 and type(None) in args:
        inner = args[0] if args[1] is type(None) else args[1]  # type: ignore[misc]
        json_type, extra, _ = _resolve_type(inner)
        return json_type, extra, inner

    # Handle List
    if origin is list:
        items = {}
        if args:
            inner_type, _, _ = _resolve_type(args[0])
            items = {"type": inner_type}
        return "array", {"items": items} if items else {}, list

    # Handle Dict
    if origin is dict:
        return "object", {}, dict

    # Primitives
    json_type = _TYPE_MAP.get(annotation, "string")
    return json_type, {}, annotation


def _build_parameters(func: Callable) -> list[ParameterSchema]:
    """Build parameter schemas from function signature and type hints."""
    sig = inspect.signature(func)
    hints = get_type_hints(func, include_extras=True)
    params: list[ParameterSchema] = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue

        annotation = hints.get(name, str)

        # Extract Annotated metadata — use typing.get_origin() because
        # on Python 3.14 Annotated[X, ...].__origin__ is X, not Annotated.
        meta: dict[str, Any] = {}
        if typing.get_origin(annotation) is typing.Annotated:
            meta = _extract_annotation_metadata(annotation)
        annotation_for_type = annotation

        json_type, extra, _base_type = _resolve_type(annotation_for_type)

        # Determine required and default
        has_default = param.default is not inspect.Parameter.empty
        is_optional = getattr(annotation, "__origin__", None) is typing.Union and type(None) in getattr(
            annotation, "__args__", ()
        )

        ps = ParameterSchema(
            name=name,
            type=json_type,
            description=meta.get("description", ""),
            required=not has_default and not is_optional,
            default=param.default if has_default else None,
            enum=meta.get("enum"),
            items=extra.get("items"),
        )
        params.append(ps)

    return params


def tool(
    name: str | None = None,
    description: str | None = None,
    category: str = "general",
    requires_decompiler: bool = False,
    mutating: bool = False,
    timeout: float | None = None,
    requires: list[str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to register a function as an agent tool.

    Usage::

        @tool(name="decompile_function", category="decompiler", requires_decompiler=True)
        def decompile_function(address: Annotated[int, "Function address"]) -> str:
            '''Decompile the function at the given address.'''
            ...
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or func.__name__
        # Use the full docstring as description, not just the first line. A
        # single-line description like "List all imported functions" gives
        # the LLM no signal about output format, pagination, sibling tools,
        # or when NOT to use this tool. Multi-line docstrings are how we
        # give the model the context it needs to choose correctly.
        if description is not None:
            tool_desc = description
        else:
            doc = (func.__doc__ or "").strip()
            # Normalize internal whitespace: collapse runs of blank lines and
            # strip per-line indents so docstrings written with arbitrary
            # indentation render cleanly in the prompt.
            lines = [line.strip() for line in doc.splitlines()]
            paragraphs: list[str] = []
            current: list[str] = []
            for line in lines:
                if line:
                    current.append(line)
                elif current:
                    paragraphs.append(" ".join(current))
                    current = []
            if current:
                paragraphs.append(" ".join(current))
            tool_desc = "\n\n".join(paragraphs)
        params = _build_parameters(func)

        # Build requires list — merge explicit requires with requires_decompiler compat
        effective_requires = list(requires or [])
        if requires_decompiler and "hexrays" not in effective_requires:
            effective_requires.append("hexrays")

        # Capture the signature once so the wrapper can reject
        # hallucinated/extra arguments (e.g. the model passing ``count`` to
        # ``read_function_disassembly``, which only takes ``address``) with an
        # actionable message naming the valid parameters — instead of a bare
        # TypeError that the model retries unchanged in a loop.
        func_sig = inspect.signature(func)
        valid_params = [p for p in func_sig.parameters if p not in ("self", "cls")]
        accepts_str = ", ".join(valid_params) or "(no parameters)"

        defn = ToolDefinition(
            name=tool_name,
            description=tool_desc,
            parameters=params,
            category=category,
            requires_decompiler=requires_decompiler,
            mutating=mutating,
            timeout=timeout,
            handler=func,
            requires=effective_requires,
        )

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            log_trace(f"tool:{tool_name} CALL args={kwargs}")
            # Validate arguments against the signature before calling func.
            # Signature.bind() raises TypeError only for argument-count / name
            # mismatches — never for TypeErrors raised inside the body — so this
            # cleanly separates "model sent bad args" from "tool raised".
            try:
                func_sig.bind(*args, **kwargs)
            except TypeError as e:
                msg = f"Invalid arguments for {tool_name}: {e} This tool accepts: {accepts_str}."
                _log_error(f"tool:{tool_name} EXCEPTION: {msg}")
                raise ToolValidationError(msg, tool_name=tool_name) from e
            try:
                result = func(*args, **kwargs)
                log_trace(f"tool:{tool_name} OK result_len={len(str(result))}")
                return result
            except ToolError:
                raise
            except Exception as e:
                _log_error(f"tool:{tool_name} EXCEPTION: {e}\n{traceback.format_exc()}")
                raise ToolError(str(e), tool_name=tool_name) from e

        wrapper._tool_definition = defn  # type: ignore[attr-defined]
        defn.handler = wrapper
        return wrapper

    return decorator

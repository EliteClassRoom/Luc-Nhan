"""MCP JSON-RPC protocol helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# JSON-RPC 2.0 standard error codes
_JSONRPC_PARSE_ERROR = -32700

# Truncate parse-error previews so error messages stay bounded.
_PARSE_ERROR_PREVIEW_BYTES = 200


@dataclass
class MCPToolSchema:
    """Schema for a tool exposed by an MCP server."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


def encode_jsonrpc_request(method: str, params: dict[str, Any] | None = None, id: int = 1) -> bytes:
    """Encode a JSON-RPC 2.0 request with Content-Length framing."""
    body = {
        "jsonrpc": "2.0",
        "id": id,
        "method": method,
    }
    if params is not None:
        body["params"] = params

    payload = json.dumps(body)
    frame = f"Content-Length: {len(payload)}\r\n\r\n{payload}"
    return frame.encode("utf-8")


def decode_jsonrpc_response(data: str) -> dict[str, Any]:
    """Decode a JSON-RPC 2.0 response from a raw string."""
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return {
            "error": {
                "code": _JSONRPC_PARSE_ERROR,
                "message": f"Parse error: {data[:_PARSE_ERROR_PREVIEW_BYTES]}",
            }
        }


def parse_content_length_frame(stream) -> str | None:
    """Read one Content-Length framed message from a binary stream.

    Also handles plain newline-delimited JSON as fallback.
    Returns the body string, or None on EOF.
    """
    header_line = stream.readline()
    if not header_line:
        return None  # EOF

    header_str = header_line.decode("utf-8", errors="replace").strip()

    # Content-Length framing
    if header_str.lower().startswith("content-length:"):
        length_str = header_str.split(":", 1)[1].strip()
        try:
            content_length = int(length_str)
        except ValueError:
            return None

        # Consume the blank separator line
        sep = stream.readline()
        if not sep:
            return None

        content_data = stream.read(content_length)
        if len(content_data) < content_length:
            return None  # Truncated read (EOF mid-message)
        return content_data.decode("utf-8", errors="replace")

    # Newline-delimited JSON fallback
    if header_str.startswith("{"):
        return header_str

    return None

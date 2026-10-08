"""Request/response helpers for the headless control API."""

from __future__ import annotations

from typing import Any


def make_json_response(
    body: Any,
    status: int = 200,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    """Build a (status, headers, body) tuple compatible with most HTTP frameworks."""
    import json

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    return (status, headers, json.dumps(body, ensure_ascii=False))


def make_error_json(
    message: str,
    status: int = 400,
    detail: str | None = None,
) -> tuple[int, dict[str, str], str]:
    body: dict[str, Any] = {"error": message}
    if detail:
        body["detail"] = detail
    return make_json_response(body, status=status)

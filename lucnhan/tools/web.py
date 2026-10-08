"""Web search and image understanding tools using MiniMax's REST API.

These tools call MiniMax's coding_plan API endpoints directly. They only work
when the active LLM provider is MiniMax — no MCP dependency.

``requests`` is only imported when the tool actually makes an HTTP request,
not at module import time.

SSRF protection: ``understand_image`` accepts an image URL that the LLM
provides. To prevent the tool from being tricked into fetching
``http://169.254.169.254/latest/meta-data/`` (cloud metadata) or
internal-network URLs, URL downloads are validated through the same
``_is_safe_url`` / ``_resolve_hostsafe`` helpers used by ``web_fetch``:
HTTPS-only, reject private/loopback/link-local/multicast/reserved IPs at
both the hostname level (literal IP) and after DNS resolution. Any unsafe
URL is rejected with ``ToolError`` before the request is made.
"""

from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING, Annotated

from ..core.errors import ToolError
from ..core.logging import log_debug
from .base import tool

if TYPE_CHECKING:
    from ..core.config import LucNhanConfig

# Tool constants
WEB_SEARCH_TIMEOUT = 30.0
UNDERSTAND_IMAGE_TIMEOUT = 60.0
DEFAULT_API_HOST = "https://api.minimax.io"
_runtime_config: LucNhanConfig | None = None


def _safe_url_host(url: str) -> str:
    """Extract the hostname from *url* for SSRF DNS checks.

    Tiny local helper — re-uses urllib.parse so we don't import it twice.
    """
    import urllib.parse

    return (urllib.parse.urlparse(url).hostname or "").lower()


def set_runtime_config(config: LucNhanConfig | None) -> None:
    """Provide the active in-memory config for web tools.

    Web tools execute from worker threads and cannot receive the session config
    as a normal tool argument.  The session controller injects the active config
    here so MiniMax auth uses decrypted/current settings instead of reloading a
    potentially stale or encrypted copy from disk.
    """
    global _runtime_config
    _runtime_config = config


def _get_minimax_auth(
    config: LucNhanConfig | None = None,
) -> tuple[str, str, str] | tuple[None, None, None]:
    """Read MiniMax API key and host from the active runtime config.

    Returns (api_key, api_host, model_name) if MiniMax is the active
    provider and an API key is configured, or (None, None, None) otherwise.
    """
    if config is None:
        config = _runtime_config
    if config is None:
        from ..core.config import LucNhanConfig

        try:
            config = LucNhanConfig.load_or_create()
        except Exception as e:
            log_debug(f"Failed to load config for MiniMax auth: {e}")
            return None, None, None

    # Only available when MiniMax is the active provider
    if config.provider.name != "minimax":
        return None, None, None

    minimax_cfg = config.providers.get("minimax", {})
    api_key = minimax_cfg.get("api_key", "") or config.provider.api_key

    if not api_key:
        return None, None, None

    # Determine the REST API host.  Prefer the stored api_base
    # (both from the providers snapshot and the active provider),
    # strip any /anthropic suffix, and fall back to the global host.
    raw_base = minimax_cfg.get("api_base", "") or config.provider.api_base or DEFAULT_API_HOST
    api_host = raw_base.replace("/anthropic", "").rstrip("/")

    # Model name: prefer provider-level, then providers dict,
    # then the hard-coded default.
    model_name = config.provider.model or minimax_cfg.get("model") or "MiniMax-M3"

    return api_key, api_host, model_name


def _call_minimax_api(endpoint: str, payload: dict, timeout: float) -> dict:
    """Make an HTTP POST request to a MiniMax coding_plan endpoint.

    Args:
        endpoint: API path (e.g. "/v1/coding_plan/search")
        payload: JSON body
        timeout: Request timeout in seconds

    Returns:
        Parsed JSON response dict

    Raises:
        ToolError: If MiniMax is not the active provider, auth fails,
                   or the API returns an error.
    """
    import requests

    api_key, api_host, _model = _get_minimax_auth()

    if api_key is None:
        raise ToolError(
            "Web search and image analysis are only available when MiniMax is the "
            "active provider. Open Settings, select MiniMax as your provider, and "
            "enter your MiniMax API key from https://platform.minimax.io.",
            tool_name="web_search",
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "MM-API-Source": "LucNhan",
    }

    url = f"{api_host}{endpoint}"

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        response.raise_for_status()
    except requests.Timeout:
        raise ToolError(
            f"MiniMax API timed out after {timeout:.0f}s",
            tool_name="web_search",
        ) from None
    except requests.RequestException as e:
        raise ToolError(
            f"MiniMax API request failed: {e}",
            tool_name="web_search",
        ) from e

    try:
        data = response.json()
    except ValueError as e:
        raise ToolError(
            f"MiniMax API returned non-JSON response: {response.text[:200]}",
            tool_name="web_search",
        ) from e

    # Check for API-level errors
    base_resp = data.get("base_resp", {})
    status_code = base_resp.get("status_code", 0)
    if status_code != 0:
        status_msg = base_resp.get("status_msg", "unknown error")
        if status_code == 1004:
            raise ToolError(
                f"MiniMax authentication failed — invalid API key. "
                f"Check your key at Settings → MiniMax → API Key. ({status_msg})",
                tool_name="web_search",
            )
        raise ToolError(
            f"MiniMax API error ({status_code}): {status_msg}",
            tool_name="web_search",
        )

    return data


def _process_image_source(image_source: str) -> str:
    """Convert an image source to a base64 data URL for the MiniMax API.

    Handles:
      - HTTP/HTTPS URLs: downloads and converts to base64 data URL
      - Local file paths: reads and converts to base64 data URL
      - Already base64 data URLs (data:...): passed through unchanged

    Returns a string in "data:image/{format};base64,{data}" format.
    """
    # Strip @ prefix if present (Claude Desktop convention)
    if image_source.startswith("@"):
        image_source = image_source[1:]

    # Already a base64 data URL
    if image_source.startswith("data:"):
        return image_source

    # HTTP/HTTPS URL — download and convert (with SSRF guard)
    if image_source.startswith(("http://", "https://")):
        import requests

        # Reject URLs targeting private/internal networks before making
        # the request.  This blocks SSRF attempts where the LLM is
        # prompt-injected into fetching cloud metadata endpoints
        # (169.254.169.254) or internal services on 127.0.0.1 / 10.x.
        from .web_fetch import _is_safe_url, _resolve_hostsafe

        safe, err = _is_safe_url(image_source)
        if not safe:
            raise ToolError(
                f"Image URL rejected (SSRF guard): {err}",
                tool_name="understand_image",
            )
        host_safe, host_err = _resolve_hostsafe(_safe_url_host(image_source))
        if not host_safe:
            raise ToolError(
                f"Image URL rejected (SSRF guard): {host_err}",
                tool_name="understand_image",
            )

        try:
            img_response = requests.get(image_source, timeout=30)
            img_response.raise_for_status()
        except requests.RequestException as e:
            raise ToolError(
                f"Failed to download image from URL: {e}",
                tool_name="understand_image",
            ) from e

        image_data = img_response.content
        content_type = img_response.headers.get("content-type", "").lower()
        if "jpeg" in content_type or "jpg" in content_type:
            fmt = "jpeg"
        elif "png" in content_type:
            fmt = "png"
        elif "webp" in content_type:
            fmt = "webp"
        else:
            fmt = "jpeg"

        b64 = base64.b64encode(image_data).decode("utf-8")
        return f"data:image/{fmt};base64,{b64}"

    # Local file path
    if not os.path.exists(image_source):
        raise ToolError(
            f"Image file not found: {image_source}",
            tool_name="understand_image",
        )

    try:
        with open(image_source, "rb") as f:
            image_data = f.read()
    except OSError as e:
        raise ToolError(
            f"Failed to read image file: {e}",
            tool_name="understand_image",
        ) from e

    lower = image_source.lower()
    if lower.endswith(".png"):
        fmt = "png"
    elif lower.endswith(".webp"):
        fmt = "webp"
    elif lower.endswith((".jpg", ".jpeg")):
        fmt = "jpeg"
    else:
        fmt = "jpeg"

    b64 = base64.b64encode(image_data).decode("utf-8")
    return f"data:image/{fmt};base64,{b64}"


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


@tool(
    name="web_search",
    description=(
        "Search the web for information. Use this when you need current events, "
        "technical documentation, or other information from the internet."
    ),
    category="web",
    requires=["minimax_provider"],
    timeout=WEB_SEARCH_TIMEOUT,
)
def web_search(query: Annotated[str, "The search query to find information"]) -> str:
    """Search the web via MiniMax's coding_plan/search API.

    Returns organic search results with titles, links, and snippets.
    """
    log_debug(f"web_search: query={query!r}")

    data = _call_minimax_api("/v1/coding_plan/search", {"q": query}, WEB_SEARCH_TIMEOUT)

    # Format results for the LLM to consume
    organic = data.get("organic", [])
    related = data.get("related_searches", [])

    if not organic:
        return "(No search results found.)"

    lines: list[str] = []
    for i, result in enumerate(organic, 1):
        title = result.get("title", "Untitled")
        link = result.get("link", "")
        snippet = result.get("snippet", "")
        date = result.get("date", "")

        lines.append(f"{i}. **{title}**")
        if link:
            lines.append(f"   {link}")
        if snippet:
            lines.append(f"   {snippet}")
        if date:
            lines.append(f"   *{date}*")
        lines.append("")

    if related:
        lines.append("**Related searches:**")
        for r in related:
            q = r.get("query", "")
            if q:
                lines.append(f"  - {q}")

    return "\n".join(lines)


@tool(
    name="understand_image",
    description=(
        "Analyze an image using AI vision. Provide an image URL, "
        "local file path, or base64 data, and a prompt describing what to "
        "analyze."
    ),
    category="web",
    requires=["minimax_provider"],
    timeout=UNDERSTAND_IMAGE_TIMEOUT,
)
def understand_image(
    image: Annotated[
        str,
        "Image to analyze: a URL (http://... or https://...), a local file path, or a base64 data URL (data:...).",
    ],
    query: Annotated[str, "Question or analysis request about the image"],
) -> str:
    """Analyze an image via MiniMax's coding_plan/vlm API.

    Converts URLs and local files to base64 data URLs before sending.
    """
    log_debug(f"understand_image: image_len={len(image)}, query={query!r}")

    image_url = _process_image_source(image)
    data = _call_minimax_api(
        "/v1/coding_plan/vlm",
        {"prompt": query, "image_url": image_url},
        UNDERSTAND_IMAGE_TIMEOUT,
    )

    content = data.get("content", "")
    if not content:
        return "(Image analysis returned no content.)"

    return content

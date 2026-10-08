"""General-purpose web fetch tool — works with any LLM provider.

Fetches arbitrary HTTPS URLs and returns content as text, markdown, or raw HTML.
Uses html2text for HTML→markdown conversion when available, falls back to tag-stripping.
Supports pagination via offset/limit to handle large documentation pages.
"""

from __future__ import annotations

import html as _html_lib
import html.parser
import ipaddress
import re
import socket
import urllib.parse
from typing import TYPE_CHECKING, Annotated

from ..core.errors import ToolError
from ..core.logging import log_debug
from .base import tool

if TYPE_CHECKING:
    import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WEB_FETCH_TIMEOUT = 30.0
WEB_FETCH_DEFAULT_MAX_BYTES = 20 * 1024 * 1024  # 20 MB raw response cap
WEB_FETCH_HARD_MAX_BYTES = 100 * 1024 * 1024  # 100 MB absolute max
WEB_FETCH_MAX_REDIRECTS = 5

# Output limits — tuned to stay under ToolRegistry's TOOL_RESULT_TRUNCATE_LEN (8000)
# so the header + content fits in one chunk without silent truncation.
WEB_FETCH_DEFAULT_LIMIT = 7400
WEB_FETCH_MAX_RETURN_CHARS = 7600

# html2text is imported only when first needed (in _html_to_markdown).
_HAS_HTML2TEXT: bool | None = None


# ---------------------------------------------------------------------------
# Security: private / internal IP detection via ipaddress stdlib
# ---------------------------------------------------------------------------


def _is_private_ip(value: str) -> bool:
    """Check whether an IPv4 or IPv6 address string is private/internal."""
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified


def _is_safe_url(url: str) -> tuple[bool, str]:
    """Check that a URL uses HTTPS and does not target a private/reserved IP.

    Returns (is_safe, error_message).
    """
    parsed = urllib.parse.urlparse(url)

    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        return False, f"Only HTTPS URLs are supported. Got: {scheme}://"

    host = (parsed.hostname or "").lower()
    if not host:
        return False, "URL has no valid host"

    if _is_private_ip(host):
        return False, f"URL host '{host}' is a private/internal IP address"

    return True, ""


def _resolve_hostsafe(host: str) -> tuple[bool, str]:
    """Resolve hostname and verify none of its addresses are private/internal.

    Returns (is_safe, error_message).
    """
    try:
        addr_info = socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return True, ""  # Can't resolve → let the request fail later with a clear error

    for family, _, _, _, sockaddr in addr_info:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        ip = sockaddr[0]
        if _is_private_ip(ip):
            return False, f"URL host '{host}' resolves to private/internal IP '{ip}'"

    return True, ""


def _fetch_with_safe_redirects(
    url: str,
    headers: dict[str, str],
) -> requests.Response:
    """Perform the HTTP request, validating every redirect hop for safety.

    Raises ToolError on unsafe redirects, HTTP errors, or timeouts.
    Returns the final non-redirect response.
    """
    import requests

    current_url = url
    for _hop in range(WEB_FETCH_MAX_REDIRECTS + 1):
        safe, err = _is_safe_url(current_url)
        if not safe:
            raise ToolError(f"Unsafe URL rejected: {err}", tool_name="web_fetch")

        parsed = urllib.parse.urlparse(current_url)
        host = (parsed.hostname or "").lower()
        if host and not _is_private_ip(host):
            host_safe, host_err = _resolve_hostsafe(host)
            if not host_safe:
                raise ToolError(f"Unsafe URL rejected: {host_err}", tool_name="web_fetch")

        try:
            response = requests.get(
                current_url,
                headers=headers,
                timeout=WEB_FETCH_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.Timeout:
            raise ToolError(
                f"Request timed out after {WEB_FETCH_TIMEOUT:.0f}s: {current_url}",
                tool_name="web_fetch",
            ) from None
        except requests.RequestException as e:
            raise ToolError(
                f"Request failed for {current_url}: {e}",
                tool_name="web_fetch",
            ) from e

        if response.status_code not in (301, 302, 303, 307, 308):
            response.raise_for_status()
            return response

        location = response.headers.get("Location", "")
        if not location:
            raise ToolError(
                f"Redirect response (HTTP {response.status_code}) from {current_url} has no Location header",
                tool_name="web_fetch",
            )

        current_url = urllib.parse.urljoin(current_url, location)

    raise ToolError(
        f"Too many redirects (>{WEB_FETCH_MAX_REDIRECTS}) from {url} to {current_url}",
        tool_name="web_fetch",
    )


# ---------------------------------------------------------------------------
# HTML → text conversion
# ---------------------------------------------------------------------------


class _HTMLTagStripper(html.parser.HTMLParser):
    """Strip HTML tags from content, preserving block-level structure.

    Skips <script>, <style>, <noscript>, and <svg> blocks entirely.
    """

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "svg"})

    def __init__(self) -> None:
        super().__init__()
        self.result: list[str] = []
        self._in_pre = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in ("pre", "code"):
            self._in_pre = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in ("pre", "code"):
            self._in_pre = False
        elif tag == "br":
            self.result.append("\n")
        elif tag == "p":
            self.result.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if not self._in_pre:
            data = re.sub(r"[ \t]+", " ", data)
        self.result.append(data)

    def get_text(self) -> str:
        text = "".join(self.result)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _strip_html_tags(html_content: str) -> str:
    """Strip all HTML tags and decode entities, returning plain text."""
    stripper = _HTMLTagStripper()
    try:
        stripper.feed(html_content)
    except Exception:
        # Fallback: regex-based tag stripping + full entity decode
        text = re.sub(r"<[^>]+>", "", html_content)
        return _html_lib.unescape(text).strip()
    return _html_lib.unescape(stripper.get_text())


def _html_to_markdown(html_content: str, url: str) -> str:
    """Convert HTML content to markdown using html2text (with stdlib fallback)."""
    global _HAS_HTML2TEXT
    if _HAS_HTML2TEXT is None:
        try:
            import html2text  # probe only; real use is below

            _HAS_HTML2TEXT = True
        except ImportError:
            _HAS_HTML2TEXT = False

    if _HAS_HTML2TEXT:
        import html2text

        h2t = html2text.HTML2Text()
        h2t.ignore_links = False
        h2t.ignore_images = True
        h2t.body_width = 0  # Don't wrap lines
        h2t.url = url
        return h2t.handle(html_content)
    return _strip_html_tags(html_content)


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------


@tool(
    name="web_fetch",
    description=(
        "Fetch content from a URL and return it as readable text. "
        "Use this to read technical documentation, source code from GitHub, "
        "release notes, API references, and other online resources. "
        "Supports HTTPS URLs returning HTML, plain text, JSON, or Markdown. "
        "For HTML pages use format='markdown' to get converted Markdown, "
        "format='text' for plain text with HTML tags stripped, "
        "or format='html' for raw HTML. "
        "For non-HTML content use format='text'. "
        "Large pages can be read in chunks using offset and limit."
    ),
    category="web",
    requires=[],  # Works with any provider
    timeout=WEB_FETCH_TIMEOUT,
)
def web_fetch(
    url: Annotated[
        str,
        "The HTTPS URL to fetch. Must start with https://. "
        "Cannot point to private IPs, internal networks, or file:// URLs.",
    ],
    format: Annotated[
        str,
        "Output format: 'markdown' (converted Markdown, best for docs), "
        "'text' (plain text with HTML tags stripped), or 'html' (raw HTML). "
        "Default: 'markdown'.",
    ] = "markdown",
    offset: Annotated[
        int,
        "Character offset to start returning content from (0 = beginning). "
        "Use this to page through large documentation pages.",
    ] = 0,
    limit: Annotated[
        int,
        "Maximum number of characters to return. Default: 7400 (fits within tool truncation limit of 8000). "
        "Maximum: 7600. Use offset to page through larger pages.",
    ] = WEB_FETCH_DEFAULT_LIMIT,
    max_bytes: Annotated[
        int,
        "Maximum raw response size in bytes. Increase if fetching large documentation pages. "
        f"Default: {WEB_FETCH_DEFAULT_MAX_BYTES // (1024 * 1024)} MB. Maximum: {WEB_FETCH_HARD_MAX_BYTES // (1024 * 1024)} MB.",
    ] = WEB_FETCH_DEFAULT_MAX_BYTES,
) -> str:
    """Fetch a URL and return its content in the requested format.

    - HTTPS only; private/internal IPs rejected at both hostname and DNS levels.
    - Every redirect hop is validated before following.
    - Response body is streamed and capped by max_bytes (default 20 MB).
    - Character encoding is detected from HTTP headers, falling back to UTF-8.
    - Pagination via offset/limit to read large pages in chunks.
    """
    # Guard offset/limit/max_bytes ranges
    if offset < 0:
        offset = 0
    if limit < 1:
        limit = 1
    if limit > WEB_FETCH_MAX_RETURN_CHARS:
        limit = WEB_FETCH_MAX_RETURN_CHARS
    if max_bytes < 1:
        max_bytes = 1
    if max_bytes > WEB_FETCH_HARD_MAX_BYTES:
        max_bytes = WEB_FETCH_HARD_MAX_BYTES

    log_debug(f"web_fetch: url={url!r}, format={format!r}, offset={offset}, limit={limit}")

    # Validate format
    fmt_lower = format.lower().strip()
    if fmt_lower not in ("markdown", "text", "html"):
        raise ToolError(
            f"Invalid format '{format}'. Must be one of: markdown, text, html.",
            tool_name="web_fetch",
        )

    # Fetch with redirect validation
    headers = {
        "User-Agent": "LucNhan/1.0 (web_fetch)",
        "Accept": "text/html,text/plain,application/xhtml+xml,application/xml,*/*;q=0.9",
    }
    response = _fetch_with_safe_redirects(url, headers)

    # Stream response body with size cap
    chunks: list[bytes] = []
    total_bytes = 0
    try:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise ToolError(
                        f"Response too large (>{max_bytes // 1024 // 1024} MB). "
                        f"Maximum is {max_bytes:,} bytes. Increase max_bytes if needed.",
                        tool_name="web_fetch",
                    )
                chunks.append(chunk)
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(
            f"Error reading response body: {e}",
            tool_name="web_fetch",
        ) from e

    raw_content = b"".join(chunks)

    # Determine content type and encoding
    content_type = (response.headers.get("content-type", "")).lower()
    is_html = "text/html" in content_type or "application/xhtml+xml" in content_type

    # Respect the server's declared encoding, falling back to apparent encoding then UTF-8
    encoding = response.encoding or response.apparent_encoding or "utf-8"
    try:
        text = raw_content.decode(encoding, errors="replace")
    except (LookupError, UnicodeError):
        text = raw_content.decode("utf-8", errors="replace")

    # Convert based on requested format
    if fmt_lower == "html":
        result = text
    elif fmt_lower == "text":
        result = _strip_html_tags(text) if is_html else text
    else:  # markdown
        if is_html:
            result = _html_to_markdown(text, url)
        else:
            result = text

    result = result.strip()
    total_chars = len(result)

    # Apply offset/limit pagination
    if offset > total_chars:
        result_chunk = ""
    else:
        result_chunk = result[offset : offset + limit]

    header = (
        f"[Fetched {url}; total chars: {total_chars:,}; showing offset {offset}-{min(offset + limit, total_chars)}]"
    )

    if not result_chunk and total_chars > 0:
        return f"{header}\n\n(reached end of content)"
    if not result_chunk:
        return f"{header}\n\n(empty response)"

    return f"{header}\n\n{result_chunk}"

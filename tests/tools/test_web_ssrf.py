"""Tests for SSRF (Server-Side Request Forgery) guards in web tools.

Both ``web_fetch`` and ``understand_image`` accept user/agent-supplied URLs
that are fetched server-side. Without protection, a prompt-injected LLM
could fetch:

    http://169.254.169.254/latest/meta-data/iam/security-credentials/
    http://127.0.0.1:19828/        (LLM Wiki)
    http://10.0.0.1/admin           (internal network)
    file:///etc/passwd              (local file via file:// scheme)

These tests verify the blocklist at three layers:
  1. Literal private IP in the URL hostname
  2. Hostname that resolves to a private IP via DNS
  3. Wrong scheme (http, ftp, file, gopher, etc.)
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

sys.path.insert(0, "")
sys.path.insert(0, "tests")
from tests.mocks.ida_mock import install_ida_mocks

install_ida_mocks()

from lucnhan.core.errors import ToolError
from lucnhan.tools import web, web_fetch


class TestPrivateIPDetection(unittest.TestCase):
    """The ``_is_private_ip`` helper must flag every reserved range."""

    def test_loopback_ipv4(self):
        assert web_fetch._is_private_ip("127.0.0.1") is True
        assert web_fetch._is_private_ip("127.255.255.254") is True

    def test_loopback_ipv6(self):
        assert web_fetch._is_private_ip("::1") is True

    def test_link_local_ipv4(self):
        # AWS / GCP / Azure metadata endpoint
        assert web_fetch._is_private_ip("169.254.169.254") is True

    def test_link_local_ipv6(self):
        assert web_fetch._is_private_ip("fe80::1") is True

    def test_private_class_a(self):
        assert web_fetch._is_private_ip("10.0.0.1") is True
        assert web_fetch._is_private_ip("10.255.255.254") is True

    def test_private_class_b(self):
        assert web_fetch._is_private_ip("172.16.0.1") is True
        assert web_fetch._is_private_ip("172.31.255.254") is True

    def test_private_class_c(self):
        assert web_fetch._is_private_ip("192.168.1.1") is True
        assert web_fetch._is_private_ip("192.168.255.254") is True

    def test_unspecified(self):
        assert web_fetch._is_private_ip("0.0.0.0") is True

    def test_multicast(self):
        assert web_fetch._is_private_ip("224.0.0.1") is True

    def test_public_ip_allowed(self):
        assert web_fetch._is_private_ip("8.8.8.8") is False
        assert web_fetch._is_private_ip("1.1.1.1") is False

    def test_invalid_string(self):
        # Invalid IP strings return False (not crash).
        assert web_fetch._is_private_ip("not-an-ip") is False
        assert web_fetch._is_private_ip("") is False


class TestIsSafeUrl(unittest.TestCase):
    """``_is_safe_url`` validates scheme + literal hostname."""

    def test_https_public_domain(self):
        safe, err = web_fetch._is_safe_url("https://example.com/page")
        assert safe is True
        assert err == ""

    def test_http_rejected(self):
        safe, err = web_fetch._is_safe_url("http://example.com/page")
        assert safe is False
        assert "HTTPS" in err

    def test_ftp_rejected(self):
        safe, _ = web_fetch._is_safe_url("ftp://example.com/file")
        assert safe is False

    def test_file_scheme_rejected(self):
        safe, _ = web_fetch._is_safe_url("file:///etc/passwd")
        assert safe is False

    def test_no_scheme_rejected(self):
        safe, _ = web_fetch._is_safe_url("example.com")
        assert safe is False

    def test_literal_private_ip_rejected(self):
        safe, err = web_fetch._is_safe_url("https://127.0.0.1/admin")
        assert safe is False
        assert "private" in err.lower() or "internal" in err.lower()

    def test_literal_aws_metadata_rejected(self):
        safe, _ = web_fetch._is_safe_url("https://169.254.169.254/latest/meta-data/")
        assert safe is False

    def test_literal_internal_network_rejected(self):
        safe, _ = web_fetch._is_safe_url("https://10.0.0.1/")
        assert safe is False

    def test_empty_host_rejected(self):
        safe, _ = web_fetch._is_safe_url("https:///path")
        assert safe is False


class TestResolveHostsafe(unittest.TestCase):
    """``_resolve_hostsafe`` validates DNS results to defeat DNS rebinding."""

    def test_public_ip_allowed(self):
        with mock.patch.object(web_fetch.socket, "getaddrinfo") as mock_resolve:
            mock_resolve.return_value = [
                (2, 1, 6, "", ("8.8.8.8", 443)),  # AF_INET, SOCK_STREAM
            ]
            safe, err = web_fetch._resolve_hostsafe("dns.google")
            assert safe is True
            assert err == ""

    def test_dns_rebinding_to_private_rejected(self):
        # Hostname resolves to a private IP — block.
        with mock.patch.object(web_fetch.socket, "getaddrinfo") as mock_resolve:
            mock_resolve.return_value = [
                (2, 1, 6, "", ("127.0.0.1", 443)),
            ]
            safe, err = web_fetch._resolve_hostsafe("evil.example.com")
            assert safe is False
            assert "127.0.0.1" in err

    def test_unresolvable_host_returns_safe(self):
        # If we can't resolve, we don't block — let the request fail later
        # with a clear connection error. This avoids false positives on
        # temporarily flaky DNS.
        with mock.patch.object(
            web_fetch.socket,
            "getaddrinfo",
            side_effect=web_fetch.socket.gaierror("name or service not known"),
        ):
            safe, _ = web_fetch._resolve_hostsafe("nonexistent.example.com")
            assert safe is True


class TestRedirectHopLifetime(unittest.TestCase):
    """A streamed redirect hop must not leak its socket."""

    @staticmethod
    def _response(status: int, location: str | None = None) -> mock.MagicMock:
        response = mock.MagicMock()
        response.status_code = status
        response.headers = {} if location is None else {"Location": location}
        return response

    def test_followed_hop_is_closed(self):
        first = self._response(302, "https://example.com/two")
        second = self._response(200)

        with (
            mock.patch.object(web_fetch, "_is_safe_url", return_value=(True, "")),
            mock.patch("requests.get", side_effect=[first, second]),
        ):
            result = web_fetch._fetch_with_safe_redirects("https://example.com/one", {})

        self.assertIs(result, second)
        first.close.assert_called_once()

    def test_hop_without_location_is_closed(self):
        response = self._response(302)

        with (
            mock.patch.object(web_fetch, "_is_safe_url", return_value=(True, "")),
            mock.patch("requests.get", return_value=response),
            self.assertRaises(ToolError),
        ):
            web_fetch._fetch_with_safe_redirects("https://example.com/one", {})

        response.close.assert_called_once()


class TestImageRedirectValidation(unittest.TestCase):
    """understand_image must validate redirect hops like web_fetch does.

    A bare ``requests.get`` follows a public https URL to
    ``http://169.254.169.254/...`` without re-checking, so cloud credentials
    could be pulled and forwarded to the provider.
    """

    @staticmethod
    def _response(status: int, location: str | None = None) -> mock.MagicMock:
        response = mock.MagicMock()
        response.status_code = status
        response.headers = {"content-type": "image/png"}
        if location is not None:
            response.headers["Location"] = location
        return response

    def test_redirect_to_metadata_is_rejected(self):
        redirect = self._response(302, "http://169.254.169.254/latest/meta-data/")
        with mock.patch("requests.get", return_value=redirect):
            with self.assertRaises(ToolError) as cm:
                web._process_image_source("https://example.com/img.png")
        assert "Unsafe URL rejected" in str(cm.exception)

    def test_https_redirect_is_followed(self):
        redirect = self._response(302, "https://cdn.example.com/img.png")
        final = self._response(200)
        final.iter_content.return_value = iter([b"\x89PNG\r\n\x1a\n"])

        with mock.patch("requests.get", side_effect=[redirect, final]):
            result = web._process_image_source("https://example.com/img.png")

        assert result.startswith("data:image/png;base64,")


class TestUnderstandImageSSRFGuard(unittest.TestCase):
    """``understand_image`` must reject SSRF attempts in image URLs."""

    def test_http_url_rejected(self):
        with self.assertRaises(ToolError) as cm:
            web._process_image_source("http://example.com/image.png")
        assert "SSRF" in str(cm.exception) or "HTTPS" in str(cm.exception)

    def test_loopback_url_rejected(self):
        with self.assertRaises(ToolError) as cm:
            web._process_image_source("https://127.0.0.1/image.png")
        assert "SSRF" in str(cm.exception)

    def test_aws_metadata_url_rejected(self):
        with self.assertRaises(ToolError) as cm:
            web._process_image_source("https://169.254.169.254/latest/meta-data/")
        assert "SSRF" in str(cm.exception)

    def test_internal_network_url_rejected(self):
        with self.assertRaises(ToolError) as cm:
            web._process_image_source("https://10.0.0.1/admin")
        assert "SSRF" in str(cm.exception)

    def test_dns_rebinding_blocked(self):
        # Hostname that resolves to private IP must be blocked.
        with mock.patch.object(web_fetch.socket, "getaddrinfo") as mock_resolve:
            mock_resolve.return_value = [
                (2, 1, 6, "", ("192.168.1.1", 443)),
            ]
            with self.assertRaises(ToolError) as cm:
                web._process_image_source("https://internal-service.example.com/img.png")
            assert "SSRF" in str(cm.exception)
            assert "192.168.1.1" in str(cm.exception)

    def test_local_file_path_still_works(self):
        # Local file paths must continue to work (this is not URL fetching).
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
            tmp_path = f.name
        try:
            result = web._process_image_source(tmp_path)
            assert result.startswith("data:image/png;base64,")
        finally:
            os.unlink(tmp_path)

    def test_oversized_local_image_is_rejected(self):
        """Images are base64-inflated and sent in one request — cap them."""
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            f.seek(web._IMAGE_MAX_BYTES + 1)
            f.write(b"\x00")
            tmp_path = f.name
        try:
            with self.assertRaises(ToolError) as cm:
                web._process_image_source(tmp_path)
            assert "too large" in str(cm.exception)
        finally:
            os.unlink(tmp_path)

    def test_oversized_download_is_aborted_mid_stream(self):
        """A huge URL body must not be buffered whole."""
        response = mock.MagicMock()
        response.headers = {"content-type": "image/png"}
        oversized = b"x" * (web._IMAGE_MAX_BYTES + 1)
        response.iter_content.return_value = iter([oversized])

        with (
            mock.patch.object(web_fetch, "_is_safe_url", return_value=(True, "")),
            mock.patch.object(web_fetch, "_resolve_hostsafe", return_value=(True, "")),
            mock.patch("requests.get", return_value=response),
        ):
            with self.assertRaises(ToolError) as cm:
                web._process_image_source("https://example.com/big.png")
        assert "too large" in str(cm.exception)
        response.close.assert_called_once()

    def test_data_url_passthrough(self):
        # Base64 data URLs must not be URL-validated.
        data_url = "data:image/png;base64,iVBORw0KGgo="
        result = web._process_image_source(data_url)
        assert result == data_url


if __name__ == "__main__":
    unittest.main()


class TestWebFetchEncoding(unittest.TestCase):
    """The charset must come from the bytes, not from an exhausted response.

    ``response.apparent_encoding`` reads ``response.content``, which is already
    consumed by the ``iter_content`` cap loop, so it sniffs nothing (a mock
    hands back a MagicMock, which ``bytes.decode`` rejects with a TypeError).
    """

    def test_missing_charset_uses_the_read_bytes(self):
        response = mock.MagicMock()
        response.status_code = 200
        response.headers = {"content-type": "text/plain"}
        response.encoding = None
        response.iter_content.return_value = iter(["caf\u00e9".encode("cp1252")])

        with mock.patch.object(web_fetch, "_fetch_with_safe_redirects", return_value=response):
            with mock.patch.object(web_fetch, "_apparent_encoding", return_value="cp1252"):
                text = web_fetch.web_fetch("https://example.com/a.txt")

        assert "caf\u00e9" in text
        response.close.assert_called_once()

    def test_declared_charset_wins(self):
        response = mock.MagicMock()
        response.status_code = 200
        response.headers = {"content-type": "text/plain; charset=latin-1"}
        response.encoding = "latin-1"
        response.iter_content.return_value = iter(["caf\u00e9".encode("latin-1")])

        with mock.patch.object(web_fetch, "_fetch_with_safe_redirects", return_value=response):
            with mock.patch.object(web_fetch, "_apparent_encoding") as sniff:
                text = web_fetch.web_fetch("https://example.com/a.txt")

        assert "caf\u00e9" in text
        sniff.assert_not_called()


class TestWebSearchResponseShape(unittest.TestCase):
    """The API reply is untrusted JSON; wrong types must not raise AttributeError."""

    def _call(self, payload: object) -> str:
        with mock.patch.object(web, "_call_minimax_api", return_value=payload):
            return web.web_search("q")

    def test_non_object_reply_is_a_tool_error(self):
        with self.assertRaises(ToolError):
            self._call(["not", "an", "object"])

    def test_non_dict_entries_are_skipped(self):
        payload = {
            "organic": [{"title": "T", "link": "https://x", "snippet": "s"}, "junk"],
            "related_searches": [{"query": "q2"}, 7],
        }
        out = self._call(payload)
        assert "T" in out
        assert "q2" in out

    def test_wrong_typed_containers_fall_back(self):
        out = self._call({"organic": {"a": 1}, "related_searches": "x", "base_resp": []})
        assert "No search results found" in out


class TestUnderstandImageResponseShape(unittest.TestCase):
    def test_non_string_content_is_tolerated(self):
        with mock.patch.object(web, "_process_image_source", return_value="data:image/png;base64,AA"):
            with mock.patch.object(web, "_call_minimax_api", return_value={"content": ["a", "b"]}):
                out = web.understand_image("x.png", "what")
        assert "no content" in out

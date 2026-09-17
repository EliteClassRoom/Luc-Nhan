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

from rikugan.core.errors import ToolError
from rikugan.tools import web, web_fetch


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

    def test_data_url_passthrough(self):
        # Base64 data URLs must not be URL-validated.
        data_url = "data:image/png;base64,iVBORw0KGgo="
        result = web._process_image_source(data_url)
        assert result == data_url


if __name__ == "__main__":
    unittest.main()

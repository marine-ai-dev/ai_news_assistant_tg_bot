"""media.urlsafety — Step 4 SSRF protection."""

from __future__ import annotations

import socket

import pytest

from ai_news_editor.media.urlsafety import UnsafeMediaUrlError, validate_media_url


class TestSchemeAndSyntax:
    def test_https_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))]
        )
        validate_media_url("https://example.invalid/photo.jpg")

    def test_file_scheme_is_rejected(self) -> None:
        with pytest.raises(UnsafeMediaUrlError, match="only http and https"):
            validate_media_url("file:///etc/passwd")

    def test_ftp_scheme_is_rejected(self) -> None:
        with pytest.raises(UnsafeMediaUrlError, match="only http and https"):
            validate_media_url("ftp://example.invalid/x.jpg")

    def test_a_url_with_no_host_is_rejected(self) -> None:
        with pytest.raises(UnsafeMediaUrlError, match="no host"):
            validate_media_url("https:///photo.jpg")


class TestLiteralAddresses:
    def test_localhost_hostname_is_rejected(self) -> None:
        with pytest.raises(UnsafeMediaUrlError, match="loopback"):
            validate_media_url("http://localhost/x.jpg")

    def test_dot_localhost_is_rejected(self) -> None:
        with pytest.raises(UnsafeMediaUrlError, match="loopback"):
            validate_media_url("http://foo.localhost/x.jpg")

    def test_127_loopback_literal_is_rejected(self) -> None:
        with pytest.raises(UnsafeMediaUrlError, match="private, loopback"):
            validate_media_url("http://127.0.0.1/x.jpg")

    def test_ipv6_loopback_literal_is_rejected(self) -> None:
        with pytest.raises(UnsafeMediaUrlError, match="private, loopback"):
            validate_media_url("http://[::1]/x.jpg")

    def test_rfc1918_private_literal_is_rejected(self) -> None:
        with pytest.raises(UnsafeMediaUrlError, match="private, loopback"):
            validate_media_url("http://10.0.0.5/x.jpg")

        with pytest.raises(UnsafeMediaUrlError, match="private, loopback"):
            validate_media_url("http://192.168.1.1/x.jpg")

    def test_link_local_literal_is_rejected(self) -> None:
        with pytest.raises(UnsafeMediaUrlError, match="private, loopback"):
            validate_media_url("http://169.254.169.254/x.jpg")

    def test_a_public_literal_ip_is_allowed(self) -> None:
        validate_media_url("http://93.184.216.34/x.jpg")


class TestDnsResolution:
    """The gap a purely-syntactic check leaves open: a hostname that *resolves* to a
    private address, not one written as a literal IP."""

    def test_a_hostname_resolving_to_a_private_address_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("10.0.0.5", 0))]
        )
        with pytest.raises(UnsafeMediaUrlError, match="private, loopback"):
            validate_media_url("http://internal.example.invalid/x.jpg")

    def test_a_hostname_resolving_to_a_public_address_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))]
        )
        validate_media_url("http://cdn.example.invalid/x.jpg")

    def test_a_hostname_with_one_private_and_one_public_address_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any resolved address being unsafe is enough to reject the whole hostname —
        a caller cannot control which address a later connection actually uses."""
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0)), (0, 0, 0, "", ("127.0.0.1", 0))],
        )
        with pytest.raises(UnsafeMediaUrlError, match="private, loopback"):
            validate_media_url("http://mixed.example.invalid/x.jpg")

    def test_a_hostname_that_fails_to_resolve_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_args: object, **_kwargs: object) -> None:
            raise OSError("nodename nor servname provided")

        monkeypatch.setattr(socket, "getaddrinfo", _raise)
        with pytest.raises(UnsafeMediaUrlError, match="could not resolve"):
            validate_media_url("http://nonexistent.example.invalid/x.jpg")

    def test_ipv6_zone_id_is_stripped_before_parsing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("fe80::1%eth0", 0))]
        )
        with pytest.raises(UnsafeMediaUrlError, match="private, loopback"):
            validate_media_url("http://linklocal.example.invalid/x.jpg")

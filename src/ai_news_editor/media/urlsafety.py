"""SSRF-hardened URL validation for the media downloader — Step 4 (AI News Agent v2).

``sources/http.py`` already has a ``validate_url`` — reused nowhere here, on purpose.
That module's own docstring says why it stops short: "``config/sources.yaml`` is local
and operator-controlled... it does not resolve DNS." A media URL is the opposite case:
it was extracted from a third party's feed/HTML by ``media.discover``, so it is
untrusted input in the fullest sense, and deserves the stronger check that module
explicitly declined to be.

This module resolves the hostname and validates every resolved address, not just a
literal IP written in the URL — the gap a purely syntactic check leaves open. It is
still not airtight against DNS rebinding (the name could re-resolve to a different
address between this check and the actual connection); closing that fully would mean
pinning the resolved IP for the connection itself, which ``media.download`` does not
currently do. Documented here rather than silently assumed solved.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from ai_news_editor.domain.errors import FatalError

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeMediaUrlError(FatalError):
    """A media URL is not something this application is willing to fetch.

    Fatal, not retryable: an unsafe URL does not become safe on a second attempt.
    """


def validate_media_url(url: str) -> None:
    """Reject a media URL by scheme, syntax, or where its hostname actually resolves.

    Raises:
        UnsafeMediaUrlError: non-http(s) scheme, missing host, a literal or
            DNS-resolved private/loopback/link-local/reserved address, or a hostname
            that fails to resolve at all.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeMediaUrlError(f"only http and https are allowed, got {parts.scheme!r}: {url}")
    if not parts.hostname:
        raise UnsafeMediaUrlError(f"URL has no host: {url}")

    hostname = parts.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeMediaUrlError(f"refusing to fetch a loopback address: {url}")

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        _reject_unsafe_address(literal, url)
        return

    resolved = _resolve(hostname, url)
    for address in resolved:
        _reject_unsafe_address(address, url)


def _resolve(hostname: str, url: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        raise UnsafeMediaUrlError(f"could not resolve {hostname!r}: {exc}") from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        raw = info[4][0]
        # A resolved IPv6 address carries a zone id past a '%' (e.g. link-local
        # 'fe80::1%eth0') that ipaddress.ip_address() cannot parse — strip it before
        # parsing; the address itself is still validated below regardless.
        candidate = raw.split("%", 1)[0]
        try:
            addresses.append(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    if not addresses:
        raise UnsafeMediaUrlError(f"{hostname!r} in {url} did not resolve to any usable address")
    return addresses


def _reject_unsafe_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, url: str
) -> None:
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise UnsafeMediaUrlError(
            f"refusing to fetch a private, loopback, link-local or reserved address "
            f"({address}) for {url}"
        )


__all__ = ["UnsafeMediaUrlError", "validate_media_url"]

"""Deterministic URL canonicalization.

Two URLs that differ only in tracking noise should canonicalize to the same string, so
the same story arriving from two feeds is recognised as one.

The governing rule is conservative: **a canonicalizer that changes meaning is worse
than a few duplicates slipping through.** Only parameters that are known-universally
tracking are removed; anything that might select content (``id``, ``p``, ``page``,
``v``, ``q``, …) is preserved. When in doubt, keep it.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Parameters that never select content — analytics and click attribution only.
#: Deliberately a short, well-understood list rather than a broad heuristic.
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "vero_id",
        "yclid",
        "ref_src",
        "ref_url",
        "spm",
        "scid",
    }
)

#: Prefixes covering the utm_* family and similar vendor namespaces.
_TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "pk_", "piwik_", "matomo_", "hsa_")

_DEFAULT_PORTS = {"http": 80, "https": 443}
_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Index documents that denote the same resource as the bare directory.
_INDEX_SUFFIXES = ("/index.html", "/index.htm", "/index.php")


class InvalidUrlError(ValueError):
    """The URL cannot be canonicalized into something fetchable."""


def is_tracking_param(name: str) -> bool:
    """Whether a query parameter is pure tracking noise."""
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def canonicalize_url(url: str) -> str:
    """Return a canonical form of ``url``.

    Applies only transformations that provably preserve the addressed resource:

    * scheme and host lowercased (both are case-insensitive per RFC 3986)
    * a leading ``www.`` removed
    * the default port for the scheme removed
    * the fragment dropped (never sent to a server)
    * tracking parameters removed; every other parameter kept, order preserved
    * ``/index.html`` and friends reduced to their directory
    * a single trailing slash removed from non-root paths

    Path case is preserved: path segments are case-sensitive on many servers.

    Raises:
        InvalidUrlError: empty, unparseable, non-HTTP, or missing a host.
    """
    if not url or not url.strip():
        raise InvalidUrlError("empty URL")

    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise InvalidUrlError(f"unparseable URL {url!r}: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise InvalidUrlError(f"unsupported scheme {parts.scheme!r} in {url!r}")

    try:
        hostname = parts.hostname
    except ValueError as exc:
        raise InvalidUrlError(f"invalid host in {url!r}: {exc}") from exc
    if not hostname:
        raise InvalidUrlError(f"URL has no host: {url!r}")

    host = hostname.lower().removeprefix("www.")
    if not host:
        raise InvalidUrlError(f"URL has no host: {url!r}")

    netloc = host
    port = _port_of(parts, url)
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{host}:{port}"

    return urlunsplit((scheme, netloc, _canonical_path(parts.path), _canonical_query(parts), ""))


def _port_of(parts: object, url: str) -> int | None:
    try:
        return parts.port  # type: ignore[attr-defined]
    except ValueError as exc:
        raise InvalidUrlError(f"invalid port in {url!r}: {exc}") from exc


def _canonical_path(path: str) -> str:
    if not path:
        return "/"
    for suffix in _INDEX_SUFFIXES:
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)] + "/"
            break
    # A trailing slash rarely distinguishes a resource, but the root always needs one.
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return path or "/"


def _canonical_query(parts: object) -> str:
    query = parts.query  # type: ignore[attr-defined]
    if not query:
        return ""
    kept = [
        (name, value)
        for name, value in parse_qsl(query, keep_blank_values=True)
        if not is_tracking_param(name)
    ]
    return urlencode(kept)


def try_canonicalize_url(url: str | None) -> str | None:
    """Canonicalize, returning ``None`` instead of raising for unusable input."""
    if not url:
        return None
    try:
        return canonicalize_url(url)
    except InvalidUrlError:
        return None

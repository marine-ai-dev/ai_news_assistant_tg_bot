"""Adapter registry: source kind → adapter implementation.

A dict and a lookup, not a plugin framework. Adding an adapter kind in a later phase is
one import and one entry.
"""

from __future__ import annotations

from collections.abc import Callable

from ai_news_editor.domain.enums import SourceKind
from ai_news_editor.domain.errors import ConfigurationError
from ai_news_editor.sources.base import SourceAdapter
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.sources.rss import RssAdapter

#: Only RSS exists in Phase 2. HTML_CHANGELOG and HN_SIGNAL are declared in the domain
#: vocabulary but have no implementation yet, and a source configured for them fails
#: loudly rather than being silently skipped.
_ADAPTERS: dict[SourceKind, Callable[[HttpClient], SourceAdapter]] = {
    SourceKind.RSS: RssAdapter,
}


def supported_kinds() -> frozenset[SourceKind]:
    """Source kinds that have a working adapter."""
    return frozenset(_ADAPTERS)


def build_adapter(kind: SourceKind, http: HttpClient) -> SourceAdapter:
    """Return an adapter for ``kind``.

    Raises:
        ConfigurationError: no adapter is implemented for that kind yet.
    """
    factory = _ADAPTERS.get(kind)
    if factory is None:
        available = ", ".join(sorted(k.value for k in _ADAPTERS))
        raise ConfigurationError(
            f"no adapter implemented for source kind {kind.value!r}; available: {available}"
        )
    return factory(http)

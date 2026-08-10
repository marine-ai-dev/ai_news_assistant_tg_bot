"""Adapter registry: source kind → adapter implementation.

A dict and a lookup, not a plugin framework. Adding an adapter kind in a later phase is
one import and one entry.
"""

from __future__ import annotations

from collections.abc import Callable

from ai_news_editor.domain.enums import SourceKind
from ai_news_editor.domain.errors import ConfigurationError
from ai_news_editor.sources.base import SourceAdapter
from ai_news_editor.sources.hn_algolia import HnSignalAdapter
from ai_news_editor.sources.html_changelog import HtmlChangelogAdapter
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.sources.rss import RssAdapter

#: A source configured for a kind with no adapter fails loudly rather than being
#: silently skipped.
_ADAPTERS: dict[SourceKind, Callable[[HttpClient], SourceAdapter]] = {
    SourceKind.RSS: RssAdapter,
    SourceKind.HTML_CHANGELOG: HtmlChangelogAdapter,
    SourceKind.HN_SIGNAL: HnSignalAdapter,
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
        name = getattr(kind, "value", kind)
        raise ConfigurationError(
            f"no adapter implemented for source kind {name!r}; available: {available}"
        )
    return factory(http)

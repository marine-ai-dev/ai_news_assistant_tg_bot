"""Deterministic text normalization and HTML-to-text extraction.

Strictly mechanical. This module decodes, strips markup, collapses whitespace and
normalizes Unicode. It never rewrites prose, summarizes, translates, infers a title or
improves wording — everything downstream depends on the stored text being what the
source actually said.

HTML here is data. It is parsed for text extraction, never rendered and never executed;
``<script>`` and ``<style>`` content is discarded rather than treated as text.
"""

from __future__ import annotations

import re
import unicodedata

from selectolax.parser import HTMLParser

#: Elements whose text is markup plumbing, not content.
_NON_CONTENT_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "iframe",
    "form",
    "nav",
    "aside",
)

#: Elements that imply a visible line break when flattened to text.
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "li", "tr", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "ul", "ol", "table",
    }
)

#: Storage bounds. Exceeding these truncates *explicitly* — see :func:`truncate`.
MAX_TITLE_CHARS = 500
MAX_TEXT_CHARS = 20_000
TRUNCATION_MARKER = " […]"

_WHITESPACE = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n{3,}")
#: Zero-width and bidirectional control characters: invisible, and a spoofing vector.
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")


def looks_like_html(value: str) -> bool:
    """Cheap check for markup, so plain text is not put through a parser."""
    return "<" in value and ">" in value


def html_to_text(html: str) -> str:
    """Extract readable text from an HTML fragment.

    Scripts, styles and other non-content elements are dropped; block-level elements
    become line breaks so paragraph structure survives. Entities are decoded by the
    parser. No JavaScript is executed and no network request is made — the parser only
    ever sees the string it is given.
    """
    if not html.strip():
        return ""

    tree = HTMLParser(html)
    for tag in _NON_CONTENT_TAGS:
        for node in tree.css(tag):
            node.decompose()

    body = tree.body or tree.root
    if body is None:
        return ""

    pieces: list[str] = []
    _flatten(body, pieces)
    return normalize_whitespace("".join(pieces))


def _flatten(node: object, pieces: list[str]) -> None:
    """Walk the tree, emitting text and turning block elements into line breaks.

    Recursive rather than taking ``node.text()`` on each child: that shortcut collapses
    nested block structure into spaces, so paragraphs inside a wrapper ``<div>`` would
    run together.
    """
    tag = getattr(node, "tag", None)
    if tag == "-text":
        text = node.text(deep=False)  # type: ignore[attr-defined]
        if text:
            pieces.append(text)
        return

    is_block = tag in _BLOCK_TAGS
    if is_block:
        pieces.append("\n")
    for child in node.iter(include_text=True):  # type: ignore[attr-defined]
        _flatten(child, pieces)
    if is_block:
        pieces.append("\n")


def normalize_whitespace(value: str) -> str:
    """Collapse runs of spaces and blank lines while preserving paragraph breaks."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _WHITESPACE.sub(" ", value)
    value = "\n".join(line.strip() for line in value.split("\n"))
    value = _BLANK_LINES.sub("\n\n", value)
    return value.strip()


def normalize_unicode(value: str) -> str:
    """Apply NFC normalization and remove invisible control characters.

    NFC keeps Ukrainian and other non-ASCII text intact while making canonically
    equivalent sequences compare equal — necessary for stable fingerprints.
    """
    return unicodedata.normalize("NFC", _INVISIBLE.sub("", value))


def clean_text(value: str | None, *, limit: int = MAX_TEXT_CHARS) -> str | None:
    """Normalize arbitrary source text (HTML or plain) into stored text.

    Returns ``None`` for input that holds no text at all, so "absent" stays
    distinguishable from "empty string".
    """
    if value is None:
        return None
    text = html_to_text(value) if looks_like_html(value) else value
    text = normalize_whitespace(normalize_unicode(text))
    if not text:
        return None
    return truncate(text, limit)


def clean_title(value: str | None) -> str | None:
    """Normalize a title: markup removed, whitespace collapsed, newlines flattened."""
    text = clean_text(value, limit=MAX_TITLE_CHARS)
    if text is None:
        return None
    flattened = normalize_whitespace(text.replace("\n", " "))
    return flattened or None


def truncate(value: str, limit: int) -> str:
    """Shorten to ``limit`` characters, marking the cut so it is never silent."""
    if len(value) <= limit:
        return value
    keep = max(0, limit - len(TRUNCATION_MARKER))
    return value[:keep].rstrip() + TRUNCATION_MARKER


def tokenize(value: str) -> list[str]:
    """Split normalized text into lowercase word tokens for fingerprinting.

    Unicode-aware, so Ukrainian and other non-Latin scripts tokenize the same way
    English does rather than being discarded.
    """
    return [
        token
        for token in re.findall(r"\w+", normalize_unicode(value).lower(), flags=re.UNICODE)
        if token
    ]

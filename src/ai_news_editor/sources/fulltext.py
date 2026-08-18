"""Fetching one article's full text — narrowly, and only for the automation pipeline.

:mod:`sources.html_changelog` deliberately never follows a link to an individual
article: *"that is the first step towards a general-purpose crawler."* This module is
the one bounded exception to that policy, and it exists for a single, specific reason.

RSS summaries and changelog excerpts are short — measured against this project's own
collected articles, a few hundred characters on average. That is enough for a human
editor to judge whether a story is worth covering, but it is not enough material to
generate a factually grounded post from: a language model given 150 characters and
asked to write a paragraph will fill the gap with something plausible, which is
indistinguishable from inventing it. The automated NEWS pipeline (:mod:`automation`)
needs the article itself before it asks Gemini to write anything.

This is still not a crawler. It fetches exactly one URL — the canonical URL of a
candidate already selected from a configured OFFICIAL source — extracts the readable
text of that one page, and stops. It never follows a link found on the page, never
paginates, never retries past the shared :class:`~sources.http.HttpClient` policy, and
never touches anything outside ``http``/``https`` or a private address (both already
enforced by that client). Fetching only through it also means the same size cap,
redirect limit, timeout and User-Agent already governing every other outbound request
in this project apply here without being re-specified.

Fail-closed is the operating principle throughout: any of a timeout, a non-2xx status,
a non-HTML response, unparseable markup, or text that survives extraction but is too
short to write from returns :attr:`FullTextResult.ok` ``False`` with a reason, never a
partial or best-effort body. The caller's job is to skip the candidate, not to guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from selectolax.parser import HTMLParser

from ai_news_editor.observability.logging import get_logger
from ai_news_editor.pipeline.text import clean_text
from ai_news_editor.sources.http import HttpClient, HttpError, HttpStatusError, UnsafeUrlError

logger = get_logger(__name__)

#: Below this many characters, extracted text is not "an article" — it is a paywall
#: stub, a cookie notice, or a page that failed to render without JavaScript. Chosen
#: well above the ~500-character ceiling already observed on RSS-only excerpts, so a
#: fetch that merely reproduces the summary is correctly treated as insufficient.
MIN_FULLTEXT_CHARS = 800

#: Full articles, not RSS excerpts, so the cap is generous — comfortably above any real
#: AI-news blog post — while still bounding what a single candidate can cost to send to
#: Gemini as prompt tokens.
MAX_FULLTEXT_CHARS = 12_000

#: Elements whose text is never part of an article body, stripped before extraction.
_NOISE_TAGS = (
    "script", "style", "noscript", "nav", "header", "footer", "aside", "iframe",
    "form", "svg", "figure", "button",
)

#: Tried in order; the first selector that matches *something* wins. Ordered from most
#: to least specific so a page with a real ``<article>`` element never falls through to
#: a looser match that might catch a sidebar.
_CONTENT_SELECTORS = (
    "article",
    "main",
    "[role=main]",
    ".post-content, .entry-content, .article-content, .article-body",
    "#content, .content",
)

_ACCEPTED_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


@dataclass(frozen=True, slots=True)
class FullTextResult:
    """The outcome of one full-text fetch. Never a partial success."""

    url: str
    ok: bool
    text: str | None = None
    #: Present exactly when ``ok`` is False — why this candidate cannot be used.
    reason: str | None = None
    #: The response status, when the failure was one — after HttpClient's own transient
    #: retries are exhausted, so a 429 here means sustained throttling, not a blip.
    #: ``None`` for every non-HTTP failure (timeout, transport, unsafe URL, bad markup).
    #: Automation uses this to tell "this one page is unavailable" apart from "this
    #: whole domain is currently unavailable to this fetcher" — see
    #: automation.pipeline's domain-cooldown logic.
    status_code: int | None = None

    def __post_init__(self) -> None:
        if self.ok and not self.text:  # pragma: no cover - defensive
            raise ValueError("a successful result must carry text")
        if not self.ok and self.text is not None:  # pragma: no cover - defensive
            raise ValueError("a failed result must not carry text")
        if not self.ok and not self.reason:  # pragma: no cover - defensive
            raise ValueError("a failed result must say why")


def fetch_fulltext(url: str, *, http: HttpClient | None = None) -> FullTextResult:
    """Fetch and extract the readable text of one article page.

    Every failure mode returns ``ok=False`` with a reason rather than raising — the
    automation pipeline's response to any of them is identical (skip this candidate),
    so there is nothing for a caller to gain from a distinct exception type, and a
    fetch failure here must never be mistaken for a bug that stops a whole run.

    Args:
        http: an existing client to reuse (pooled connections across several
            candidates in one run); a short-lived one is created when omitted.
    """
    owns_client = http is None
    client = http or HttpClient()
    try:
        response = client.get(url)
    except UnsafeUrlError as exc:
        return FullTextResult(url=url, ok=False, reason=f"unsafe URL: {exc}")
    except HttpError as exc:
        status = exc.status_code if isinstance(exc, HttpStatusError) else None
        return FullTextResult(
            url=url, ok=False, reason=f"fetch failed: {exc}", status_code=status
        )
    finally:
        if owns_client:
            client.close()

    if response.status_code == 304:  # pragma: no cover - fulltext never sends etags
        return FullTextResult(url=url, ok=False, reason="304 Not Modified with no body")
    if response.status_code >= 400:  # pragma: no cover - HttpClient raises first
        return FullTextResult(url=url, ok=False, reason=f"HTTP {response.status_code}")

    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type and content_type not in _ACCEPTED_CONTENT_TYPES:
        return FullTextResult(
            url=url, ok=False, reason=f"not an HTML page (content-type: {content_type})"
        )

    try:
        body = response.body.decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - decode with replacement cannot raise
        return FullTextResult(url=url, ok=False, reason=f"could not decode response: {exc}")

    text = _extract(body)
    if text is None:
        return FullTextResult(url=url, ok=False, reason="could not parse the page as HTML")

    normalized = clean_text(text, limit=MAX_FULLTEXT_CHARS)
    if not normalized or len(normalized) < MIN_FULLTEXT_CHARS:
        found = len(normalized) if normalized else 0
        return FullTextResult(
            url=url, ok=False,
            reason=f"extracted text too short for factual generation ({found} chars, "
                   f"need {MIN_FULLTEXT_CHARS})",
        )

    logger.info("fetched full article text", extra={"url": url, "chars": len(normalized)})
    return FullTextResult(url=url, ok=True, text=normalized)


def _extract(html: str) -> str | None:
    """Best-effort readable text from a page, or ``None`` if the markup will not parse.

    Malformed HTML is not an error for selectolax — it is a lenient, HTML5-tolerant
    parser (Lexbor) that already exists as a project dependency, so it produces some
    tree for almost any input. ``None`` is reserved for input so broken parsing itself
    raises.
    """
    try:
        tree = HTMLParser(html)
    except Exception:  # pragma: no cover - selectolax raises exceedingly rarely
        return None

    for tag in _NOISE_TAGS:
        for node in tree.css(tag):
            node.decompose()

    for selector in _CONTENT_SELECTORS:
        nodes = tree.css(selector)
        if nodes:
            return " ".join(node.text(separator=" ") for node in nodes)

    body = tree.css_first("body")
    return body.text(separator=" ") if body is not None else tree.root.text(separator=" ")

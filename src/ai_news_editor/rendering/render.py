"""Category-specific Telegram MarkdownV2 rendering — Step 5 (AI News Agent v2).

The only code that turns an :class:`~rendering.content.EditorialContent` record into
Telegram markup. Every string field from ``content`` is passed through
``writing.format.escape_markdown_v2`` before it reaches the output — the model that
produced ``content`` cannot make a character act as syntax no matter what it wrote,
because this module is the only place ``*``/``[``/``]``/``(``/``)`` are ever inserted
unescaped, and it inserts them itself, never from a content field.

The four category-specific safety guarantees are enforced here, not merely requested
in a prompt: :func:`_render_lifehack` always wraps its anecdote in report framing and
never renders without ``editorial.safety.validate_lifehack`` passing;
:func:`_render_prompt_workflow` picks the quote-vs-label wording from
``PromptOrigin`` itself, never from anything content claims about it;
:func:`_render_free_deal` labels the offer from ``FreeDealKind``, never lets a trial
read as "free forever"; :func:`_render_research` labels a claim from
``ResearchClaimFraming`` and gates it through ``validate_research_claim``. A caller
cannot skip these by constructing ``EditorialContent`` differently — the fields that
drive them are required by that model itself for their category (see its own
validator).
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_news_editor.domain.enums import EditorialCategory, FreeDealKind, PromptOrigin
from ai_news_editor.editorial.safety import (
    EditorialSafetyError,
    FreeDealClaim,
    LifehackClaim,
    PromptClaim,
    ResearchClaim,
    ResearchClaimFraming,
    validate_free_deal,
    validate_lifehack,
    validate_prompt_provenance,
    validate_research_claim,
)
from ai_news_editor.rendering.content import EditorialContent
from ai_news_editor.rendering.style import (
    ABSOLUTE_BODY_CHARS_WARNING,
    DETAIL_HEADER,
    HYPE_PHRASES,
    SOURCE_LINE_LABEL,
    TARGET_BODY_CHARS_MAX,
    TARGET_BODY_CHARS_MIN,
    WIDE_CATEGORIES,
    block_emoji,
    category_emoji,
)
from ai_news_editor.writing.format import _escape_markdown_v2_url, escape_markdown_v2


class RenderError(ValueError):
    """``content`` cannot be safely rendered for its category."""


@dataclass(frozen=True, slots=True)
class RenderedPost:
    """One fully-assembled MarkdownV2 post, plus the metrics a caller checks it with."""

    full_text: str
    headline_plain: str
    body_chars: int
    warnings: tuple[str, ...] = ()

    @property
    def parse_mode(self) -> str:
        return "MarkdownV2"


def render_editorial_post(content: EditorialContent) -> RenderedPost:
    """Render ``content`` into Telegram MarkdownV2, following its category's template.

    Raises:
        RenderError: the category's safety gate rejected ``content`` (wrong evidence
            for AI_LIFEHACK, a verbatim-quote claim for non-verbatim prompt text, an
            unverified independent-verification claim for RESEARCH, ...).
    """
    dispatch = {
        EditorialCategory.NEWS: _render_standard,
        EditorialCategory.AI_TOOL: _render_standard,
        EditorialCategory.EXPLAINER: _render_standard,
        EditorialCategory.FREE_DEAL: _render_free_deal,
        EditorialCategory.AI_LIFEHACK: _render_lifehack,
        EditorialCategory.PROMPT_WORKFLOW: _render_prompt_workflow,
        EditorialCategory.RESEARCH: _render_research,
        EditorialCategory.WEEKLY_DIGEST: _render_weekly_digest,
    }
    try:
        post = dispatch[content.category](content)
    except EditorialSafetyError as exc:
        raise RenderError(f"{content.category.value}: {exc}") from exc

    return _with_length_warnings(post, content.category)


# --- shared building blocks -----------------------------------------------------


def _headline(content: EditorialContent) -> str:
    emoji = category_emoji(content.category)
    return f"*{emoji} {escape_markdown_v2(content.headline)}*"


def _body(content: EditorialContent) -> str:
    lines = []
    for block in content.body:
        emoji = block_emoji(block.purpose)
        lines.append(f"{emoji} {escape_markdown_v2(block.text)}")
    return "\n\n".join(lines)


def _detail(content: EditorialContent) -> str | None:
    if not content.detail_bullets:
        return None
    bullets = "\n".join(f"• {escape_markdown_v2(item)}" for item in content.detail_bullets)
    return f"{escape_markdown_v2(DETAIL_HEADER)}\n\n{bullets}"


def _source_line(content: EditorialContent) -> str:
    label = escape_markdown_v2(content.source_label)
    url = _escape_markdown_v2_url(content.source_url)
    prefix = escape_markdown_v2(SOURCE_LINE_LABEL)
    return f"🔗 [{prefix}: {label}]({url})"


def _assemble(*parts: str | None) -> str:
    return "\n\n".join(part for part in parts if part)


def _plain_body_chars(content: EditorialContent) -> int:
    return sum(len(block.text) for block in content.body)


# --- standard template: NEWS / AI_TOOL / EXPLAINER -------------------------------


def _render_standard(content: EditorialContent) -> RenderedPost:
    text = _assemble(_headline(content), _body(content), _detail(content), _source_line(content))
    return RenderedPost(
        full_text=text, headline_plain=content.headline, body_chars=_plain_body_chars(content)
    )


# --- FREE_DEAL --------------------------------------------------------------------

_FREE_DEAL_LABEL: dict[FreeDealKind, str] = {
    FreeDealKind.FREE: "Безкоштовно",
    FreeDealKind.FREE_TIER: "Безкоштовний тариф",
    FreeDealKind.FREE_TRIAL: "Безкоштовний пробний період",
    FreeDealKind.OPEN_SOURCE: "Відкритий код",
    FreeDealKind.PROMOTION: "Тимчасова акція",
    FreeDealKind.DISCOUNT: "Знижка",
}


def _render_free_deal(content: EditorialContent) -> RenderedPost:
    assert content.free_deal_kind is not None  # EditorialContent's own validator guarantees this
    # Defense in depth: the real fail-closed evidence gate runs at generation time
    # (editorial.safety.validate_free_deal, given the actual source material); here it
    # re-confirms the post is not classified FREE_DEAL without a stated kind at all.
    validate_free_deal(FreeDealClaim(has_explicit_free_evidence=True))

    kind_label = escape_markdown_v2(_FREE_DEAL_LABEL[content.free_deal_kind])
    headline = f"*{category_emoji(content.category)} {escape_markdown_v2(content.headline)}*"
    kind_line = f"_{kind_label}_"
    text = _assemble(
        headline, kind_line, _body(content), _detail(content), _source_line(content)
    )
    return RenderedPost(
        full_text=text, headline_plain=content.headline, body_chars=_plain_body_chars(content)
    )


# --- AI_LIFEHACK --------------------------------------------------------------------

_LIFEHACK_CAVEAT = "Це досвід конкретного користувача, а не гарантований результат."


def _render_lifehack(content: EditorialContent) -> RenderedPost:
    validate_lifehack(LifehackClaim(evidence=content.evidence, framed_as_report=True))

    caveat = f"⚠️ {escape_markdown_v2(_LIFEHACK_CAVEAT)}"
    text = _assemble(
        _headline(content), _body(content), caveat, _detail(content), _source_line(content)
    )
    return RenderedPost(
        full_text=text, headline_plain=content.headline, body_chars=_plain_body_chars(content)
    )


# --- PROMPT_WORKFLOW -----------------------------------------------------------

_PROMPT_LABEL: dict[PromptOrigin, str] = {
    PromptOrigin.SOURCE_VERBATIM: "Оригінальний промпт",
    PromptOrigin.SOURCE_ADAPTED: "Адаптована версія промпту",
    PromptOrigin.WORKFLOW_DERIVED: "Ідея workflow (не дослівний промпт)",
}


def _render_prompt_workflow(content: EditorialContent) -> RenderedPost:
    assert content.prompt_origin is not None and content.prompt_text  # validator-guaranteed
    presented_as_quote = content.prompt_origin is PromptOrigin.SOURCE_VERBATIM
    validate_prompt_provenance(
        PromptClaim(origin=content.prompt_origin, presented_as_verbatim_quote=presented_as_quote)
    )

    label = escape_markdown_v2(_PROMPT_LABEL[content.prompt_origin])
    prompt_body = escape_markdown_v2(content.prompt_text)
    # Verbatim text alone may be shown inside a quote block (MarkdownV2 ">" — plain,
    # copy-friendly); adapted/derived text is labeled instead, never quoted, per
    # validate_prompt_provenance's own rule.
    if presented_as_quote:
        prompt_block = f"📝 *{label}:*\n>{prompt_body}"
    else:
        prompt_block = f"📝 *{label}:*\n{prompt_body}"

    text = _assemble(
        _headline(content), _body(content), prompt_block, _detail(content), _source_line(content)
    )
    return RenderedPost(
        full_text=text, headline_plain=content.headline, body_chars=_plain_body_chars(content)
    )


# --- RESEARCH --------------------------------------------------------------------

_RESEARCH_LABEL: dict[ResearchClaimFraming, str] = {
    ResearchClaimFraming.PAPER_RESULT: "За даними дослідження",
    ResearchClaimFraming.COMPANY_CLAIM: "За заявою компанії",
    ResearchClaimFraming.INDEPENDENT_VERIFICATION: "Незалежно підтверджено",
}


def _render_research(content: EditorialContent) -> RenderedPost:
    assert content.research_framing is not None  # validator-guaranteed
    validate_research_claim(
        ResearchClaim(
            framing=content.research_framing,
            independently_verified=content.research_independently_verified,
        )
    )

    framing_line = f"_{escape_markdown_v2(_RESEARCH_LABEL[content.research_framing])}_"
    text = _assemble(
        _headline(content), framing_line, _body(content), _detail(content), _source_line(content)
    )
    return RenderedPost(
        full_text=text, headline_plain=content.headline, body_chars=_plain_body_chars(content)
    )


# --- WEEKLY_DIGEST -----------------------------------------------------------------

_DIGIT_EMOJI = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣")


def _render_weekly_digest(content: EditorialContent) -> RenderedPost:
    if not content.digest_items:
        raise RenderError("WEEKLY_DIGEST requires at least one item")

    lines = [_headline(content)]
    body_chars = 0
    for index, item in enumerate(content.digest_items):
        marker = _DIGIT_EMOJI[index] if index < len(_DIGIT_EMOJI) else f"{index + 1}."
        headline = escape_markdown_v2(item.headline)
        summary = escape_markdown_v2(item.summary)
        entry = f"{marker} *{headline}*\n{summary}"
        if item.source_url:
            assert item.source_label is not None  # DigestItem's own validator guarantees this
            label = escape_markdown_v2(item.source_label)
            url = _escape_markdown_v2_url(item.source_url)
            entry += f"\n🔗 [{label}]({url})"
        lines.append(entry)
        body_chars += len(item.summary)

    return RenderedPost(
        full_text="\n\n".join(lines), headline_plain=content.headline, body_chars=body_chars
    )


# --- size / hype warnings ---------------------------------------------------------


def _with_length_warnings(post: RenderedPost, category: EditorialCategory) -> RenderedPost:
    warnings: list[str] = []
    if category not in WIDE_CATEGORIES:
        if post.body_chars > TARGET_BODY_CHARS_MAX:
            warnings.append(
                f"body is {post.body_chars} chars, over the "
                f"{TARGET_BODY_CHARS_MAX}-char ordinary target for {category.value}"
            )
        elif post.body_chars < TARGET_BODY_CHARS_MIN // 3:
            # A post can be legitimately short; only flag the clearly-too-thin case.
            warnings.append(f"body is only {post.body_chars} chars — check it isn't too thin")
    elif post.body_chars > ABSOLUTE_BODY_CHARS_WARNING:
        warnings.append(
            f"body is {post.body_chars} chars, over the "
            f"{ABSOLUTE_BODY_CHARS_WARNING}-char absolute warning threshold"
        )

    lowered = post.full_text.lower()
    for phrase in HYPE_PHRASES:
        if phrase in lowered:
            warnings.append(f"contains a hype phrase the shared safety rules forbid: {phrase!r}")

    if not warnings:
        return post
    return RenderedPost(
        full_text=post.full_text,
        headline_plain=post.headline_plain,
        body_chars=post.body_chars,
        warnings=tuple(warnings),
    )


def render_short_summary(
    content: EditorialContent, *, max_highlight_chars: int | None = None
) -> str:
    """A short, self-contained caption: headline, one highlight, source link.

    Deterministically derived from the same ``content`` record ``render_editorial_post``
    uses — never a second, independently-generated summary that could drift from the
    full post's facts (see ``rendering.caption``, which uses this when the full post
    does not fit a media caption).

    ``max_highlight_chars``, when given, truncates the *raw highlight text* (before
    MarkdownV2 escaping) rather than the assembled string — so headline and source
    always survive intact even in a pathological case where the highlight alone would
    otherwise push the caption over Telegram's limit (see ``rendering.caption``'s
    last-resort truncation, which needs the headline/source to still be there, not
    clipped off by a blind end-of-string cut).
    """
    highlight = content.body[0]
    highlight_text = highlight.text
    if max_highlight_chars is not None and len(highlight_text) > max_highlight_chars:
        highlight_text = highlight_text[: max(0, max_highlight_chars - 1)].rstrip() + "…"
    text = f"{block_emoji(highlight.purpose)} {escape_markdown_v2(highlight_text)}"
    return _assemble(_headline(content), text, _source_line(content))


__all__ = ["RenderError", "RenderedPost", "render_editorial_post", "render_short_summary"]

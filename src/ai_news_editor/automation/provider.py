"""Selecting one story and writing it — the two Gemini calls, and nothing else.

This module plays exactly the role a Claude Code session plays in the human editorial
and writing workflow, for one content type only: read a short list of eligible NEWS
candidates and pick the most important fresh one; then, given the full text of that one
article, write a Ukrainian post that uses only facts the article actually contains.

Every instruction below that forbids inventing something exists because this project's
whole editorial premise is that a reader can trust what the channel tells them. A model
asked to write about a subject it has opinions about will reach for what it already
"knows" the moment the supplied material runs thin — that is not a flaw to patch after
the fact, it is the default behaviour of the thing being asked, and the prompt has to
refuse it explicitly rather than hope confidence scoring catches it afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from ai_news_editor.automation.gemini import GeminiClient, GeminiResponseError, parse_json_object
from ai_news_editor.automation.schema import (
    MAX_HEADLINE_CHARS,
    GeneratedPost,
    SelectionCandidate,
    SelectionResult,
)
from ai_news_editor.observability.logging import get_logger

logger = get_logger(__name__)

#: Below this, this application does not trust the model's own claim that a post is
#: fully grounded in the supplied text — held for review instead of published. Strict
#: on purpose: a single unattended publication is the one action in this pipeline that
#: cannot be quietly undone.
DEFAULT_CONFIDENCE_THRESHOLD = 80

_SELECTION_SYSTEM_INSTRUCTION = """\
You are selecting ONE news item for a Ukrainian Telegram channel about AI, written for
ordinary people rather than engineers.

You will receive a numbered list of candidate stories. Each was published in the last
few hours to a few days by an official AI vendor or platform (OpenAI, Google, Anthropic,
Microsoft, Hugging Face, Notion). Your only job is to pick the single most important,
most recent, most broadly interesting one — the story an ordinary reader would most
want to know about today.

Rules, without exception:
- You may select ONLY one of the candidates you were given, by its exact id.
- You must NOT invent a candidate, a URL, or a story that is not in the list.
- If none of the candidates is worth covering (all trivial, all stale, all unclear),
  return a rejection instead of forcing a selection.
- Do not explain your reasoning at length. One short sentence is enough.

Respond only with JSON matching the given schema.
"""

_GENERATION_SYSTEM_INSTRUCTION = """\
You are writing ONE Ukrainian-language NEWS post for the Telegram channel
@learn_ai_easy, for readers with no assumed AI experience — some have never opened an
AI chat tool. Plain, warm, concrete language. No jargon left unexplained.

You will receive the full text of ONE article from an official source. This article is
the ONLY source of facts you may use. This is an absolute rule, not a style preference:

- Do NOT add any fact, number, date, statistic, or capability that is not stated in the
  supplied article text.
- Do NOT invent a quote. If the article does not contain a quote, do not write one.
- Do NOT use anything you know about this company or product from outside this article.
  Your own background knowledge about AI companies is exactly the kind of thing that
  produces confident, plausible-sounding, wrong sentences — do not reach for it.
- Do NOT change or substitute the source URL. It will be provided to you; you must
  return it back unchanged, and you must never propose a different URL.
- Do NOT write promotional or hype language ("revolutionary", "game-changing",
  "groundbreaking"). Describe what the article says plainly.
- Do NOT draw a conclusion the article itself does not support.

If the article does not contain enough concrete, specific information to write a real
news post — if it is too vague, too short, or mostly marketing language with no actual
news in it — you MUST return a rejection instead of writing something anyway. A
rejection is a normal, successful outcome, not a failure.

The response schema requires every field's key to be present in both cases — a
rejection means setting headline, body, source_url and confidence to null and stating
your reason in rejection_reason; writing a post means the opposite: rejection_reason is
null and the other four are genuinely filled in. Never leave a field's key out, and
never fill headline or body with a placeholder just to satisfy this — null is the
correct value when you are rejecting.

List the specific factual claims your post rests on as short, separate statements, each
one traceable to a sentence in the supplied article. State your own confidence, from 0
to 100, that every sentence in your post is directly supported by the article text —
be honest and conservative; do not report a high number to make the post more likely to
be used.

Style: a short headline; one to three short paragraphs, each separated by a single
blank line (two newlines); plain Ukrainian; no markup of any kind — no bold, no
Markdown, no HTML tags, nothing but plain text and the blank lines between paragraphs.
Formatting (bold, emoji, links) is decided entirely by this application when it renders
your post, never by you. The headline must not exceed {max_headline} characters.

Respond only with JSON matching the given schema.
""".replace("{max_headline}", str(MAX_HEADLINE_CHARS))

_SELECTION_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "OBJECT",
    "properties": {
        "selected_id": {"type": "STRING", "nullable": True},
        "reason": {"type": "STRING", "nullable": True},
        "rejection_reason": {"type": "STRING", "nullable": True},
    },
    "required": [],
}

_GENERATION_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "OBJECT",
    "properties": {
        "content_type": {"type": "STRING", "enum": ["NEWS"]},
        "headline": {"type": "STRING", "nullable": True},
        "body": {"type": "STRING", "nullable": True},
        "source_url": {"type": "STRING", "nullable": True},
        "source_title": {"type": "STRING", "nullable": True},
        "factual_claims": {"type": "ARRAY", "items": {"type": "STRING"}},
        "confidence": {"type": "INTEGER", "nullable": True},
        "rejection_reason": {"type": "STRING", "nullable": True},
    },
    # Every field but content_type is nullable — that part hasn't changed, and it's
    # still what lets a rejection response validly answer with no post content at all.
    # What changed is that all of them are now required too: "required" in JSON Schema
    # means the *key* must be present, entirely independent of "nullable" allowing its
    # *value* to be null — so this closes the exact bug two real GitHub Actions runs
    # hit (Gemini returning syntactically valid JSON that simply omitted the body and
    # confidence keys) without forcing a rejection to carry fake post content, and
    # without needing an untested anyOf/discriminated-union schema shape (Gemini's
    # docs describe two schema dialects across two different endpoints — this project
    # calls generateContent, whose Schema object is the one already proven, by every
    # real call this pipeline has made, to honor `required` and `nullable` exactly as
    # used here; switching schema dialects on unverified footing was the greater risk).
    "required": [
        "content_type", "headline", "body", "source_url", "confidence", "rejection_reason",
    ],
}


class SelectionRejected(Exception):
    """Gemini looked at the candidates and chose none of them. A normal outcome."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SelectionInvalid(Exception):
    """Gemini named an id that was never offered. Treated as a hard stop, not a retry."""


class GenerationRejected(Exception):
    """Gemini declined to write from the supplied article. A normal outcome."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Selection:
    """The one candidate Gemini picked, resolved back to this process's own record."""

    candidate: SelectionCandidate
    reason: str | None


def select_candidate(
    client: GeminiClient, candidates: list[SelectionCandidate]
) -> Selection:
    """Ask Gemini to pick one candidate from the list. Never trusts a URL back.

    Raises:
        SelectionRejected: Gemini declined to pick any of them.
        SelectionInvalid: Gemini's answer did not parse, or named an id that was not
            in the list offered. Both are treated identically — a candidate id is the
            only channel Gemini has to answer through, and anything else it returns is
            not a selection this application can act on.
    """
    if not candidates:
        raise SelectionInvalid("no candidates were offered; nothing to select from")

    by_id = {candidate.id: candidate for candidate in candidates}
    prompt = _render_selection_prompt(candidates)

    try:
        result = client.generate(
            system_instruction=_SELECTION_SYSTEM_INSTRUCTION,
            prompt=prompt,
            response_schema=_SELECTION_RESPONSE_SCHEMA,
        )
        parsed = SelectionResult.model_validate(parse_json_object(result.text))
    except GeminiResponseError as exc:
        raise SelectionInvalid(f"Gemini's selection response was unusable: {exc}") from exc
    except ValidationError as exc:
        # Valid JSON, wrong shape — an ordinary rejection, same as GeminiResponseError
        # above. Deliberately NOT a bare `except Exception`: that would also catch
        # GeminiConfigurationError / GeminiTransientError / GeminiRequestError from the
        # client.generate() call above and misreport a broken API key or an exhausted
        # retry budget as "Gemini declined to select" instead of the loud GEMINI_ERROR
        # run_automation's own except clause exists to surface for exactly those.
        raise SelectionInvalid(
            f"Gemini's selection response did not match the schema: {exc}"
        ) from exc

    if parsed.rejection_reason is not None:
        raise SelectionRejected(parsed.rejection_reason)

    assert parsed.selected_id is not None  # the schema's own validator guarantees this
    candidate = by_id.get(parsed.selected_id)
    if candidate is None:
        # This is exactly the case the module docstring exists to prevent: Gemini named
        # something that was never offered. Never resolved by guessing the closest
        # match — treated as a hard failure of the whole run.
        raise SelectionInvalid(
            f"Gemini selected id {parsed.selected_id!r}, which was not among the "
            f"{len(candidates)} candidates offered"
        )
    return Selection(candidate=candidate, reason=parsed.reason)


def generate_post(
    client: GeminiClient,
    *,
    candidate: SelectionCandidate,
    article_text: str,
    confidence_threshold: int = DEFAULT_CONFIDENCE_THRESHOLD,
) -> GeneratedPost:
    """Ask Gemini to write the post from the full article text of one selected story.

    Raises:
        GenerationRejected: Gemini declined to write from this article, its response
            was not valid JSON or did not match the required shape (a real GitHub
            Actions run is what proved this happens: a live model can return
            syntactically valid JSON that is still missing a required field), its own
            stated confidence fell below ``confidence_threshold``, or its echoed
            source URL does not match the candidate this generation call was for.
    """
    prompt = _render_generation_prompt(candidate, article_text)
    try:
        result = client.generate(
            system_instruction=_GENERATION_SYSTEM_INSTRUCTION,
            prompt=prompt,
            response_schema=_GENERATION_RESPONSE_SCHEMA,
        )
        post = GeneratedPost.model_validate(parse_json_object(result.text))
    except GeminiResponseError as exc:
        raise GenerationRejected(f"Gemini's generation response was unusable: {exc}") from exc
    except ValidationError as exc:
        # Valid JSON, wrong shape — an ordinary rejection, same as GeminiResponseError
        # above, and the same reasoning select_candidate() already applies: deliberately
        # NOT a bare `except Exception`, which would also catch GeminiConfigurationError
        # / GeminiTransientError / GeminiRequestError from the client.generate() call
        # above and misreport a broken API key or an exhausted retry budget as "Gemini
        # declined to write" instead of the loud GEMINI_ERROR run_automation's own
        # except clause (around the whole select-then-generate block) exists to surface
        # for exactly those. Previously uncaught here — a real GitHub Actions run
        # crashed the whole process with a raw pydantic.ValidationError the first time a
        # live model response was missing a required field, instead of the quiet
        # GENERATION_REJECTED this is supposed to be.
        raise GenerationRejected(
            f"Gemini's generation response did not match the schema: {exc}"
        ) from exc

    if post.is_rejection:
        raise GenerationRejected(post.rejection_reason or "Gemini declined without a reason")

    # Grounding checks. Both are conditions the schema cannot express on its own, and
    # both are treated as rejections rather than corrected — silently substituting the
    # right URL back in would hide that the model drifted from the source it was given.
    if post.source_url != candidate.url:
        raise GenerationRejected(
            f"generated source_url {post.source_url!r} does not match the selected "
            f"article's URL {candidate.url!r}"
        )
    assert post.confidence is not None  # the schema's own validator guarantees this
    if post.confidence < confidence_threshold:
        raise GenerationRejected(
            f"confidence {post.confidence} is below the required {confidence_threshold}"
        )

    logger.info(
        "gemini generated a post",
        extra={
            "url": candidate.url,
            "confidence": post.confidence,
            "claim_count": len(post.factual_claims),
        },
    )
    return post


def _render_selection_prompt(candidates: list[SelectionCandidate]) -> str:
    lines = ["Candidates:\n"]
    for candidate in candidates:
        lines.append(f"id: {candidate.id}")
        lines.append(f"source: {candidate.source_name}")
        lines.append(f"title: {candidate.title}")
        if candidate.published_at:
            lines.append(f"published_at: {candidate.published_at}")
        if candidate.summary:
            lines.append(f"summary: {candidate.summary}")
        lines.append("")
    return "\n".join(lines)


def _render_generation_prompt(candidate: SelectionCandidate, article_text: str) -> str:
    return (
        f"source: {candidate.source_name}\n"
        f"title: {candidate.title}\n"
        f"published_at: {candidate.published_at or 'unknown'}\n"
        f"url: {candidate.url}\n"
        "\n"
        "Full article text (the only material you may use):\n"
        f"{article_text}\n"
    )

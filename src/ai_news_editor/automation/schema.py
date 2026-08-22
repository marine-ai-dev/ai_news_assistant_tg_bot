"""What Gemini is asked for, in both steps, and nothing more than that.

Two calls, two schemas, the same discipline behind each: Gemini receives only what a
human editor doing the same two jobs would need, and it returns a small structured
document that this application checks before trusting a single word of it.

**Selection never trusts a URL Gemini types back.** :class:`SelectionCandidate` carries
a short id (``"1"``, ``"2"`` …) assigned by this application, not derived from anything
in the model's response. :class:`SelectionResult` names the *id* it picked; the actual
URL is looked up from the candidate list this process built, never from the model's own
copy of it. A model that reproduces a URL slightly wrong — a trailing slash, a stray
encoding — can therefore never select something it was not actually offered, because it
is never asked to reproduce a URL at all.

**Generation is checked, not trusted.** :class:`GeneratedPost` still carries the source
URL and title Gemini believes it used, but only as a grounding signal to cross-check
against the real candidate — the values actually published always come from this
application's own record of the selected article (see ``automation.provider``), never
from the model's echo of them. A mismatch here is treated as a sign the generation drifted
from the source and is rejected, not silently corrected.

Every model here is ``extra="forbid"``, matching the same discipline the editorial and
writing exchange contracts already use: an unexpected field from the model is a bug in
the prompt or the model's understanding of it, not something to tolerate.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_news_editor.domain.enums import EditorialCategory, EditorialEvidence

NonEmpty = Annotated[str, Field(min_length=1, max_length=4000)]

#: How many claims a generated post may cite. Enough for a real news item; a number this
#: small also keeps a hallucinated post from padding itself with invented specifics.
MAX_FACTUAL_CLAIMS = 8

#: How many candidates the selection prompt offers in one call. Bounded and deterministic
#: — this is not "send everything and hope the model finds the best one".
MAX_SELECTION_CANDIDATES = 15

#: Bot API's own limit, imposed again here so a request that could never fit is rejected
#: before it is even sent, not after Gemini has spent tokens producing it. The canonical
#: check still runs later against the actual rendered text in writing.format.
MAX_HEADLINE_CHARS = 200


class StrictModel(BaseModel):
    """Unknown fields are a bug in the prompt or the model's understanding of it."""

    model_config = ConfigDict(extra="forbid")


# --- outbound: selection ------------------------------------------------------------


class SelectionCandidate(StrictModel):
    """One eligible NEWS candidate, as offered to Gemini for selection.

    Deliberately thin. ``id`` is a short position marker this application assigns —
    Gemini names it back rather than a URL, and the URL is resolved from this record,
    never from the model's response.
    """

    id: str
    source_name: str
    title: str
    #: ISO 8601, or ``None`` when the source did not supply one.
    published_at: str | None = None
    url: str
    #: The RSS/changelog excerpt already collected — short by construction, and never
    #: the full article; selection decides *which* story, not what to write about it.
    summary: str | None = None


class SelectionRequest(StrictModel):
    """The whole selection call in one document, for logging and testing."""

    candidates: tuple[SelectionCandidate, ...] = Field(
        min_length=1, max_length=MAX_SELECTION_CANDIDATES
    )


class SelectionResult(StrictModel):
    """Gemini's answer: one candidate id, or an explicit rejection of the whole batch."""

    selected_id: str | None = None
    reason: str | None = None
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> Self:
        if (self.selected_id is None) == (self.rejection_reason is None):
            raise ValueError(
                "a selection must name exactly one of selected_id or rejection_reason, "
                "never both and never neither"
            )
        return self


# --- outbound: generation ------------------------------------------------------------


class GenerationRequest(StrictModel):
    """The whole generation call, for logging and testing."""

    source_name: str
    title: str
    published_at: str | None = None
    url: str
    #: The full article text fetched by ``sources.fulltext`` — the only material Gemini
    #: is allowed to draw facts from. Never a search result, never model knowledge.
    article_text: str


class GeneratedPost(StrictModel):
    """One Ukrainian NEWS post, or an explicit refusal to write one.

    ``content_type`` is fixed to the single literal value ``"NEWS"`` rather than merely
    checked against it — the automated pipeline can produce no other content type by
    construction, which is a stronger guarantee than a runtime comparison.
    """

    content_type: Literal["NEWS"] = "NEWS"
    headline: NonEmpty | None = Field(default=None, max_length=MAX_HEADLINE_CHARS)
    body: NonEmpty | None = None
    #: Gemini's own belief about what it used. Compared against the real candidate by
    #: the caller; never itself the value that gets published. See the module docstring.
    source_url: str | None = None
    source_title: str | None = None
    #: Short, explicit statements the post rests on, each traceable to the source text.
    #: Not independently verified word-for-word — that is a human's job on review — but
    #: asking for them explicitly is itself a check: a model forced to enumerate its
    #: claims cannot pad a thin story with vague unsupported color.
    factual_claims: tuple[NonEmpty, ...] = Field(default=(), max_length=MAX_FACTUAL_CLAIMS)
    #: The model's own stated confidence that the post is fully grounded in the supplied
    #: text. Compared against a configured threshold; never overridden upward by this
    #: application.
    confidence: Annotated[int, Field(ge=0, le=100)] | None = None
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def _post_or_rejection(self) -> Self:
        have_post = self.headline is not None or self.body is not None
        if self.rejection_reason is not None and have_post:
            raise ValueError("a rejection must not also carry post content")
        if self.rejection_reason is None:
            missing = [
                name
                for name, value in (
                    ("headline", self.headline),
                    ("body", self.body),
                    ("source_url", self.source_url),
                    ("confidence", self.confidence),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    f"a post that is not a rejection must supply {', '.join(missing)}"
                )
        return self

    @property
    def is_rejection(self) -> bool:
        return self.rejection_reason is not None


# --- outbound: editorial classification (Step 3, additive) --------------------------


class ClassificationResult(StrictModel):
    """Gemini's editorial classification of one candidate: its content type and the
    strength of evidence behind it — both constrained to the exact enum members of
    :class:`~domain.enums.EditorialCategory` / :class:`~domain.enums.EditorialEvidence`
    by pydantic *and* by the response schema in :mod:`automation.classification`, so
    Gemini cannot invent an arbitrary type string even if it tried.

    Additive to the two schemas above: nothing in :func:`automation.provider
    .select_candidate` or ``generate_post`` reads this model, and this model is never
    produced by those calls. See :mod:`automation.classification` for the (also new,
    also unwired) call that produces one.
    """

    content_type: EditorialCategory | None = None
    evidence_type: EditorialEvidence | None = None
    reason: NonEmpty | None = None
    rejection_reason: NonEmpty | None = None
    #: Step 6B: answered on every classification, independent of the classify/reject
    #: outcome above — this is the model's own judgment of whether the candidate's
    #: primary content is speculative doom/dystopian futurism (an "AI apocalypse"
    #: narrative, a hypothetical "AI-run state") rather than concrete present-day
    #: reporting. See :func:`automation.classification.classify_candidate` for the
    #: deterministic local rejection this drives — a `True` here is rejected
    #: unconditionally, never overridden by a `content_type` the model also filled in.
    is_speculative_doom: bool = False
    #: Step 6C: answered on every classification, same discipline as
    #: ``is_speculative_doom`` above — whether the *story itself* (not its source) is
    #: substantially about Russia, Belarus or Iran (their AI development, research,
    #: companies, products, policy, statistics). This is a content/subject check, not
    #: the source-origin check ``sources.geography`` already does — a US/EU/UK/UA
    #: outlet *reporting on* one of those countries still answers `True` here and is
    #: still rejected, because the rule is about the story's focus, not who wrote it.
    is_about_forbidden_geography: bool = False

    # -- AI News Agent v3 priority step: AI-first relevance + hard editorial filters --
    #
    # Answered on every classification, same discipline as the two flags above.
    # ``False`` is the fail-closed default for every one of these — a classification
    # response that omits a key (or a candidate the model genuinely could not judge)
    # never accidentally reads as "passed" a check it never actually answered See
    # automation.eligibility.evaluate_eligibility for the deterministic code-level
    # decision these feed — that decision, not this prompt-answered data, is what
    # actually blocks a candidate from reaching generation/publication.
    #
    #: True only if AI itself is one of the story's primary subjects — the core v3
    #: rule: if removing the AI component would leave essentially the same story, this
    #: must be answered False.
    is_ai_primary: bool = False
    #: True if this is primarily a political story — elections, party politics,
    #: political disputes/statements, geopolitical disputes.
    is_political: bool = False
    #: True if this is primarily about war, armed conflict, combat operations, or
    #: military operations.
    is_war_or_conflict: bool = False
    #: True if this is primarily about military AI/battlefield AI, military drones,
    #: targeting systems, autonomous weapons, military robotics or surveillance.
    is_miltech: bool = False
    #: True if this is primarily about defence-industry/defence-technology — including
    #: a defence startup, defence procurement tech, or a battlefield-intelligence
    #: platform. A "startup" or "AI company" framing never exempts a story from this.
    is_deftech: bool = False
    #: True if this is primarily a cybersecurity story — a hack, breach, cyberattack,
    #: vulnerability, malware, ransomware, phishing campaign, threat actor, or security
    #: incident — rather than an AI capability or product in its own right.
    is_cybersecurity: bool = False
    #: True if this is generic government/state news where AI is not clearly the main
    #: practical subject — a minister's or president's statement that merely mentions
    #: AI, a generic public-sector digitalisation story. False for a genuinely
    #: AI-specific product/policy story whose direct subject is AI.
    is_generic_government_news: bool = False
    #: True if this is ordinary developer tooling/cloud/database/infrastructure/SaaS/
    #: software-engineering news where AI is only an incidental feature, not the
    #: material subject of the release.
    is_generic_devtech: bool = False

    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> Self:
        if (self.content_type is None) != (self.evidence_type is None):
            raise ValueError("content_type and evidence_type must be set together, or not at all")
        classified = self.content_type is not None
        rejected = self.rejection_reason is not None
        if classified == rejected:
            raise ValueError(
                "a classification must set content_type/evidence_type, or "
                "rejection_reason, never both and never neither"
            )
        return self


__all__ = [
    "MAX_FACTUAL_CLAIMS",
    "MAX_HEADLINE_CHARS",
    "MAX_SELECTION_CANDIDATES",
    "ClassificationResult",
    "GeneratedPost",
    "GenerationRequest",
    "SelectionCandidate",
    "SelectionRequest",
    "SelectionResult",
]

"""Recent-publication reader — Step 3 (AI News Agent v2), section 8.

Bridges real production data into ``editorial.diversity.rank()``'s ``recent``
parameter: walks ``Publication`` (most recent first) -> ``Draft`` -> ``Article`` ->
source registry, and the ``Evaluation`` that authorised the draft, to recover each
published post's editorial category and source family.

Lives in ``planning/``, not ``editorial/``, on purpose: it reads ``Draft`` rows, and
the ``editorial`` package is forbidden by a safety test
(``test_layer_does_not_touch_drafts_or_review_decisions``) from importing
``DraftRepository`` at all — that boundary is what keeps evaluation-time code from
drifting into publishing internals. ``planning`` is the one package already documented
as "reads approved drafts... read-only, nothing here writes a row," which is exactly
this module's contract too. ``editorial.diversity`` itself stays a pure function of
already-in-memory data and never imports this module.
"""

from __future__ import annotations

from ai_news_editor.domain.enums import EditorialCategory, PublicationStatus
from ai_news_editor.domain.errors import ConfigurationError
from ai_news_editor.editorial.diversity import RecentPost
from ai_news_editor.sources.config import SourcesConfig
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    DraftRepository,
    EvaluationRepository,
    PublicationRepository,
)


def recent_history(
    *,
    publications: PublicationRepository,
    drafts: DraftRepository,
    articles: ArticleRepository,
    evaluations: EvaluationRepository,
    sources: SourcesConfig,
    limit: int = 10,
    channel: str | None = None,
) -> list[RecentPost]:
    """The last ``limit`` successful publications, newest first, as ``RecentPost``.

    ``channel``, when given, restricts this to publications sent to that one channel —
    e.g. a TEST-channel soak or dispatch should never see (or be scored against) a
    production post's diversity history, and vice versa.

    Every article-derived (NEWS) draft published to date carries no
    ``editorial_category`` on its evaluation yet — migration 014's own documented
    rule is that a missing value means NEWS, since that is the only category any
    automated evaluation has ever produced so far. A draft with no ``article_id``
    (editorial-original content) or whose evaluation cannot be resolved is skipped
    rather than guessed at.
    """
    posts: list[RecentPost] = []
    for publication in publications.list_recent(limit=limit * 4):
        if publication.status != PublicationStatus.SUCCEEDED:
            continue
        if channel is not None and publication.channel != channel:
            continue
        if len(posts) >= limit:
            break

        draft = drafts.get(publication.draft_id)
        if draft.article_id is None:
            continue

        article = articles.get(draft.article_id)
        try:
            source_family = sources.get(article.source_id).source_family
        except ConfigurationError:
            source_family = None

        evaluation = evaluations.latest_for_article(article.id)
        category = (
            evaluation.editorial_category
            if evaluation is not None and evaluation.editorial_category is not None
            else EditorialCategory.NEWS
        )

        posts.append(RecentPost(editorial_category=category, source_family=source_family))

    return posts

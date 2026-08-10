"""Explicit per-entity repositories. Domain objects in, domain objects out."""

from ai_news_editor.storage.repositories.articles import ArticleRepository
from ai_news_editor.storage.repositories.drafts import DraftRepository
from ai_news_editor.storage.repositories.raw_items import RawItemRepository
from ai_news_editor.storage.repositories.review_decisions import ReviewDecisionRepository
from ai_news_editor.storage.repositories.source_fetch_state import SourceFetchStateRepository
from ai_news_editor.storage.repositories.sources import SourceRepository

__all__ = [
    "ArticleRepository",
    "DraftRepository",
    "RawItemRepository",
    "ReviewDecisionRepository",
    "SourceFetchStateRepository",
    "SourceRepository",
]

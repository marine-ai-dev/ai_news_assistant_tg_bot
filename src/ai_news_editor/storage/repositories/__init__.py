"""Explicit per-entity repositories. Domain objects in, domain objects out."""

from ai_news_editor.storage.repositories.articles import ArticleRepository
from ai_news_editor.storage.repositories.community_signals import CommunitySignalRepository
from ai_news_editor.storage.repositories.content_items import ContentItemRepository
from ai_news_editor.storage.repositories.drafts import DraftRepository
from ai_news_editor.storage.repositories.evaluations import EvaluationRepository
from ai_news_editor.storage.repositories.publications import PublicationRepository
from ai_news_editor.storage.repositories.raw_items import RawItemRepository
from ai_news_editor.storage.repositories.review_decisions import ReviewDecisionRepository
from ai_news_editor.storage.repositories.source_fetch_state import SourceFetchStateRepository
from ai_news_editor.storage.repositories.sources import SourceRepository

__all__ = [
    "ArticleRepository",
    "CommunitySignalRepository",
    "ContentItemRepository",
    "DraftRepository",
    "EvaluationRepository",
    "PublicationRepository",
    "RawItemRepository",
    "ReviewDecisionRepository",
    "SourceFetchStateRepository",
    "SourceRepository",
]

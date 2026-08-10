"""Typed error hierarchy.

Split by *what the caller should do*: retryable problems are transient and worth
another attempt, fatal problems mean the run cannot continue correctly.
"""

from __future__ import annotations


class AiNewsError(Exception):
    """Base class for every error raised by this application."""


class RetryableError(AiNewsError):
    """Transient failure; the same operation may succeed if attempted again."""


class FatalError(AiNewsError):
    """Unrecoverable failure; the run must stop rather than continue incorrectly."""


class ConfigurationError(FatalError):
    """Settings are missing, malformed, or forbid the requested operation."""


class MigrationError(FatalError):
    """The database schema could not be brought to the expected state."""


class RepositoryError(AiNewsError):
    """A persistence operation failed or violated an invariant."""


class EntityNotFoundError(RepositoryError):
    """A lookup by primary key found nothing."""


class IllegalStateTransition(AiNewsError):
    """A lifecycle transition was attempted that the state machine forbids."""

    def __init__(self, entity: str, current: str, target: str) -> None:
        super().__init__(f"{entity}: {current} -> {target} is not an allowed transition")
        self.entity = entity
        self.current = current
        self.target = target


class ApprovalError(AiNewsError):
    """Base class for failures of the human approval gate."""


class NotApprovedError(ApprovalError):
    """Publication was attempted for content that no human approved."""


class ApprovalInvalidatedError(ApprovalError):
    """The approved content changed after approval, so the approval no longer applies."""


class UnauthorizedConstructionError(ApprovalError):
    """A publication authorization was constructed outside the approval gate.

    Raised to make forging an authorization a loud failure rather than a silent
    security hole. See :mod:`ai_news_editor.domain.authorization`.
    """


class PublicationError(AiNewsError):
    """Base class for failures of the publication path."""


class PublicationAlreadyExistsError(PublicationError):
    """This exact draft version was already published to this destination.

    Not a failure of the send — a refusal to make a second one.
    """


class PublicationOutcomeUncertainError(PublicationError):
    """The request may or may not have reached the channel.

    Raised when the local side lost the response. Retrying could duplicate a real post,
    so the attempt is recorded for a human to resolve and nothing is retried.
    """


class TelegramError(PublicationError):
    """Base class for failures reported by, or against, the Telegram Bot API."""


class TelegramAuthenticationError(TelegramError, FatalError):
    """The bot token is missing, malformed or rejected. Never retried."""


class TelegramPermissionError(TelegramError, FatalError):
    """The bot may not post to the destination. Never retried."""


class TelegramDestinationError(TelegramError, FatalError):
    """The configured destination does not exist or is not a postable chat."""


class TelegramContentError(TelegramError, FatalError):
    """Telegram refused the message text itself: too long, or bad markup."""


class TelegramRateLimitError(TelegramError, RetryableError):
    """Too many requests. Carries the server's own ``retry_after`` when given."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TelegramApiError(TelegramError, RetryableError):
    """Any other Telegram failure, including malformed responses."""

    def __init__(self, message: str, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code

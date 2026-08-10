"""Loading and strict validation of ``config/sources.yaml``.

Configuration is human-edited, so it is validated hard and fails with a message that
names the offending source. A typo should stop the run, not quietly disable a feed.

The file shape is deliberately adapter-agnostic: ``adapter`` selects the implementation
and ``options`` carries whatever that adapter needs. Future kinds (HTML changelogs, an
HN signal reader, JSON APIs) slot in without reshaping the file, and none of them are
forced through an RSS-shaped abstraction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Self

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from ai_news_editor.domain.enums import SourceKind, TrustTier
from ai_news_editor.domain.errors import ConfigurationError
from ai_news_editor.domain.models import Source
from ai_news_editor.sources.base import DEFAULT_MAX_ITEMS
from ai_news_editor.sources.http import DEFAULT_TIMEOUT_SECONDS, UnsafeUrlError, validate_url

DEFAULT_CONFIG_PATH = Path("config/sources.yaml")
SUPPORTED_CONFIG_VERSION = 1

SourceId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_]*$", min_length=2, max_length=64)]


def _upper(value: object) -> object:
    """Let YAML use natural lowercase (``adapter: rss``) for uppercase domain enums."""
    return value.upper() if isinstance(value, str) else value


AdapterKind = Annotated[SourceKind, BeforeValidator(_upper)]
ConfiguredTrustTier = Annotated[TrustTier, BeforeValidator(_upper)]


class FetchDefaults(BaseModel):
    """Request settings applied to every source unless overridden."""

    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0, le=120)
    max_items_per_fetch: int = Field(default=DEFAULT_MAX_ITEMS, ge=1, le=500)
    poll_interval_minutes: int = Field(default=60, ge=1)


class SourceDefinition(BaseModel):
    """One configured source, as written in YAML."""

    model_config = ConfigDict(extra="forbid")

    id: SourceId
    name: str = Field(min_length=1)
    enabled: bool = True
    adapter: AdapterKind
    url: str = Field(min_length=1)
    trust_tier: ConfiguredTrustTier
    editorial_role: str = Field(
        min_length=1,
        description="Why this source is in the mix. Required: an unexplained source is "
        "a source nobody can evaluate later.",
    )
    publisher: str | None = None
    language: str = "en"
    signal_only: bool = False
    tags: tuple[str, ...] = ()
    max_items_per_fetch: int | None = Field(default=None, ge=1, le=500)
    poll_interval_minutes: int | None = Field(default=None, ge=1)
    #: Adapter-specific settings. Empty for RSS; future kinds will use it. No secrets
    #: belong here — this file is committed.
    options: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _url_is_fetchable(self) -> Self:
        try:
            validate_url(self.url)
        except UnsafeUrlError as exc:
            raise ValueError(str(exc)) from exc
        return self

    def to_source(self, defaults: FetchDefaults) -> Source:
        """Convert to the persisted domain entity."""
        return Source(
            id=self.id,
            name=self.name,
            kind=self.adapter,
            url=self.url,
            trust_tier=self.trust_tier,
            signal_only=self.signal_only,
            enabled=self.enabled,
            language=self.language,
            publisher=self.publisher,
            poll_interval_minutes=self.poll_interval_minutes or defaults.poll_interval_minutes,
            editorial_role=self.editorial_role,
            tags=self.tags,
            config=dict(self.options),
        )

    def max_items(self, defaults: FetchDefaults) -> int:
        return self.max_items_per_fetch or defaults.max_items_per_fetch


class SourcesConfig(BaseModel):
    """The whole ``sources.yaml`` document."""

    model_config = ConfigDict(extra="forbid")

    version: int = SUPPORTED_CONFIG_VERSION
    defaults: FetchDefaults = Field(default_factory=FetchDefaults)
    sources: list[SourceDefinition]

    @model_validator(mode="after")
    def _validate_document(self) -> Self:
        if self.version != SUPPORTED_CONFIG_VERSION:
            raise ValueError(
                f"unsupported config version {self.version}; expected {SUPPORTED_CONFIG_VERSION}"
            )
        if not self.sources:
            raise ValueError("at least one source must be configured")

        seen: set[str] = set()
        for definition in self.sources:
            if definition.id in seen:
                raise ValueError(f"duplicate source id: {definition.id!r}")
            seen.add(definition.id)
        return self

    def enabled(self) -> list[SourceDefinition]:
        return [definition for definition in self.sources if definition.enabled]

    def get(self, source_id: str) -> SourceDefinition:
        for definition in self.sources:
            if definition.id == source_id:
                return definition
        known = ", ".join(sorted(d.id for d in self.sources))
        raise ConfigurationError(f"unknown source id {source_id!r}; configured: {known}")


def load_sources_config(path: Path = DEFAULT_CONFIG_PATH) -> SourcesConfig:
    """Read and validate the source configuration.

    Raises:
        ConfigurationError: the file is missing, unreadable, not a mapping, or invalid.
    """
    if not path.exists():
        raise ConfigurationError(f"source configuration not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{path} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigurationError(f"could not read {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path} must contain a YAML mapping at the top level")

    try:
        return SourcesConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigurationError(f"invalid source configuration in {path}: {exc}") from exc

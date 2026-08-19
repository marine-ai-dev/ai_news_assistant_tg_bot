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
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from ai_news_editor.domain.enums import (
    ContentCapability,
    FulltextPolicy,
    MediaPolicy,
    PublisherRegion,
    SourceKind,
    SourcePriority,
    TrustTier,
)
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


def _upper_seq(value: object) -> object:
    """The list form of :func:`_upper`, for tuple-of-enum fields."""
    if isinstance(value, list):
        return [_upper(item) for item in value]
    return value


AdapterKind = Annotated[SourceKind, BeforeValidator(_upper)]
ConfiguredTrustTier = Annotated[TrustTier, BeforeValidator(_upper)]
ConfiguredPriority = Annotated[SourcePriority, BeforeValidator(_upper)]
ConfiguredMediaPolicy = Annotated[MediaPolicy, BeforeValidator(_upper)]
ConfiguredFulltextPolicy = Annotated[FulltextPolicy, BeforeValidator(_upper)]
ConfiguredContentCapabilities = Annotated[
    tuple[ContentCapability, ...], BeforeValidator(_upper_seq)
]
ConfiguredPublisherRegion = Annotated[PublisherRegion, BeforeValidator(_upper)]


class FetchDefaults(BaseModel):
    """Request settings applied to every source unless overridden."""

    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0, le=120)
    max_items_per_fetch: int = Field(default=DEFAULT_MAX_ITEMS, ge=1, le=500)
    poll_interval_minutes: int = Field(default=60, ge=1)


#: Cosmetic labels only — TrustTier stays the one canonical enum. Used by the
#: source-health diagnostic and docs/sources.md, never by editorial/collection logic.
TIER_LABELS: dict[TrustTier, str] = {
    TrustTier.OFFICIAL: "TIER_A_PRIMARY",
    TrustTier.REPUTABLE_SECONDARY: "TIER_B_DISCOVERY",
    TrustTier.COMMUNITY_SIGNAL: "TIER_C_COMMUNITY",
    TrustTier.UNVERIFIED: "UNVERIFIED",
}


class SourceDefinition(BaseModel):
    """One configured source, as written in YAML.

    Fields below the adapter-mechanics ones (``id`` through ``options``, unchanged
    since Phase 1) are the v2 registry additions: metadata for a future editorial
    classifier, diversity pass and media policy — none of it read by the current
    NEWS-only automation pipeline, which still only ever asks ``trust_tier``. See
    docs/sources.md.
    """

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

    # -- v2 registry metadata (Step 2) --------------------------------------------
    #
    # None of this is mirrored into the persisted `sources` DB table (Source.to_source
    # below is unchanged) — it lives only in this config-loading layer, read fresh from
    # YAML by whatever later phase needs it. That is what keeps this step migration-free.

    #: Coarse ranking for a future diversity/ranking pass. Required, not defaulted: a
    #: source's priority is an editorial judgement call, not a safe guess.
    priority: ConfiguredPriority
    #: What this source may eventually contribute to, editorially. At least one.
    content_types: ConfiguredContentCapabilities = Field(min_length=1)
    media_policy: ConfiguredMediaPolicy = MediaPolicy.NO_MEDIA
    fulltext_policy: ConfiguredFulltextPolicy = FulltextPolicy.NORMAL_ATTEMPT
    #: Groups sibling feeds from the same company for diversity purposes (e.g. Google AI
    #: Blog + Google DeepMind + Google Research share "Google") — never inferred, so
    #: unrelated companies are never accidentally collapsed together.
    source_family: str | None = None
    #: Explicit hostnames this source's canonical URLs use, when that differs from (or
    #: exceeds) the feed URL's own host. Most sources leave this empty and get one
    #: domain derived from `url` automatically — see `canonical_domains`.
    domains: tuple[str, ...] = ()
    #: Required exactly when `enabled` is False — why this source isn't live yet, so a
    #: disabled entry documents itself instead of looking like an oversight.
    disabled_reason: str | None = Field(default=None, min_length=1)
    #: Step 6B: where this source's publisher is actually headquartered/edited —
    #: required, never defaulted, and never inferred from the feed's TLD or domain.
    #: See ``sources.geography`` for allowlist enforcement; ``PublisherRegion.UNKNOWN``
    #: is a valid, explicit value for a not-yet-reviewed source, which still fails the
    #: allowlist check until it is reviewed.
    publisher_region: ConfiguredPublisherRegion

    @model_validator(mode="after")
    def _url_is_fetchable(self) -> Self:
        try:
            validate_url(self.url)
        except UnsafeUrlError as exc:
            raise ValueError(str(exc)) from exc
        return self

    @model_validator(mode="after")
    def _tier_and_priority_agree(self) -> Self:
        """The check this exists for: a Tier C source cannot outrank Tier A/B ones by
        quietly carrying a PRIMARY priority, and priority COMMUNITY is reserved for
        genuinely COMMUNITY_SIGNAL sources — the two fields must always agree."""
        is_community_tier = self.trust_tier is TrustTier.COMMUNITY_SIGNAL
        is_community_priority = self.priority is SourcePriority.COMMUNITY
        if is_community_tier != is_community_priority:
            raise ValueError(
                f"{self.id}: trust_tier {self.trust_tier.value} and priority "
                f"{self.priority.value} disagree about whether this is a community "
                "source — COMMUNITY_SIGNAL must pair with priority COMMUNITY, and "
                "priority COMMUNITY is reserved for COMMUNITY_SIGNAL sources"
            )
        return self

    @model_validator(mode="after")
    def _disabled_sources_document_why(self) -> Self:
        if not self.enabled and not self.disabled_reason:
            raise ValueError(f"{self.id}: a disabled source must set disabled_reason")
        if self.enabled and self.disabled_reason:
            raise ValueError(
                f"{self.id}: an enabled source should not carry a disabled_reason"
            )
        return self

    @property
    def canonical_domains(self) -> tuple[str, ...]:
        """Normalized hostnames this source is grouped/cooled-down by.

        Explicit `domains` wins when given; otherwise derived from `url` the same way
        automation.pipeline._domain_of normalizes a candidate's URL, so registry
        metadata and runtime domain-cooldown logic never disagree about what "the
        domain" of a source is.
        """
        if self.domains:
            return tuple(d.removeprefix("www.").lower() for d in self.domains)
        host = urlsplit(self.url).hostname or ""
        return (host.removeprefix("www.").lower(),) if host else ()

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
        seen_urls: dict[str, str] = {}
        for definition in self.sources:
            if definition.id in seen:
                raise ValueError(f"duplicate source id: {definition.id!r}")
            seen.add(definition.id)

            existing_id = seen_urls.get(definition.url)
            if existing_id is not None:
                raise ValueError(
                    f"duplicate feed url {definition.url!r}: used by both "
                    f"{existing_id!r} and {definition.id!r}"
                )
            seen_urls[definition.url] = definition.id
        return self

    def enabled(self) -> list[SourceDefinition]:
        return [definition for definition in self.sources if definition.enabled]

    def disabled(self) -> list[SourceDefinition]:
        return [definition for definition in self.sources if not definition.enabled]

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

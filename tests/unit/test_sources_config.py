"""Source configuration loading and strict validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_news_editor.domain.enums import (
    ContentCapability,
    MediaPolicy,
    SourceKind,
    SourcePriority,
    TrustTier,
)
from ai_news_editor.domain.errors import ConfigurationError
from ai_news_editor.sources.config import (
    DEFAULT_CONFIG_PATH,
    FetchDefaults,
    SourcesConfig,
    load_sources_config,
)

MINIMAL = """
version: 1
sources:
  - id: example_feed
    name: Example Feed
    adapter: rss
    url: https://example.invalid/feed.xml
    trust_tier: OFFICIAL
    editorial_role: Test source.
    priority: PRIMARY_NORMAL
    content_types: [NEWS]
    publisher_region: UNITED_STATES
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestLoading:
    def test_loads_a_minimal_document(self, tmp_path: Path) -> None:
        config = load_sources_config(write(tmp_path, MINIMAL))
        assert len(config.sources) == 1
        assert config.sources[0].id == "example_feed"

    def test_lowercase_adapter_maps_to_the_enum(self, tmp_path: Path) -> None:
        """YAML reads naturally in lowercase; the domain vocabulary stays uppercase."""
        config = load_sources_config(write(tmp_path, MINIMAL))
        assert config.sources[0].adapter is SourceKind.RSS

    def test_defaults_are_applied(self, tmp_path: Path) -> None:
        config = load_sources_config(write(tmp_path, MINIMAL))
        assert config.defaults.max_items_per_fetch > 0
        assert config.defaults.timeout_seconds > 0

    def test_per_source_override_wins(self, tmp_path: Path) -> None:
        text = MINIMAL + "    max_items_per_fetch: 7\n"
        config = load_sources_config(write(tmp_path, text))
        assert config.sources[0].max_items(config.defaults) == 7

    def test_falls_back_to_the_default_cap(self, tmp_path: Path) -> None:
        config = load_sources_config(write(tmp_path, MINIMAL))
        expected = config.defaults.max_items_per_fetch
        assert config.sources[0].max_items(config.defaults) == expected


class TestValidationFailures:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            load_sources_config(tmp_path / "absent.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not valid YAML"):
            load_sources_config(write(tmp_path, "sources: [unclosed\n"))

    def test_top_level_must_be_a_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="mapping"):
            load_sources_config(write(tmp_path, "- just\n- a\n- list\n"))

    def test_unknown_field_is_rejected(self, tmp_path: Path) -> None:
        """A typo must stop the run, not silently do nothing."""
        with pytest.raises(ConfigurationError, match="invalid source configuration"):
            load_sources_config(write(tmp_path, MINIMAL + "    trust_teir: OFFICIAL\n"))

    def test_unknown_adapter_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL.replace("adapter: rss", "adapter: telepathy")
        with pytest.raises(ConfigurationError):
            load_sources_config(write(tmp_path, text))

    def test_missing_editorial_role_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL.replace("    editorial_role: Test source.\n", "")
        with pytest.raises(ConfigurationError):
            load_sources_config(write(tmp_path, text))

    def test_duplicate_ids_are_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="duplicate source id"):
            load_sources_config(write(tmp_path, MINIMAL + MINIMAL.split("sources:")[1]))

    def test_empty_source_list_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="at least one source"):
            load_sources_config(write(tmp_path, "version: 1\nsources: []\n"))

    def test_unsupported_version_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="unsupported config version"):
            load_sources_config(write(tmp_path, MINIMAL.replace("version: 1", "version: 99")))

    @pytest.mark.parametrize(
        "url",
        ["file:///etc/passwd", "http://127.0.0.1/feed", "ftp://example.invalid/f"],
    )
    def test_unsafe_urls_are_rejected_at_config_time(self, tmp_path: Path, url: str) -> None:
        text = MINIMAL.replace("https://example.invalid/feed.xml", url)
        with pytest.raises(ConfigurationError):
            load_sources_config(write(tmp_path, text))

    def test_malformed_source_id_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL.replace("id: example_feed", "id: 'Example Feed!'")
        with pytest.raises(ConfigurationError):
            load_sources_config(write(tmp_path, text))


class TestSelection:
    def test_enabled_excludes_disabled_sources(self, tmp_path: Path) -> None:
        text = MINIMAL + """
  - id: switched_off
    name: Disabled Feed
    enabled: false
    adapter: rss
    url: https://off.invalid/feed.xml
    trust_tier: OFFICIAL
    editorial_role: Disabled source.
    priority: PRIMARY_NORMAL
    content_types: [NEWS]
    publisher_region: UNITED_STATES
    disabled_reason: Turned off for this test.
"""
        config = load_sources_config(write(tmp_path, text))
        assert [d.id for d in config.enabled()] == ["example_feed"]
        assert len(config.sources) == 2

    def test_get_finds_a_source_by_id(self, tmp_path: Path) -> None:
        config = load_sources_config(write(tmp_path, MINIMAL))
        assert config.get("example_feed").name == "Example Feed"

    def test_get_reports_unknown_ids_with_the_valid_options(self, tmp_path: Path) -> None:
        config = load_sources_config(write(tmp_path, MINIMAL))
        with pytest.raises(ConfigurationError, match="example_feed"):
            config.get("nope")


class TestConversionToDomain:
    def test_produces_a_persistable_source(self, tmp_path: Path) -> None:
        config = load_sources_config(write(tmp_path, MINIMAL))
        source = config.sources[0].to_source(config.defaults)
        assert source.id == "example_feed"
        assert source.kind is SourceKind.RSS
        assert source.trust_tier is TrustTier.OFFICIAL
        assert source.editorial_role == "Test source."

    def test_adapter_options_become_source_config(self, tmp_path: Path) -> None:
        text = MINIMAL + "    options:\n      future_setting: value\n"
        config = load_sources_config(write(tmp_path, text))
        source = config.sources[0].to_source(config.defaults)
        assert source.config == {"future_setting": "value"}

    def test_tags_are_preserved(self, tmp_path: Path) -> None:
        text = MINIMAL + "    tags: [product_update, chatgpt]\n"
        config = load_sources_config(write(tmp_path, text))
        assert config.sources[0].to_source(config.defaults).tags == ("product_update", "chatgpt")


class TestShippedConfiguration:
    """The real config/sources.yaml that ships with the project."""

    @pytest.fixture
    def shipped(self) -> SourcesConfig:
        return load_sources_config(DEFAULT_CONFIG_PATH)

    def test_it_is_valid(self, shipped: SourcesConfig) -> None:
        assert shipped.sources

    def test_it_configures_at_least_twenty_enabled_sources(self, shipped: SourcesConfig) -> None:
        """The v2 registry target (Step 2): quality over an exact count, but the
        source universe must be meaningfully broader than the original 8."""
        assert len(shipped.enabled()) >= 20

    def test_it_spans_every_adapter_kind(self, shipped: SourcesConfig) -> None:
        assert {d.adapter for d in shipped.enabled()} == set(SourceKind)

    def test_every_source_uses_an_implemented_adapter(self, shipped: SourcesConfig) -> None:
        from ai_news_editor.sources.registry import supported_kinds

        assert all(d.adapter in supported_kinds() for d in shipped.sources)

    def test_every_source_explains_itself(self, shipped: SourcesConfig) -> None:
        assert all(len(d.editorial_role.strip()) > 20 for d in shipped.sources)

    def test_every_url_is_https(self, shipped: SourcesConfig) -> None:
        assert all(d.url.startswith("https://") for d in shipped.sources)

    def test_the_mix_is_not_purely_official(self, shipped: SourcesConfig) -> None:
        """Vendors never publish the odd, viral or embarrassing stories themselves."""
        tiers = {d.trust_tier for d in shipped.enabled()}
        assert TrustTier.OFFICIAL in tiers
        assert TrustTier.REPUTABLE_SECONDARY in tiers

    def test_community_sources_are_always_signal_only(self, shipped: SourcesConfig) -> None:
        """A community source must never be configured as an authoritative one."""
        community = [d for d in shipped.sources if d.trust_tier is TrustTier.COMMUNITY_SIGNAL]
        assert community
        assert all(d.signal_only for d in community)

    def test_only_community_sources_are_signal_only(self, shipped: SourcesConfig) -> None:
        for definition in shipped.sources:
            if definition.signal_only:
                assert definition.trust_tier is TrustTier.COMMUNITY_SIGNAL

    def test_html_sources_declare_a_breakage_threshold(self, shipped: SourcesConfig) -> None:
        """A redesigned page must fail loudly rather than report zero items as success.

        Scoped to enabled sources only: a disabled entry's `adapter` is an aspirational
        best guess for "how this would work if it were ever enabled," not a verified,
        selector-tested configuration — fabricating selectors for a page nobody has
        actually inspected would be worse than leaving them unset.
        """
        for definition in shipped.enabled():
            if definition.adapter is SourceKind.HTML_CHANGELOG:
                assert definition.options.get("min_expected_items", 0) >= 1
                assert definition.options.get("item_selector")

    def test_contains_no_secret_shaped_values(self, shipped: SourcesConfig) -> None:
        """Comments may say the word "secrets"; no line may assign one."""
        lines = [
            line.lower()
            for line in DEFAULT_CONFIG_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for word in ("api_key", "apikey", "token", "password", "secret", "authorization"):
            assert not any(word in line for line in lines)


class TestFetchDefaults:
    def test_rejects_a_non_positive_timeout(self) -> None:
        with pytest.raises(ValueError, match="timeout_seconds"):
            FetchDefaults(timeout_seconds=0)

    def test_rejects_an_absurd_item_cap(self) -> None:
        with pytest.raises(ValueError, match="max_items_per_fetch"):
            FetchDefaults(max_items_per_fetch=100000)


class TestYamlFootguns:
    def test_a_name_yaml_reads_as_a_boolean_fails_loudly(self, tmp_path: Path) -> None:
        """YAML 1.1 turns bare Off/No/Yes into booleans. Better a clear error than a
        source silently named ``False``."""
        text = MINIMAL.replace("name: Example Feed", "name: Off")
        with pytest.raises(ConfigurationError, match="valid string"):
            load_sources_config(write(tmp_path, text))

    def test_quoting_resolves_it(self, tmp_path: Path) -> None:
        text = MINIMAL.replace("name: Example Feed", 'name: "Off"')
        assert load_sources_config(write(tmp_path, text)).sources[0].name == "Off"


class TestV2RegistryMetadata:
    """Step 2: priority, content_types, media_policy, fulltext_policy, source_family,
    domains and disabled_reason — none read by the live automation pipeline yet, but
    validated here so they are trustworthy whenever a later phase reads them."""

    def test_content_types_accepts_the_documented_vocabulary(self, tmp_path: Path) -> None:
        text = MINIMAL.replace(
            "content_types: [NEWS]",
            "content_types: [NEWS, AI_TOOL, FREE_DEAL, AI_LIFEHACK, PROMPT_WORKFLOW, "
            "EXPLAINER, RESEARCH, WEEKLY_DIGEST_INPUT]",
        )
        config = load_sources_config(write(tmp_path, text))
        assert config.sources[0].content_types == (
            ContentCapability.NEWS,
            ContentCapability.AI_TOOL,
            ContentCapability.FREE_DEAL,
            ContentCapability.AI_LIFEHACK,
            ContentCapability.PROMPT_WORKFLOW,
            ContentCapability.EXPLAINER,
            ContentCapability.RESEARCH,
            ContentCapability.WEEKLY_DIGEST_INPUT,
        )

    def test_an_unknown_content_type_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL.replace("content_types: [NEWS]", "content_types: [MEME_OF_THE_DAY]")
        with pytest.raises(ConfigurationError):
            load_sources_config(write(tmp_path, text))

    def test_content_types_may_not_be_empty(self, tmp_path: Path) -> None:
        text = MINIMAL.replace("content_types: [NEWS]", "content_types: []")
        with pytest.raises(ConfigurationError):
            load_sources_config(write(tmp_path, text))

    def test_content_types_is_required(self, tmp_path: Path) -> None:
        text = MINIMAL.replace("    content_types: [NEWS]\n", "")
        with pytest.raises(ConfigurationError, match="content_types"):
            load_sources_config(write(tmp_path, text))

    def test_priority_is_required(self, tmp_path: Path) -> None:
        text = MINIMAL.replace("    priority: PRIMARY_NORMAL\n", "")
        with pytest.raises(ConfigurationError, match="priority"):
            load_sources_config(write(tmp_path, text))

    @pytest.mark.parametrize(
        "policy", ["NO_MEDIA", "LINK_PREVIEW_ONLY", "DISCOVER_MEDIA", "EXPLICIT_REUSE_ALLOWED"]
    )
    def test_media_policy_accepts_the_documented_vocabulary(
        self, tmp_path: Path, policy: str
    ) -> None:
        text = MINIMAL + f"    media_policy: {policy}\n"
        config = load_sources_config(write(tmp_path, text))
        assert config.sources[0].media_policy is MediaPolicy(policy)

    def test_media_policy_defaults_to_no_media(self, tmp_path: Path) -> None:
        config = load_sources_config(write(tmp_path, MINIMAL))
        assert config.sources[0].media_policy is MediaPolicy.NO_MEDIA

    def test_an_unknown_media_policy_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL + "    media_policy: FREELY_REUSABLE\n"
        with pytest.raises(ConfigurationError):
            load_sources_config(write(tmp_path, text))

    def test_a_community_tier_source_must_use_community_priority(self, tmp_path: Path) -> None:
        """The check that matters: Tier C must never be able to outrank Tier A/B by
        quietly carrying a PRIMARY priority."""
        text = MINIMAL.replace("trust_tier: OFFICIAL", "trust_tier: COMMUNITY_SIGNAL").replace(
            "    priority: PRIMARY_NORMAL\n", "    priority: PRIMARY_HIGH\n    signal_only: true\n"
        )
        with pytest.raises(ConfigurationError, match="disagree"):
            load_sources_config(write(tmp_path, text))

    def test_community_priority_is_reserved_for_community_tier(self, tmp_path: Path) -> None:
        text = MINIMAL.replace("    priority: PRIMARY_NORMAL\n", "    priority: COMMUNITY\n")
        with pytest.raises(ConfigurationError, match="disagree"):
            load_sources_config(write(tmp_path, text))

    def test_a_correctly_paired_community_source_is_accepted(self, tmp_path: Path) -> None:
        text = MINIMAL.replace("trust_tier: OFFICIAL", "trust_tier: COMMUNITY_SIGNAL").replace(
            "    priority: PRIMARY_NORMAL\n", "    priority: COMMUNITY\n    signal_only: true\n"
        )
        config = load_sources_config(write(tmp_path, text))
        assert config.sources[0].priority is SourcePriority.COMMUNITY

    def test_a_disabled_source_must_carry_a_reason(self, tmp_path: Path) -> None:
        text = MINIMAL.replace("version: 1", "version: 1") + "    enabled: false\n"
        with pytest.raises(ConfigurationError, match="disabled_reason"):
            load_sources_config(write(tmp_path, text))

    def test_an_enabled_source_may_not_carry_a_disabled_reason(self, tmp_path: Path) -> None:
        text = MINIMAL + "    disabled_reason: Should not be here.\n"
        with pytest.raises(ConfigurationError, match="disabled_reason"):
            load_sources_config(write(tmp_path, text))

    def test_a_disabled_source_with_a_reason_loads_cleanly(self, tmp_path: Path) -> None:
        text = (
            MINIMAL
            + "    enabled: false\n"
            + "    disabled_reason: No working feed was found.\n"
        )
        config = load_sources_config(write(tmp_path, text))
        assert config.sources[0].disabled_reason == "No working feed was found."
        assert config.enabled() == []
        assert config.disabled() == config.sources

    def test_duplicate_feed_urls_are_rejected(self, tmp_path: Path) -> None:
        second = (
            MINIMAL.split("sources:")[1]
            .replace("example_feed", "example_feed_two")
            .replace("Example Feed", "Example Feed Two")
        )
        text = MINIMAL + second
        with pytest.raises(ConfigurationError, match="duplicate feed url"):
            load_sources_config(write(tmp_path, text))

    def test_domains_are_derived_from_the_url_by_default(self, tmp_path: Path) -> None:
        config = load_sources_config(write(tmp_path, MINIMAL))
        assert config.sources[0].canonical_domains == ("example.invalid",)

    def test_a_www_prefix_is_normalized_away(self, tmp_path: Path) -> None:
        text = MINIMAL.replace(
            "https://example.invalid/feed.xml", "https://www.example.invalid/feed.xml"
        )
        config = load_sources_config(write(tmp_path, text))
        assert config.sources[0].canonical_domains == ("example.invalid",)

    def test_explicit_domains_override_the_derived_one(self, tmp_path: Path) -> None:
        text = MINIMAL + "    domains: [other.invalid, www.Another.Invalid]\n"
        config = load_sources_config(write(tmp_path, text))
        assert config.sources[0].canonical_domains == ("other.invalid", "another.invalid")

    def test_source_family_defaults_to_none(self, tmp_path: Path) -> None:
        config = load_sources_config(write(tmp_path, MINIMAL))
        assert config.sources[0].source_family is None

    def test_source_family_groups_sibling_sources(self, tmp_path: Path) -> None:
        text = MINIMAL + "    source_family: Example Corp\n"
        second = (
            MINIMAL.split("sources:")[1]
            .replace("example_feed", "example_feed_two")
            .replace("https://example.invalid/feed.xml", "https://example.invalid/other.xml")
        ) + "    source_family: Example Corp\n"
        config = load_sources_config(write(tmp_path, text + second))
        families = {d.source_family for d in config.sources}
        assert families == {"Example Corp"}

    def test_fulltext_policy_defaults_to_normal_attempt(self, tmp_path: Path) -> None:
        from ai_news_editor.domain.enums import FulltextPolicy

        config = load_sources_config(write(tmp_path, MINIMAL))
        assert config.sources[0].fulltext_policy is FulltextPolicy.NORMAL_ATTEMPT

    def test_fulltext_policy_discovery_only_is_accepted(self, tmp_path: Path) -> None:
        from ai_news_editor.domain.enums import FulltextPolicy

        text = MINIMAL + "    fulltext_policy: DISCOVERY_ONLY\n"
        config = load_sources_config(write(tmp_path, text))
        assert config.sources[0].fulltext_policy is FulltextPolicy.DISCOVERY_ONLY


class TestShippedRegistryV2:
    """The real config/sources.yaml — the v2 registry specifically (tier mix, family
    grouping, disabled-source documentation), on top of TestShippedConfiguration's
    existing checks."""

    @pytest.fixture
    def shipped(self) -> SourcesConfig:
        return load_sources_config(DEFAULT_CONFIG_PATH)

    def test_every_source_declares_priority_and_content_types(
        self, shipped: SourcesConfig
    ) -> None:
        for definition in shipped.sources:
            assert definition.priority is not None
            assert definition.content_types

    def test_no_community_source_is_ever_a_primary_priority(self, shipped: SourcesConfig) -> None:
        for definition in shipped.sources:
            if definition.trust_tier is TrustTier.COMMUNITY_SIGNAL:
                assert definition.priority is SourcePriority.COMMUNITY

    def test_every_disabled_source_documents_why(self, shipped: SourcesConfig) -> None:
        for definition in shipped.disabled():
            assert definition.disabled_reason
            assert len(definition.disabled_reason.strip()) > 10

    def test_no_enabled_source_carries_a_disabled_reason(self, shipped: SourcesConfig) -> None:
        for definition in shipped.enabled():
            assert definition.disabled_reason is None

    def test_at_least_fifteen_tier_a_sources_are_enabled(self, shipped: SourcesConfig) -> None:
        tier_a = [d for d in shipped.enabled() if d.trust_tier is TrustTier.OFFICIAL]
        assert len(tier_a) >= 15

    def test_at_least_five_tier_b_sources_are_enabled(self, shipped: SourcesConfig) -> None:
        tier_b = [d for d in shipped.enabled() if d.trust_tier is TrustTier.REPUTABLE_SECONDARY]
        assert len(tier_b) >= 5

    def test_at_least_two_tier_c_sources_are_enabled(self, shipped: SourcesConfig) -> None:
        tier_c = [d for d in shipped.enabled() if d.trust_tier is TrustTier.COMMUNITY_SIGNAL]
        assert len(tier_c) >= 2

    def test_google_family_groups_its_sibling_feeds(self, shipped: SourcesConfig) -> None:
        google = [d for d in shipped.sources if d.source_family == "Google"]
        assert {d.id for d in google} >= {"google_ai_blog", "google_deepmind", "google_research"}

    def test_github_is_its_own_family_not_collapsed_into_microsoft(
        self, shipped: SourcesConfig
    ) -> None:
        github = shipped.get("github_changelog")
        assert github.source_family == "GitHub"
        assert github.source_family != "Microsoft"

"""Source configuration loading and strict validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_news_editor.domain.enums import SourceKind, TrustTier
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

    def test_it_configures_eight_enabled_sources(self, shipped: SourcesConfig) -> None:
        assert len(shipped.enabled()) == 8

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
        """A redesigned page must fail loudly rather than report zero items as success."""
        for definition in shipped.sources:
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

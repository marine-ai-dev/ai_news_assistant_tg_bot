"""URL canonicalization.

Tested heavily because the failure mode is asymmetric: over-normalizing merges two
different pages into one story and silently loses news, while under-normalizing only
lets a duplicate through to the editor.
"""

from __future__ import annotations

import pytest

from ai_news_editor.pipeline.urls import (
    InvalidUrlError,
    canonicalize_url,
    is_tracking_param,
    try_canonicalize_url,
)


class TestCaseAndHost:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("HTTPS://Example.Invalid/Path", "https://example.invalid/Path"),
            ("https://WWW.Example.Invalid/x", "https://example.invalid/x"),
            ("http://www.example.invalid/x", "http://example.invalid/x"),
        ],
    )
    def test_scheme_and_host_are_lowercased_and_www_dropped(self, url: str, expected: str) -> None:
        assert canonicalize_url(url) == expected

    def test_path_case_is_preserved(self) -> None:
        """Path segments are case-sensitive on many servers; lowering them breaks links."""
        assert canonicalize_url("https://example.invalid/News/GPT-5") == (
            "https://example.invalid/News/GPT-5"
        )

    def test_a_host_that_merely_starts_with_www_is_untouched(self) -> None:
        assert canonicalize_url("https://wwwfoo.invalid/x") == "https://wwwfoo.invalid/x"


class TestPorts:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://example.invalid:443/x", "https://example.invalid/x"),
            ("http://example.invalid:80/x", "http://example.invalid/x"),
        ],
    )
    def test_default_ports_are_removed(self, url: str, expected: str) -> None:
        assert canonicalize_url(url) == expected

    def test_non_default_port_is_kept(self) -> None:
        assert canonicalize_url("https://example.invalid:8443/x") == (
            "https://example.invalid:8443/x"
        )


class TestFragments:
    def test_fragment_is_dropped(self) -> None:
        assert canonicalize_url("https://example.invalid/a#section") == "https://example.invalid/a"

    def test_fragment_only_difference_collapses(self) -> None:
        assert canonicalize_url("https://example.invalid/a#one") == canonicalize_url(
            "https://example.invalid/a#two"
        )


class TestTrackingParameters:
    @pytest.mark.parametrize(
        "name",
        ["utm_source", "utm_medium", "utm_campaign", "UTM_SOURCE", "fbclid", "gclid", "mc_cid"],
    )
    def test_known_tracking_params_are_recognised(self, name: str) -> None:
        assert is_tracking_param(name)

    def test_tracking_params_are_stripped(self) -> None:
        assert canonicalize_url(
            "https://example.invalid/post?utm_source=twitter&utm_medium=social"
        ) == "https://example.invalid/post"

    def test_stripping_leaves_meaningful_params(self) -> None:
        assert canonicalize_url(
            "https://example.invalid/post?id=42&utm_source=rss&page=2"
        ) == "https://example.invalid/post?id=42&page=2"

    @pytest.mark.parametrize(
        "name", ["id", "p", "page", "v", "q", "story", "article", "lang", "s", "post_id"]
    )
    def test_content_selecting_params_are_never_stripped(self, name: str) -> None:
        """Dropping one of these would silently point at a different page."""
        assert not is_tracking_param(name)
        url = f"https://example.invalid/x?{name}=7"
        assert canonicalize_url(url) == url

    def test_youtube_style_video_id_survives(self) -> None:
        assert canonicalize_url("https://example.invalid/watch?v=abc123&utm_source=x") == (
            "https://example.invalid/watch?v=abc123"
        )

    def test_parameter_order_is_preserved(self) -> None:
        assert canonicalize_url("https://example.invalid/x?b=2&a=1") == (
            "https://example.invalid/x?b=2&a=1"
        )

    def test_blank_values_are_kept(self) -> None:
        assert canonicalize_url("https://example.invalid/x?flag=") == (
            "https://example.invalid/x?flag="
        )


class TestPaths:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://example.invalid/a/", "https://example.invalid/a"),
            ("https://example.invalid/a", "https://example.invalid/a"),
            ("https://example.invalid/", "https://example.invalid/"),
            ("https://example.invalid", "https://example.invalid/"),
        ],
    )
    def test_trailing_slash_handling(self, url: str, expected: str) -> None:
        assert canonicalize_url(url) == expected

    @pytest.mark.parametrize("suffix", ["index.html", "index.htm", "index.php"])
    def test_index_documents_reduce_to_their_directory(self, suffix: str) -> None:
        assert canonicalize_url(f"https://example.invalid/news/{suffix}") == (
            "https://example.invalid/news"
        )


class TestRejection:
    @pytest.mark.parametrize(
        "url",
        ["", "   ", "not a url", "ftp://example.invalid/f", "file:///etc/passwd", "mailto:a@b.c"],
    )
    def test_unusable_urls_raise(self, url: str) -> None:
        with pytest.raises(InvalidUrlError):
            canonicalize_url(url)

    def test_missing_host_raises(self) -> None:
        with pytest.raises(InvalidUrlError, match="no host"):
            canonicalize_url("https:///path")

    def test_try_variant_returns_none_instead_of_raising(self) -> None:
        assert try_canonicalize_url("ftp://example.invalid/f") is None
        assert try_canonicalize_url(None) is None
        assert try_canonicalize_url("https://example.invalid/x") == "https://example.invalid/x"


class TestStability:
    def test_canonicalization_is_idempotent(self) -> None:
        once = canonicalize_url("https://WWW.Example.invalid:443/News/?utm_source=x&id=3#frag")
        assert canonicalize_url(once) == once

    def test_equivalent_urls_converge(self) -> None:
        """The point of the whole module: the same page from two feeds becomes one string."""
        variants = [
            "https://example.invalid/news/gpt",
            "https://www.example.invalid/news/gpt",
            "https://example.invalid/news/gpt/",
            "https://example.invalid/news/gpt#top",
            "https://example.invalid/news/gpt?utm_source=feed",
            "HTTPS://EXAMPLE.INVALID:443/news/gpt",
        ]
        assert len({canonicalize_url(v) for v in variants}) == 1

    def test_different_pages_stay_different(self) -> None:
        assert canonicalize_url("https://example.invalid/news/a") != canonicalize_url(
            "https://example.invalid/news/b"
        )

    def test_non_ascii_paths_survive(self) -> None:
        assert "новини" in canonicalize_url("https://example.invalid/новини/шi")

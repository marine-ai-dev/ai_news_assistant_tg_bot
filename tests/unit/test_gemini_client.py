"""GeminiClient's timeout configuration and retry semantics.

These were previously verified only by hand, against mocked 429/403/500 responses,
while the client itself was being written — never as pytest. A real GitHub Actions
dry-run (run 32119037001) is what surfaced the gap in the original single flat 30s
timeout: selection needed two retries to succeed and generation exhausted its retry
budget entirely, both on read timeouts from a genuinely slow — not broken — Gemini
response. This file exists so a future change to either the timeout budgets or the
retry policy has to break a test to get it wrong.
"""

from __future__ import annotations

import httpx
import pytest

from ai_news_editor.automation.gemini import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_POOL_TIMEOUT_SECONDS,
    DEFAULT_READ_TIMEOUT_SECONDS,
    DEFAULT_WRITE_TIMEOUT_SECONDS,
    GeminiClient,
    GeminiRequestError,
    GeminiTransientError,
)

#: Assembled rather than written out — see the project-wide convention: a secret
#: scanner cannot tell a placeholder from a credential, and this string is shaped like
#: one even though it is not.
FAKE_KEY = "AIzaSy" + "z" * 33

_SCHEMA: dict[str, object] = {"type": "OBJECT", "properties": {}}


def _ok_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}
            ]
        },
    )


def _client(handler, **overrides: object) -> GeminiClient:
    kwargs: dict[str, object] = {
        "model": "gemini-test",
        "backoff_seconds": 0.0,  # no real sleeping between retries in tests
        "transport": httpx.MockTransport(handler),
    }
    kwargs.update(overrides)
    return GeminiClient(FAKE_KEY, **kwargs)  # type: ignore[arg-type]


class TestTimeoutConfiguration:
    """httpx.Timeout, not a single flat number — connect/write/pool stay short and
    fixed (a network that is not going to answer at all should fail fast); read is
    the one budget long enough for a model that is genuinely still generating, and the
    one this project makes configurable."""

    def test_defaults_match_the_documented_budgets(self) -> None:
        assert DEFAULT_CONNECT_TIMEOUT_SECONDS == 10.0
        assert DEFAULT_READ_TIMEOUT_SECONDS == 90.0
        assert DEFAULT_WRITE_TIMEOUT_SECONDS == 30.0
        assert DEFAULT_POOL_TIMEOUT_SECONDS == 10.0

        client = _client(lambda request: _ok_response())
        timeout = client._client.timeout  # the actual httpx.Timeout passed to httpx.Client
        assert timeout.connect == DEFAULT_CONNECT_TIMEOUT_SECONDS
        assert timeout.read == DEFAULT_READ_TIMEOUT_SECONDS
        assert timeout.write == DEFAULT_WRITE_TIMEOUT_SECONDS
        assert timeout.pool == DEFAULT_POOL_TIMEOUT_SECONDS
        client.close()

    def test_read_timeout_is_independently_configurable(self) -> None:
        """This is the exact knob AI_NEWS_GEMINI_READ_TIMEOUT_SECONDS drives — proving
        it moves only the read budget, not the others, is what makes it safe to raise
        without also loosening the fast-fail guarantees on connect/write/pool."""
        client = _client(lambda request: _ok_response(), read_timeout=45.0)
        timeout = client._client.timeout
        assert timeout.read == 45.0
        assert timeout.connect == DEFAULT_CONNECT_TIMEOUT_SECONDS
        assert timeout.write == DEFAULT_WRITE_TIMEOUT_SECONDS
        assert timeout.pool == DEFAULT_POOL_TIMEOUT_SECONDS
        client.close()


class TestRetrySemantics:
    def test_a_read_timeout_is_retried_and_can_still_succeed(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) < DEFAULT_MAX_ATTEMPTS:
                raise httpx.ReadTimeout("simulated read timeout", request=request)
            return _ok_response()

        client = _client(handler)
        result = client.generate(system_instruction="x", prompt="y", response_schema=_SCHEMA)
        assert len(calls) == DEFAULT_MAX_ATTEMPTS
        assert result.text == "{}"
        client.close()

    def test_retries_are_bounded_never_infinite(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ReadTimeout("simulated read timeout", request=request)

        client = _client(handler)
        with pytest.raises(GeminiTransientError):
            client.generate(system_instruction="x", prompt="y", response_schema=_SCHEMA)
        assert len(calls) == DEFAULT_MAX_ATTEMPTS == 3
        client.close()

    def test_a_connection_error_is_retried(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) < 2:
                raise httpx.ConnectError("simulated connection failure", request=request)
            return _ok_response()

        client = _client(handler)
        client.generate(system_instruction="x", prompt="y", response_schema=_SCHEMA)
        assert len(calls) == 2
        client.close()

    def test_429_is_retried(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) < 2:
                return httpx.Response(429, json={"error": {"message": "rate limited"}})
            return _ok_response()

        client = _client(handler)
        client.generate(system_instruction="x", prompt="y", response_schema=_SCHEMA)
        assert len(calls) == 2
        client.close()

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_selected_5xx_statuses_are_retried(self, status: int) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) < 2:
                return httpx.Response(status, json={"error": {"message": "server error"}})
            return _ok_response()

        client = _client(handler)
        client.generate(system_instruction="x", prompt="y", response_schema=_SCHEMA)
        assert len(calls) == 2
        client.close()

    def test_a_permanent_4xx_is_never_retried(self) -> None:
        """An invalid API key, in particular — see automation/provider.py's own test
        for why this distinction matters at the pipeline level too."""
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                403, json={"error": {"message": "API key not valid", "code": 403}}
            )

        client = _client(handler)
        with pytest.raises(GeminiRequestError):
            client.generate(system_instruction="x", prompt="y", response_schema=_SCHEMA)
        assert len(calls) == 1, "a permanent rejection must not be retried at all"
        client.close()

    def test_malformed_json_is_never_retried(self) -> None:
        """Asking the same question again will not fix a model that returned garbage —
        this is a content-level rejection, not a transport failure."""
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, content=b"not valid json at all")

        client = _client(handler)
        from ai_news_editor.automation.gemini import GeminiResponseError

        with pytest.raises(GeminiResponseError):
            client.generate(system_instruction="x", prompt="y", response_schema=_SCHEMA)
        assert len(calls) == 1
        client.close()

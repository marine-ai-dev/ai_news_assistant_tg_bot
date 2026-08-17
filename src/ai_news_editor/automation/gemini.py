"""A thin, synchronous client for the Gemini Developer API's ``generateContent`` call.

REST over httpx, matching the rest of this project — no Google SDK. The exact contract
below was read from the live documentation while this module was written
(https://ai.google.dev/api/generate-content, https://ai.google.dev/gemini-api/docs/api-key),
not assumed:

* ``POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent``
* the API key travels in the ``x-goog-api-key`` header. The canonical curl example on
  that page authenticates via a ``?key=`` query parameter instead; the header form is
  used here on purpose, so the key is never part of a URL that could end up in an
  access log or an exception message built from ``request.url``.
* the request body is ``{"contents": [...], "systemInstruction": {...},
  "generationConfig": {"responseMimeType": "application/json", "responseSchema": {...}}}``
* the response is ``candidates[0].content.parts[0].text`` — a JSON *string*, because
  ``responseMimeType`` asks for structured output as text, not a typed field.

Retry policy is deliberately narrow, and the reason is written once, in
:mod:`automation.pipeline`, and enforced here: this project already treats a retry as
dangerous wherever it could produce a second real effect in front of an audience, and a
generation call is the input to exactly that kind of effect. Only clearly transient
transport failures are retried — timeouts, connection errors, 429, and a short list of
5xx codes — with a small bounded count and exponential backoff. A 4xx that is not 429
means the request itself was wrong (bad key, bad model, bad payload) and retrying it
would just ask the same broken question again.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx

from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.observability.redaction import redact

logger = get_logger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 2.0

#: Retried because they are plausibly transient, mirroring sources.http's own list for
#: the same reason: a 404 or 401 will not fix itself, and retrying is just rude.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_USER_AGENT = "AiNewsEditorBot-automation (+https://github.com/marine-ai-dev/ai_news_assistant_tg_bot)"


class GeminiError(AiNewsError):
    """Base for every failure this client can raise."""


class GeminiConfigurationError(GeminiError):
    """No key, or a model id so obviously wrong the call was never attempted."""


class GeminiTransientError(GeminiError):
    """Timeout, connection failure, or a retryable status — exhausted all attempts."""


class GeminiRequestError(GeminiError):
    """A non-retryable 4xx: the request itself was rejected, not just delayed."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"Gemini rejected the request (HTTP {status_code}): {detail}")
        self.status_code = status_code


class GeminiResponseError(GeminiError):
    """The call succeeded but the response was not usable — blocked, empty, malformed."""


@dataclass(frozen=True, slots=True)
class GeminiResult:
    """The raw text Gemini returned, before any schema is applied to it.

    Kept separate from the parsed automation schema on purpose: this client's job ends
    at "here is what the model said", not at deciding whether that is a valid
    :class:`~automation.schema.GeneratedPost`. That parsing, and what a failure to parse
    means, belongs to the caller.
    """

    text: str
    finish_reason: str | None
    model: str


class GeminiClient:
    """One model, one API key, reused across the select and generate calls of a run."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        api_root: str = API_ROOT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        if not api_key or not api_key.strip():
            raise GeminiConfigurationError(
                "no Gemini API key configured. Set AI_NEWS_GEMINI_API_KEY."
            )
        if not model or not model.strip():
            raise GeminiConfigurationError(
                "no Gemini model configured. Set AI_NEWS_LLM_MODEL."
            )
        self._model = model.strip()
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = backoff_seconds
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            headers={
                "x-goog-api-key": api_key.strip(),
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
            },
        )
        self._api_root = api_root.rstrip("/")

    def __enter__(self) -> GeminiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def generate(
        self,
        *,
        system_instruction: str,
        prompt: str,
        response_schema: dict[str, object],
    ) -> GeminiResult:
        """One ``generateContent`` call, with a JSON schema Gemini's own output must fit.

        Raises:
            GeminiTransientError: every retry attempt failed with a transient cause.
            GeminiRequestError: a non-retryable rejection — bad key, bad model, bad
                payload. Retrying would not help.
            GeminiResponseError: the call succeeded but produced nothing usable — the
                prompt or the content was blocked, or the response was not valid JSON.
        """
        url = f"{self._api_root}/models/{self._model}:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": response_schema,
            },
        }

        last_error: GeminiTransientError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._attempt(url, body)
            except GeminiTransientError as exc:
                last_error = exc
                if attempt < self._max_attempts:
                    delay = self._backoff_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        "retrying Gemini call",
                        extra={"attempt": attempt, "reason": redact(str(exc))},
                    )
                    time.sleep(delay)

        assert last_error is not None  # the loop always sets one before exhausting
        raise last_error

    def _attempt(self, url: str, body: dict[str, object]) -> GeminiResult:
        try:
            response = self._client.post(url, json=body)
        except httpx.TimeoutException as exc:
            raise GeminiTransientError(f"Gemini request timed out: {redact(str(exc))}") from exc
        except httpx.HTTPError as exc:
            raise GeminiTransientError(
                f"Gemini request failed: {redact(str(exc))}"
            ) from exc

        if response.status_code in _RETRYABLE_STATUS:
            raise GeminiTransientError(
                f"Gemini returned HTTP {response.status_code}: "
                f"{redact(_short_body(response))}"
            )
        if response.status_code >= 400:
            raise GeminiRequestError(response.status_code, redact(_short_body(response)))

        return self._parse(response)

    def _parse(self, response: httpx.Response) -> GeminiResult:
        try:
            payload = response.json()
        except ValueError as exc:
            raise GeminiResponseError(
                f"Gemini response was not valid JSON: {redact(str(exc))}"
            ) from exc

        block_reason = payload.get("promptFeedback", {}).get("blockReason")
        if block_reason:
            raise GeminiResponseError(f"Gemini blocked the prompt: {block_reason}")

        candidates = payload.get("candidates") or []
        if not candidates:
            raise GeminiResponseError("Gemini returned no candidates")

        candidate = candidates[0]
        finish_reason = candidate.get("finishReason")
        parts = candidate.get("content", {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        if not text.strip():
            raise GeminiResponseError(
                f"Gemini returned an empty response (finishReason={finish_reason})"
            )

        return GeminiResult(text=text, finish_reason=finish_reason, model=self._model)


def parse_json_object(text: str) -> dict[str, object]:
    """Parse Gemini's structured-output text as a JSON object.

    A separate function, not folded into the client, because "the model said something
    that is not valid JSON" and "the model said valid JSON that fails our schema" are
    both ordinary rejections at the pipeline level — neither is a transport failure, and
    neither is worth retrying in the hope that asking again produces different prose.

    Raises:
        GeminiResponseError: not a JSON object.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeminiResponseError(f"Gemini output was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise GeminiResponseError(
            f"Gemini output was valid JSON but not an object (got {type(parsed).__name__})"
        )
    return parsed


def _short_body(response: httpx.Response) -> str:
    """The first part of a response body, for an error message — never the whole thing."""
    try:
        return response.text[:300]
    except Exception:  # pragma: no cover - defensive
        return "(unreadable response body)"

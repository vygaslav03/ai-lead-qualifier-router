"""Provider-agnostic LLM interface with retry/backoff and safe JSON parsing."""
import asyncio
import json
import logging
import random
import re
from abc import ABC, abstractmethod

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    """Non-retryable provider error (bad key, bad request, ...)."""


class RetryableLLMError(LLMError):
    """Rate limit or transient server error; safe to retry."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class InvalidJSONError(LLMError):
    """Model returned something that is not a JSON object."""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_response(raw: str) -> dict:
    """Strip markdown fences / preamble and parse the first JSON object."""
    text = _FENCE_RE.sub("", raw.strip()).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise InvalidJSONError(f"No JSON object in response: {raw[:200]!r}")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise InvalidJSONError(f"Invalid JSON: {e}; response: {raw[:200]!r}") from e
    if not isinstance(data, dict):
        raise InvalidJSONError(f"Expected a JSON object, got {type(data).__name__}")
    return data


def raise_for_http_status(response: httpx.Response, provider: str) -> None:
    """Map HTTP errors of REST-based providers onto our exception types."""
    if response.is_success:
        return
    msg = f"{provider} HTTP {response.status_code}: {response.text[:300]}"
    if response.status_code in RETRYABLE_STATUS:
        retry_after = response.headers.get("retry-after")
        try:
            delay = float(retry_after) if retry_after else None
        except ValueError:
            delay = None
        raise RetryableLLMError(msg, retry_after=delay)
    raise LLMError(msg)


class LLMClient(ABC):
    """Single interface used by the rest of the app, whatever the provider."""

    provider: str = "base"

    def __init__(self, model: str, max_retries: int = 5, base_delay: float = 2.0, max_delay: float = 60.0):
        self.model = model
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

    async def generate_json(self, system: str, user: str, schema: dict | None = None) -> dict:
        raw = await self._with_retry(system, user, json_mode=True, schema=schema)
        return parse_json_response(raw)

    async def generate_text(self, system: str, user: str) -> str:
        return (await self._with_retry(system, user, json_mode=False, schema=None)).strip()

    @abstractmethod
    async def _complete(self, system: str, user: str, *, json_mode: bool, schema: dict | None) -> str:
        """One raw provider call. Raise RetryableLLMError on 429/5xx."""

    async def _with_retry(self, system: str, user: str, *, json_mode: bool, schema: dict | None) -> str:
        attempt = 0
        while True:
            try:
                return await self._complete(system, user, json_mode=json_mode, schema=schema)
            except RetryableLLMError as e:
                attempt += 1
                if attempt > self.max_retries:
                    raise
                backoff = min(self.base_delay * 2 ** (attempt - 1), self.max_delay)
                delay = max(backoff, e.retry_after or 0) + random.uniform(0, 1)
                log.warning(
                    "%s: retryable error (attempt %d/%d), sleeping %.1fs: %s",
                    self.provider, attempt, self.max_retries, delay, str(e)[:150],
                )
                await asyncio.sleep(delay)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} model={self.model}>"

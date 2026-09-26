"""Google Gemini (free tier) via the google-genai SDK."""
import re

import httpx
from google import genai
from google.genai import errors, types

from .base import RETRYABLE_STATUS, LLMClient, LLMError, RetryableLLMError

REQUEST_TIMEOUT_S = 30  # under "high demand" Gemini can hold a request open for minutes

_RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s")


class GeminiClient(LLMClient):
    provider = "gemini"

    def __init__(self, api_key: str, model: str, **kwargs):
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set (get a free key at https://aistudio.google.com/apikey)")
        super().__init__(model, **kwargs)
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_S * 1000))

    async def _complete(self, system: str, user: str, *, json_mode: bool, schema: dict | None) -> str:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.1 if json_mode else 0.6,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        if json_mode:
            config.response_mime_type = "application/json"
            if schema:
                config.response_json_schema = schema
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model, contents=user, config=config
            )
        except httpx.TimeoutException as e:
            raise RetryableLLMError(f"Gemini request timed out after {REQUEST_TIMEOUT_S}s") from e
        except httpx.TransportError as e:
            raise RetryableLLMError(f"Gemini connection error: {e}") from e
        except errors.APIError as e:
            if e.code == 429 and "PerDay" in str(e):
                # Daily free-tier quota: retrying within seconds cannot help
                raise LLMError(
                    f"Gemini daily free-tier quota exhausted for {self.model}. "
                    "Try again tomorrow, set LLM_MODEL to another model, or switch LLM_PROVIDER."
                ) from e
            if e.code in RETRYABLE_STATUS:
                match = _RETRY_DELAY_RE.search(str(e))
                raise RetryableLLMError(
                    f"Gemini {e.code}: {e.message}", retry_after=float(match.group(1)) if match else None
                ) from e
            raise LLMError(f"Gemini {e.code}: {e.message}") from e

        if not response.text:
            raise RetryableLLMError("Gemini returned an empty response")
        return response.text

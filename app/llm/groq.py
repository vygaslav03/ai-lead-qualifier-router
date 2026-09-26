"""Groq free tier via its OpenAI-compatible REST API."""
import httpx

from .base import LLMClient, LLMError, RetryableLLMError, raise_for_http_status

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqClient(LLMClient):
    provider = "groq"

    def __init__(self, api_key: str, model: str, **kwargs):
        if not api_key:
            raise LLMError("GROQ_API_KEY is not set (get a free key at https://console.groq.com/keys)")
        super().__init__(model, **kwargs)
        self._api_key = api_key

    async def _complete(self, system: str, user: str, *, json_mode: bool, schema: dict | None) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1 if json_mode else 0.6,
        }
        if json_mode:
            # json_object works on all Groq models; the schema itself is described in the prompt
            body["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    GROQ_URL, json=body, headers={"Authorization": f"Bearer {self._api_key}"}
                )
        except httpx.TransportError as e:
            raise RetryableLLMError(f"Groq connection error: {e}") from e
        raise_for_http_status(response, "Groq")
        return response.json()["choices"][0]["message"]["content"] or ""

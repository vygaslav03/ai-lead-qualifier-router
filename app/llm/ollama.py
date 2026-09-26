"""Local model via Ollama (fully offline, no API key)."""
import httpx

from .base import LLMClient, LLMError, RetryableLLMError, raise_for_http_status


class OllamaClient(LLMClient):
    provider = "ollama"

    def __init__(self, base_url: str, model: str, **kwargs):
        super().__init__(model, **kwargs)
        self._url = base_url.rstrip("/") + "/api/chat"

    async def _complete(self, system: str, user: str, *, json_mode: bool, schema: dict | None) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.1 if json_mode else 0.6},
        }
        if json_mode:
            body["format"] = schema or "json"
        try:
            # Local models can be slow on CPU, especially on first load
            async with httpx.AsyncClient(timeout=300) as client:
                response = await client.post(self._url, json=body)
        except httpx.ConnectError as e:
            raise LLMError(f"Cannot reach Ollama at {self._url}. Is `ollama serve` running?") from e
        except httpx.TransportError as e:
            raise RetryableLLMError(f"Ollama transport error: {e}") from e
        raise_for_http_status(response, "Ollama")
        return response.json()["message"]["content"]

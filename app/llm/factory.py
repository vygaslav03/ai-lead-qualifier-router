"""Pick the LLM provider from LLM_PROVIDER."""
from ..config import Settings, get_settings
from .base import LLMClient, LLMError


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    s = settings or get_settings()
    provider = s.llm_provider.lower().strip()
    common = {"model": s.resolved_model, "max_retries": s.llm_max_retries}

    if provider == "gemini":
        from .gemini import GeminiClient
        return GeminiClient(api_key=s.gemini_api_key, **common)
    if provider == "groq":
        from .groq import GroqClient
        return GroqClient(api_key=s.groq_api_key, **common)
    if provider == "ollama":
        from .ollama import OllamaClient
        return OllamaClient(base_url=s.ollama_base_url, **common)
    raise LLMError(f"Unknown LLM_PROVIDER {s.llm_provider!r}; use gemini, groq or ollama")

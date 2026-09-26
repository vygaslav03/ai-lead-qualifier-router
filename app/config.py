"""Application settings, loaded from environment variables / .env."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

DEFAULT_MODELS = {
    "gemini": "gemini-flash-lite-latest",
    "groq": "llama-3.3-70b-versatile",
    "ollama": "qwen2.5:7b",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    # LLM
    llm_provider: str = "gemini"
    llm_model: str = ""
    gemini_api_key: str = ""
    groq_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    llm_max_retries: int = 5
    llm_request_delay: float = 6.0

    # Used in customer replies (leave empty to omit)
    company_name: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_manager_chat_id: str = ""

    # Storage
    database_path: str = "data/leads.db"
    google_sheets_credentials_file: str = ""
    google_sheet_id: str = ""

    # Server
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def resolved_model(self) -> str:
        return self.llm_model or DEFAULT_MODELS.get(self.llm_provider.lower(), "")


@lru_cache
def get_settings() -> Settings:
    return Settings()

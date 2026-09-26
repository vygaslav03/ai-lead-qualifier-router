"""FastAPI app: HTML form at "/" and a small JSON API for leads."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .config import BASE_DIR, DATA_DIR, get_settings
from .extractor import ExtractionError
from .llm import LLMError
from .matcher import MatchResult
from .models import LeadRecord, LeadStatus
from .pipeline import LeadPipeline
from .telegram_bot import TelegramBot

log = logging.getLogger(__name__)

FORM_HTML = BASE_DIR / "app" / "templates" / "form.html"


class LeadIn(BaseModel):
    text: str = Field(min_length=10, max_length=5000, description="Raw customer message")
    source: str = Field(default="web_form", pattern="^(web_form|email|sms)$")


class LeadOut(BaseModel):
    lead: LeadRecord
    match: dict | None = None


def match_to_dict(match: MatchResult) -> dict:
    return {
        "contractor": asdict(match.contractor) if match.contractor else None,
        "reason": match.reason,
        "metro": match.metro,
        "alternatives": [asdict(c) for c in match.alternatives],
        "unavailable": [asdict(c) for c in match.unavailable],
    }


def create_app(pipeline: LeadPipeline | None = None, enable_telegram: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        app.state.pipeline = p = pipeline or LeadPipeline.from_settings(settings)
        log.info("Pipeline ready: LLM=%s model=%s", p.llm.provider, p.llm.model)

        bot, poll_task = None, None
        if enable_telegram and settings.telegram_bot_token and settings.telegram_manager_chat_id:
            bot = TelegramBot(settings.telegram_bot_token, settings.telegram_manager_chat_id, p)
            p.notifier = bot.send_lead
            poll_task = asyncio.create_task(bot.run_polling(), name="telegram-polling")
        else:
            log.warning("Telegram not configured - leads are saved but no notifications are sent")
        app.state.telegram_enabled = bot is not None

        yield

        if poll_task:
            poll_task.cancel()
            try:
                await poll_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if bot:
            await bot.close()
        p.storage.close()  # flush pending Google Sheets writes

    app = FastAPI(title="AI Lead Qualifier & Router", version="0.6.0", lifespan=lifespan)

    def get_pipeline(request: Request) -> LeadPipeline:
        return request.app.state.pipeline

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def form() -> str:
        return FORM_HTML.read_text(encoding="utf-8")

    @app.get("/health")
    async def health(request: Request) -> dict:
        p = get_pipeline(request)
        return {
            "status": "ok",
            "llm_provider": p.llm.provider,
            "llm_model": p.llm.model,
            "telegram": request.app.state.telegram_enabled,
            "google_sheets": type(p.storage).__name__ == "MirroredStorage",
        }

    @app.post("/api/leads", response_model=LeadOut, status_code=201)
    async def create_lead(body: LeadIn, request: Request) -> LeadOut:
        try:
            lead, match = await get_pipeline(request).process(body.text.strip(), body.source)
        except ExtractionError as e:
            raise HTTPException(422, f"Could not understand the message: {e}") from e
        except LLMError as e:
            log.error("LLM failure: %s", e)
            raise HTTPException(503, f"LLM provider unavailable: {e}") from e
        return LeadOut(lead=lead, match=match_to_dict(match))

    @app.get("/api/leads", response_model=list[LeadRecord])
    async def list_leads(
        request: Request,
        status: LeadStatus | None = None,
        limit: int = Query(20, ge=1, le=200),
    ) -> list[LeadRecord]:
        return get_pipeline(request).storage.list_leads(limit=limit, status=status)

    @app.get("/api/leads/{lead_id}")
    async def get_lead(lead_id: str, request: Request) -> dict:
        storage = get_pipeline(request).storage
        lead = storage.get_lead(lead_id)
        if not lead:
            raise HTTPException(404, "Lead not found")
        return {"lead": lead.model_dump(mode="json"), "events": storage.get_events(lead_id)}

    @app.get("/api/samples", include_in_schema=False)
    async def samples() -> list[dict]:
        """Sample messages for the demo form's 'try an example' buttons."""
        data = json.loads((DATA_DIR / "sample_leads.json").read_text(encoding="utf-8"))
        return [{"id": s["id"], "label": s["note"], "source": s["source"], "text": s["text"]} for s in data]

    return app

"""Lead processing pipeline: extract -> match -> save -> draft reply -> notify.

Used by the API, the Telegram bot and the demo script."""
import logging
import secrets
from datetime import datetime
from typing import Awaitable, Callable

from .config import DATA_DIR, Settings, get_settings
from .extractor import extract_lead
from .llm import LLMClient, get_llm_client
from .matcher import ContractorDirectory, MatchResult
from .models import LeadExtraction, LeadRecord, LeadStatus
from .reply import draft_reply
from .storage import LeadStorage, build_storage, now_iso

log = logging.getLogger(__name__)

# Sends a new lead to the manager and returns the chat message id (or None)
Notifier = Callable[[LeadRecord], Awaitable[int | None]]


def new_lead_id() -> str:
    return f"LD-{datetime.now():%y%m%d}-{secrets.token_hex(2).upper()}"


class LeadPipeline:
    def __init__(self, llm: LLMClient, directory: ContractorDirectory, storage: LeadStorage,
                 notifier: Notifier | None = None, company_name: str = ""):
        self.llm = llm
        self.directory = directory
        self.storage = storage
        self.notifier = notifier
        self.company_name = company_name

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "LeadPipeline":
        s = settings or get_settings()
        return cls(
            llm=get_llm_client(s),
            directory=ContractorDirectory(DATA_DIR / "contractors.csv"),
            storage=build_storage(s),
            company_name=s.company_name,
        )

    async def process(self, text: str, source: str = "web_form") -> tuple[LeadRecord, MatchResult]:
        """Full path for a new raw message."""
        extraction = await extract_lead(text, self.llm, source=source)
        lead, match = self.route(extraction, text, source)
        lead = await self.add_draft_reply(lead)
        lead = await self.notify(lead)
        return lead, match

    def route(self, extraction: LeadExtraction, raw_text: str, source: str) -> tuple[LeadRecord, MatchResult]:
        """Match an already-extracted lead to a contractor and persist it."""
        match = self.directory.match(extraction)
        ts = now_iso()
        lead = LeadRecord(
            id=new_lead_id(),
            created_at=ts,
            updated_at=ts,
            source=source,
            raw_text=raw_text,
            extraction=extraction,
            status=LeadStatus.MATCHED if match.matched else LeadStatus.NEEDS_MANUAL_ASSIGNMENT,
            contractor_id=match.contractor.id if match.contractor else None,
            contractor_name=match.contractor.name if match.contractor else None,
            match_reason=match.reason,
            tried_contractors=[match.contractor.id] if match.contractor else [],
        )
        self.storage.save_lead(lead)
        log.info("Lead %s saved: %s (%s)", lead.id, lead.status.value, match.reason)
        return lead, match

    async def add_draft_reply(self, lead: LeadRecord) -> LeadRecord:
        """Second LLM call. Never fails the lead - falls back to a template inside draft_reply."""
        reply = await draft_reply(lead, self.llm, self.company_name)
        return self.storage.update_lead(lead.id, draft_reply=reply, event="reply_drafted") or lead

    async def notify(self, lead: LeadRecord) -> LeadRecord:
        """Send to the manager. A failed notification never loses the lead - it is logged instead."""
        if not self.notifier:
            return lead
        try:
            message_id = await self.notifier(lead)
        except Exception as e:  # noqa: BLE001 - any delivery failure must not break intake
            log.error("Notification for %s failed: %s", lead.id, e)
            self.storage.update_lead(lead.id, event="notify_failed")
            return lead
        return self.storage.update_lead(lead.id, telegram_message_id=message_id, event="manager_notified") or lead

    # ----- manager actions (Telegram buttons) -----

    def confirm(self, lead_id: str, by: str) -> LeadRecord | None:
        return self.storage.update_lead(lead_id, status=LeadStatus.CONFIRMED, event=f"confirmed by {by}")

    def call_first(self, lead_id: str, by: str) -> LeadRecord | None:
        return self.storage.update_lead(lead_id, status=LeadStatus.CALL_FIRST, event=f"call_first by {by}")

    def reassign(self, lead_id: str, by: str) -> tuple[LeadRecord, MatchResult] | None:
        """Offer the next-best contractor, skipping everyone already tried for this lead."""
        lead = self.storage.get_lead(lead_id)
        if not lead:
            return None
        match = self.directory.match(lead.extraction, exclude_ids=set(lead.tried_contractors))
        if match.contractor:
            updated = self.storage.update_lead(
                lead_id, event=f"reassigned by {by}",
                status=LeadStatus.MATCHED,
                contractor_id=match.contractor.id,
                contractor_name=match.contractor.name,
                match_reason=match.reason,
                tried_contractors=[*lead.tried_contractors, match.contractor.id],
            )
        else:
            updated = self.storage.update_lead(
                lead_id, event=f"reassign by {by}: no candidates",
                status=LeadStatus.NEEDS_MANUAL_ASSIGNMENT,
                contractor_id=None,
                contractor_name=None,
                match_reason=match.reason,
            )
        return updated, match

"""Storage interface so SQLite (default) and Google Sheets (optional) are interchangeable."""
from abc import ABC, abstractmethod

from ..models import LeadRecord, LeadStatus


class LeadStorage(ABC):
    @abstractmethod
    def save_lead(self, lead: LeadRecord) -> None: ...

    @abstractmethod
    def update_lead(self, lead_id: str, *, event: str | None = None, **fields) -> LeadRecord | None:
        """Update top-level fields (status, contractor_id, draft_reply, ...) and log an event."""

    @abstractmethod
    def get_lead(self, lead_id: str) -> LeadRecord | None: ...

    @abstractmethod
    def list_leads(self, limit: int = 50, status: LeadStatus | None = None) -> list[LeadRecord]: ...

    @abstractmethod
    def get_events(self, lead_id: str) -> list[dict]:
        """Audit trail for a lead: [{"at", "event", "detail"}, ...]."""

    def close(self) -> None:
        """Flush/release resources on shutdown (no-op by default)."""

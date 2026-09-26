"""Optional Google Sheets mirror (gspread + service account).

SQLite stays the source of truth; every saved/updated lead is upserted into a "Leads" worksheet
so managers can browse, filter and share leads without touching the database.

Writes run on a background thread and are coalesced per lead, so a slow or failing Sheets API
never delays intake or Telegram buttons, and bursts stay within the 60 writes/min quota.
"""
import logging
import queue
import threading
import time
from typing import Iterable

from ..models import LeadRecord, LeadStatus
from .base import LeadStorage

log = logging.getLogger(__name__)

WORKSHEET = "Leads"
HEADER = [
    "Lead ID", "Created (UTC)", "Updated (UTC)", "Status", "Urgency", "Service", "City", "State",
    "Customer", "Phone", "Email", "Address", "Problem", "Missing info", "Preferred time",
    "Contractor", "Match reason", "Draft reply", "Source", "Language", "Confidence",
]
_STOP = object()


def lead_to_row(lead: LeadRecord) -> list[str]:
    x = lead.extraction
    return [
        lead.id, lead.created_at, lead.updated_at, lead.status.value, x.urgency.value, x.service_category.value,
        x.city or "", x.state or "", x.customer_name or "", x.phone or "", x.email or "", x.address or "",
        x.problem_summary, ", ".join(x.missing_info), x.preferred_time or "",
        lead.contractor_name or "", lead.match_reason, lead.draft_reply or "", lead.source,
        x.language_detected, f"{x.confidence:.2f}",
    ]


def _column_letter(n: int) -> str:
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


LAST_COL = _column_letter(len(HEADER))


def open_worksheet(credentials_file: str, sheet_id: str):
    """Open (or create) the Leads worksheet and make sure the header row is in place."""
    import gspread  # optional dependency, only needed when Sheets is enabled

    spreadsheet = gspread.service_account(filename=credentials_file).open_by_key(sheet_id)
    try:
        ws = spreadsheet.worksheet(WORKSHEET)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(WORKSHEET, rows=1000, cols=len(HEADER))
    if ws.row_values(1) != HEADER:
        ws.update(values=[HEADER], range_name=f"A1:{LAST_COL}1")
        ws.format(f"A1:{LAST_COL}1", {"textFormat": {"bold": True}})
        ws.freeze(rows=1)
    return ws


class SheetsMirror:
    """Background upserter. `worksheet` is a gspread Worksheet (or a test double with the same methods)."""

    def __init__(self, worksheet, debounce_s: float = 1.5):
        self.ws = worksheet
        self.debounce_s = debounce_s
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="sheets-mirror", daemon=True)
        self._thread.start()

    def push(self, lead: LeadRecord) -> None:
        self._queue.put(lead)

    def close(self, timeout: float = 30) -> None:
        """Flush pending writes and stop the worker."""
        self._queue.put(_STOP)
        self._thread.join(timeout)

    def full_sync(self, leads: Iterable[LeadRecord]) -> int:
        """Rewrite the whole sheet from the database (one API call). Returns row count."""
        rows = [lead_to_row(l) for l in sorted(leads, key=lambda l: l.created_at)]
        self.ws.batch_clear([f"A2:{LAST_COL}"])
        if rows:
            self.ws.update(values=rows, range_name=f"A2:{LAST_COL}{len(rows) + 1}", value_input_option="RAW")
        return len(rows)

    def _run(self) -> None:
        stop = False
        while not stop:
            item = self._queue.get()
            if item is _STOP:
                break
            batch = {item.id: item}
            time.sleep(self.debounce_s)  # let the create -> reply -> notified updates of one lead coalesce
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is _STOP:
                    stop = True
                    break
                batch[item.id] = item  # keep only the latest state per lead
            self._flush(list(batch.values()))

    def _flush(self, leads: list[LeadRecord], attempt: int = 1) -> None:
        try:
            # Re-read the ID column every batch so manual sorting/filtering in the sheet can't break upserts
            ids = self.ws.col_values(1)
            updates, appends = [], []
            for lead in leads:
                row = lead_to_row(lead)
                if lead.id in ids:
                    n = ids.index(lead.id) + 1
                    updates.append({"range": f"A{n}:{LAST_COL}{n}", "values": [row]})
                else:
                    appends.append(row)
            if updates:
                self.ws.batch_update(updates, value_input_option="RAW")
            if appends:
                self.ws.append_rows(appends, value_input_option="RAW", table_range="A1")
            log.info("Sheets: %d updated, %d appended", len(updates), len(appends))
        except Exception as e:  # noqa: BLE001 - the mirror must never take the app down
            if attempt < 3 and "429" in str(e):
                log.warning("Sheets rate limit, retrying in %ds", 20 * attempt)
                time.sleep(20 * attempt)
                return self._flush(leads, attempt + 1)
            log.error("Sheets sync failed for %s: %s", [l.id for l in leads], e)


class MirroredStorage(LeadStorage):
    """Primary storage + Sheets mirror. Reads always come from the primary."""

    def __init__(self, primary: LeadStorage, mirror: SheetsMirror):
        self.primary = primary
        self.mirror = mirror

    def save_lead(self, lead: LeadRecord) -> None:
        self.primary.save_lead(lead)
        self.mirror.push(lead)

    def update_lead(self, lead_id: str, *, event: str | None = None, **fields) -> LeadRecord | None:
        updated = self.primary.update_lead(lead_id, event=event, **fields)
        if updated:
            self.mirror.push(updated)
        return updated

    def get_lead(self, lead_id: str) -> LeadRecord | None:
        return self.primary.get_lead(lead_id)

    def list_leads(self, limit: int = 50, status: LeadStatus | None = None) -> list[LeadRecord]:
        return self.primary.list_leads(limit, status)

    def get_events(self, lead_id: str) -> list[dict]:
        return self.primary.get_events(lead_id)

    def close(self) -> None:
        self.mirror.close()

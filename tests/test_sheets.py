"""Sheets mirror tests against an in-memory worksheet double (no Google API calls)."""
import re

from app.config import Settings
from app.models import LeadExtraction, LeadRecord, LeadStatus
from app.storage import SQLiteStorage, build_storage, now_iso
from app.storage.sheets_store import HEADER, MirroredStorage, SheetsMirror, lead_to_row


class FakeWorksheet:
    def __init__(self):
        self.rows = [list(HEADER)]
        self.calls = []

    def col_values(self, col):
        return [r[col - 1] for r in self.rows]

    def batch_update(self, data, value_input_option=None):
        self.calls.append(("batch_update", value_input_option))
        for item in data:
            n = int(re.match(r"A(\d+):", item["range"]).group(1))
            self.rows[n - 1] = item["values"][0]

    def append_rows(self, rows, value_input_option=None, table_range=None):
        self.calls.append(("append_rows", value_input_option))
        self.rows.extend(rows)

    def batch_clear(self, ranges):
        self.rows = self.rows[:1]

    def update(self, values, range_name, value_input_option=None):
        self.rows.extend(values)


def make_lead(lead_id="LD-1", phone="713-555-0142"):
    ts = now_iso()
    return LeadRecord(
        id=lead_id, created_at=ts, updated_at=ts, source="sms", raw_text="x",
        extraction=LeadExtraction(problem_summary="Leak", city="Houston", phone=phone, missing_info=["address"]),
        status=LeadStatus.MATCHED, contractor_name="Bayou City Plumbing Co",
    )


def test_row_matches_header():
    row = lead_to_row(make_lead())
    assert len(row) == len(HEADER)
    assert row[HEADER.index("Missing info")] == "address"


def test_create_and_updates_coalesce_into_one_row(tmp_path):
    ws = FakeWorksheet()
    storage = MirroredStorage(SQLiteStorage(tmp_path / "t.db"), SheetsMirror(ws, debounce_s=0.2))
    storage.save_lead(make_lead())
    storage.update_lead("LD-1", draft_reply="Hi!", event="reply_drafted")
    storage.update_lead("LD-1", status=LeadStatus.CONFIRMED, event="confirmed")
    storage.close()

    assert len(ws.rows) == 2                      # header + one lead, not three
    row = dict(zip(HEADER, ws.rows[1]))
    assert row["Status"] == "confirmed" and row["Draft reply"] == "Hi!"
    assert ws.calls == [("append_rows", "RAW")]  # one write for the whole burst, formulas never evaluated


def test_existing_row_is_updated_in_place_even_after_manual_sort(tmp_path):
    ws = FakeWorksheet()
    ws.rows += [lead_to_row(make_lead("LD-2")), lead_to_row(make_lead("LD-1"))]   # someone sorted the sheet
    mirror = SheetsMirror(ws, debounce_s=0)
    lead = make_lead("LD-1")
    lead.status = LeadStatus.CALL_FIRST
    mirror.push(lead)
    mirror.close()
    assert [r[0] for r in ws.rows] == ["Lead ID", "LD-2", "LD-1"]
    assert ws.rows[2][HEADER.index("Status")] == "call_first"


def test_sheets_errors_never_break_the_app(tmp_path):
    class BrokenWorksheet(FakeWorksheet):
        def col_values(self, col):
            raise RuntimeError("403 The caller does not have permission")

    storage = MirroredStorage(SQLiteStorage(tmp_path / "t.db"), SheetsMirror(BrokenWorksheet(), debounce_s=0))
    storage.save_lead(make_lead())
    storage.close()
    assert storage.get_lead("LD-1") is not None


def test_full_sync(tmp_path):
    ws = FakeWorksheet()
    ws.rows.append(["stale"] * len(HEADER))
    count = SheetsMirror(ws, debounce_s=0).full_sync([make_lead("LD-2"), make_lead("LD-1")])
    assert count == 2 and len(ws.rows) == 3 and "stale" not in ws.rows[1]


def test_sheets_disabled_without_config(tmp_path):
    base = dict(_env_file=None, database_path=str(tmp_path / "a.db"))
    assert isinstance(build_storage(Settings(**base)), SQLiteStorage)
    missing = Settings(**base, google_sheet_id="abc", google_sheets_credentials_file="credentials/nope.json")
    assert isinstance(build_storage(missing), SQLiteStorage)


def test_sheet_id_accepts_url_and_rejects_email():
    from app.storage import _sheet_id
    assert _sheet_id("https://docs.google.com/spreadsheets/d/1AbC_x-9/edit#gid=0") == "1AbC_x-9"
    assert _sheet_id(" 1AbC_x-9 ") == "1AbC_x-9"
    assert _sheet_id("bot@proj.iam.gserviceaccount.com") is None

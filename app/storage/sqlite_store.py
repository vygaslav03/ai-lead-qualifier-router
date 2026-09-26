"""SQLite storage (stdlib, zero setup). Key fields are flattened into columns for easy querying;
the full extraction is kept as JSON. A lead_events table keeps an audit trail with timestamps."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..models import LeadExtraction, LeadRecord, LeadStatus
from .base import LeadStorage

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id                  TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    source              TEXT NOT NULL,
    raw_text            TEXT NOT NULL,
    customer_name       TEXT,
    phone               TEXT,
    email               TEXT,
    address             TEXT,
    city                TEXT,
    state               TEXT,
    service_category    TEXT NOT NULL,
    urgency             TEXT NOT NULL,
    problem_summary     TEXT NOT NULL,
    missing_info        TEXT NOT NULL,          -- JSON list
    confidence          REAL NOT NULL,
    status              TEXT NOT NULL,
    contractor_id       TEXT,
    contractor_name     TEXT,
    match_reason        TEXT NOT NULL DEFAULT '',
    tried_contractors   TEXT NOT NULL DEFAULT '[]', -- JSON list
    draft_reply         TEXT,
    telegram_message_id INTEGER,
    extraction_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);

CREATE TABLE IF NOT EXISTS lead_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id  TEXT NOT NULL REFERENCES leads(id),
    at       TEXT NOT NULL,
    event    TEXT NOT NULL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON lead_events(lead_id);
"""

UPDATABLE = {
    "status", "contractor_id", "contractor_name", "match_reason", "tried_contractors",
    "draft_reply", "telegram_message_id",
}

# Columns added after the first release: (name, DDL) applied to older databases on startup
MIGRATIONS = [
    ("tried_contractors", "ALTER TABLE leads ADD COLUMN tried_contractors TEXT NOT NULL DEFAULT '[]'"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteStorage(LeadStorage):
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(leads)")}
            for column, ddl in MIGRATIONS:
                if column not in existing:
                    conn.execute(ddl)

    @contextmanager
    def _conn(self):
        # One short-lived connection per operation: safe across FastAPI threads and the bot task
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save_lead(self, lead: LeadRecord) -> None:
        ex = lead.extraction
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO leads (id, created_at, updated_at, source, raw_text, customer_name, phone, email,
                       address, city, state, service_category, urgency, problem_summary, missing_info, confidence,
                       status, contractor_id, contractor_name, match_reason, tried_contractors, draft_reply,
                       telegram_message_id, extraction_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lead.id, lead.created_at, lead.updated_at, lead.source, lead.raw_text,
                    ex.customer_name, ex.phone, ex.email, ex.address, ex.city, ex.state,
                    ex.service_category.value, ex.urgency.value, ex.problem_summary,
                    json.dumps(ex.missing_info), ex.confidence,
                    lead.status.value, lead.contractor_id, lead.contractor_name, lead.match_reason,
                    json.dumps(lead.tried_contractors), lead.draft_reply, lead.telegram_message_id, ex.model_dump_json(),
                ),
            )
            self._log(conn, lead.id, "created", f"{lead.status.value}: {lead.match_reason}")

    def update_lead(self, lead_id: str, *, event: str | None = None, **fields) -> LeadRecord | None:
        unknown = set(fields) - UPDATABLE
        if unknown:
            raise ValueError(f"Cannot update fields: {unknown}")
        values = {
            k: v.value if isinstance(v, LeadStatus) else json.dumps(v) if isinstance(v, list) else v
            for k, v in fields.items()
        }
        values["updated_at"] = now_iso()
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE leads SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
                (*values.values(), lead_id),
            )
            if cur.rowcount == 0:
                return None
            if event:
                detail = ", ".join(f"{k}={v}" for k, v in values.items() if k not in {"updated_at", "draft_reply"})
                self._log(conn, lead_id, event, detail)
        return self.get_lead(lead_id)

    def get_lead(self, lead_id: str) -> LeadRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return self._to_record(row) if row else None

    def list_leads(self, limit: int = 50, status: LeadStatus | None = None) -> list[LeadRecord]:
        sql, params = "SELECT * FROM leads", []
        if status:
            sql += " WHERE status = ?"
            params.append(status.value)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [self._to_record(r) for r in conn.execute(sql, params).fetchall()]

    def get_events(self, lead_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT at, event, detail FROM lead_events WHERE lead_id = ? ORDER BY id", (lead_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _log(conn: sqlite3.Connection, lead_id: str, event: str, detail: str | None = None) -> None:
        conn.execute(
            "INSERT INTO lead_events (lead_id, at, event, detail) VALUES (?,?,?,?)",
            (lead_id, now_iso(), event, detail),
        )

    @staticmethod
    def _to_record(row: sqlite3.Row) -> LeadRecord:
        return LeadRecord(
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            source=row["source"],
            raw_text=row["raw_text"],
            extraction=LeadExtraction.model_validate_json(row["extraction_json"]),
            status=LeadStatus(row["status"]),
            contractor_id=row["contractor_id"],
            contractor_name=row["contractor_name"],
            match_reason=row["match_reason"],
            tried_contractors=json.loads(row["tried_contractors"]),
            draft_reply=row["draft_reply"],
            telegram_message_id=row["telegram_message_id"],
        )

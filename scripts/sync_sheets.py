"""Backfill / repair: rewrite the Google Sheet from the SQLite database.

    python scripts/sync_sheets.py

Live changes are mirrored automatically while the app runs; use this for leads created
before Sheets was enabled, or after editing the sheet by hand.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app.config import get_settings  # noqa: E402
from app.storage import SQLiteStorage, sheets_config  # noqa: E402
from app.storage.sheets_store import SheetsMirror, open_worksheet  # noqa: E402


def main() -> None:
    settings = get_settings()
    config = sheets_config(settings)
    if not config:
        sys.exit("Google Sheets is not configured: set GOOGLE_SHEETS_CREDENTIALS_FILE and GOOGLE_SHEET_ID in .env")
    worksheet = open_worksheet(*config)
    leads = SQLiteStorage(settings.database_path).list_leads(limit=100_000)
    mirror = SheetsMirror(worksheet)
    count = mirror.full_sync(leads)
    mirror.close()
    print(f"Synced {count} leads to {worksheet.url}")


if __name__ == "__main__":
    main()

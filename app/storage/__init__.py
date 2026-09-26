import logging
import re
from pathlib import Path

from ..config import BASE_DIR, Settings
from .base import LeadStorage
from .sqlite_store import SQLiteStorage, now_iso

log = logging.getLogger(__name__)

__all__ = ["LeadStorage", "SQLiteStorage", "build_storage", "sheets_config", "now_iso"]


def _sheet_id(value: str) -> str | None:
    """Accept a bare sheet id or a full sheet URL; reject obvious mix-ups."""
    value = value.strip().strip("\"'")
    if "@" in value:
        log.warning("Google Sheets disabled: GOOGLE_SHEET_ID looks like an email. Put the service account "
                    "email in the sheet's Share dialog, and the id from the sheet URL (/d/<ID>/edit) in .env")
        return None
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    return match.group(1) if match else value


def sheets_config(settings: Settings) -> tuple[str, str] | None:
    """(credentials path, sheet id) if Google Sheets is fully configured, else None."""
    if not (settings.google_sheets_credentials_file and settings.google_sheet_id):
        return None
    sheet_id = _sheet_id(settings.google_sheet_id)
    if not sheet_id:
        return None
    path = Path(settings.google_sheets_credentials_file)
    path = path if path.is_absolute() else BASE_DIR / path
    if not path.exists():
        log.warning("Google Sheets disabled: credentials file not found at %s", path)
        return None
    return str(path), sheet_id


def build_storage(settings: Settings) -> LeadStorage:
    """SQLite, plus a Google Sheets mirror when credentials are configured."""
    sqlite = SQLiteStorage(settings.database_path)
    config = sheets_config(settings)
    if not config:
        return sqlite
    try:
        from .sheets_store import MirroredStorage, SheetsMirror, open_worksheet

        worksheet = open_worksheet(*config)
    except Exception as e:  # noqa: BLE001 - bad credentials/sharing must not stop the app
        log.error("Google Sheets disabled: %s: %s", type(e).__name__, e)
        return sqlite
    log.info("Google Sheets sync enabled: %s", worksheet.url)
    return MirroredStorage(sqlite, SheetsMirror(worksheet))

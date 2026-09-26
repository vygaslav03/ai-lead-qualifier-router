"""Single entry point: python main.py  -> FastAPI (form + API) and Telegram long polling in one process."""
import logging
import sys

import uvicorn

from app.api import create_app
from app.config import get_settings

sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
# httpx logs full request URLs at INFO - Telegram URLs contain the bot token, and polling is noisy
logging.getLogger("httpx").setLevel(logging.WARNING)

app = create_app()

if __name__ == "__main__":
    s = get_settings()
    print(f"\n  Lead form: http://{s.host}:{s.port}/   API docs: http://{s.host}:{s.port}/docs\n")
    uvicorn.run(app, host=s.host, port=s.port, log_level="info")

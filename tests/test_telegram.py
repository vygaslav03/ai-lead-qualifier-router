"""Bot logic tests with a stubbed Telegram API (no network)."""
import asyncio

import pytest

from app.config import DATA_DIR
from app.matcher import ContractorDirectory
from app.models import LeadExtraction, LeadStatus
from app.pipeline import LeadPipeline
from app.storage import SQLiteStorage
from app.telegram_bot import TelegramBot, format_lead_message, lead_keyboard

MANAGER = 111


@pytest.fixture
def setup(tmp_path):
    pipeline = LeadPipeline(None, ContractorDirectory(DATA_DIR / "contractors.csv"), SQLiteStorage(tmp_path / "t.db"))
    bot = TelegramBot("123:fake", str(MANAGER), pipeline)
    calls = []

    async def fake_call(method, **params):
        calls.append((method, params))
        return {"message_id": 42} if method == "sendMessage" else True

    bot._call = fake_call
    return pipeline, bot, calls


def make_lead(pipeline, city="Houston", category="plumbing"):
    ex = LeadExtraction(
        customer_name="Mark <script>", phone="713-555-0142", city=city, state="TX", service_category=category,
        problem_summary="Leak", urgency="emergency", urgency_reason="Active leak", missing_info=["address"],
    )
    lead, _ = pipeline.route(ex, "raw", "sms")
    return lead


def click(bot, action, lead_id, chat_id=MANAGER):
    query = {"id": "q1", "data": f"{action}:{lead_id}", "from": {"username": "anna"},
             "message": {"message_id": 42, "chat": {"id": chat_id}}}
    asyncio.run(bot._on_button(query))


def test_message_format_and_escaping(setup):
    pipeline, _, _ = setup
    text = format_lead_message(make_lead(pipeline), pipeline.directory)
    assert text.startswith("🔴 <b>EMERGENCY</b> · Plumbing · Houston, TX")
    assert "Mark &lt;script&gt;" in text
    assert "⚠️ <b>Missing:</b> address" in text
    assert "Bayou City Plumbing Co (4.8★)" in text


def test_keyboard_depends_on_state(setup):
    pipeline, _, _ = setup
    lead = make_lead(pipeline)
    assert [b["text"] for b in lead_keyboard(lead)["inline_keyboard"][0]] == ["✅ Confirm", "🔄 Reassign", "📞 Call first"]
    manual = make_lead(pipeline, city="Austin")
    assert [b["text"] for b in lead_keyboard(manual)["inline_keyboard"][0]] == ["🔄 Reassign", "📞 Call first"]


def test_notify_stores_message_id(setup):
    pipeline, bot, calls = setup
    pipeline.notifier = bot.send_lead
    lead = asyncio.run(pipeline.notify(make_lead(pipeline)))
    assert lead.telegram_message_id == 42
    assert calls[0][1]["reply_markup"]["inline_keyboard"]


def test_confirm_updates_status_and_removes_buttons(setup):
    pipeline, bot, calls = setup
    lead = make_lead(pipeline)
    click(bot, "confirm", lead.id)
    assert pipeline.storage.get_lead(lead.id).status == LeadStatus.CONFIRMED
    edit = next(p for m, p in calls if m == "editMessageText")
    assert "reply_markup" not in edit and "Confirmed</b> by anna" in edit["text"]


def test_reassign_walks_through_candidates(setup):
    pipeline, bot, _ = setup
    lead = make_lead(pipeline)                      # Houston plumbing: C001 then C004
    click(bot, "reassign", lead.id)
    assert pipeline.storage.get_lead(lead.id).contractor_id == "C004"
    click(bot, "reassign", lead.id)
    after = pipeline.storage.get_lead(lead.id)
    assert after.status == LeadStatus.NEEDS_MANUAL_ASSIGNMENT and after.contractor_id is None


def test_call_first_and_foreign_chat_rejected(setup):
    pipeline, bot, calls = setup
    lead = make_lead(pipeline)
    click(bot, "call", lead.id, chat_id=999)
    assert pipeline.storage.get_lead(lead.id).status == LeadStatus.MATCHED
    assert calls[-1] == ("answerCallbackQuery", {"callback_query_id": "q1", "text": "Not allowed"})
    click(bot, "call", lead.id)
    assert pipeline.storage.get_lead(lead.id).status == LeadStatus.CALL_FIRST


def test_failed_notification_keeps_lead(setup):
    pipeline, _, _ = setup

    async def broken(_lead):
        raise RuntimeError("telegram down")

    pipeline.notifier = broken
    lead = asyncio.run(pipeline.notify(make_lead(pipeline)))
    assert pipeline.storage.get_lead(lead.id) is not None
    assert pipeline.storage.get_events(lead.id)[-1]["event"] == "notify_failed"

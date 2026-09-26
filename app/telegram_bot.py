"""Telegram Bot API over plain httpx, using long polling (no webhook, no public URL needed).

- send_lead(): posts a lead card with inline buttons to the manager chat
- run_polling(): getUpdates loop handling button clicks and a couple of commands
"""
import asyncio
import html
import logging
from datetime import datetime

import httpx

from .matcher import ContractorDirectory
from .models import LeadRecord, LeadStatus
from .pipeline import LeadPipeline
from .reply import LANGUAGE_NAMES

log = logging.getLogger(__name__)

URGENCY = {
    "emergency": ("🔴", "EMERGENCY"),
    "within_48h": ("🟠", "Within 48h"),
    "this_week": ("🟡", "This week"),
    "flexible": ("🟢", "Flexible"),
}
SOURCE = {"web_form": "web form", "email": "email", "sms": "SMS"}
POLL_TIMEOUT = 30  # seconds Telegram holds a getUpdates request open


def _e(value) -> str:
    return html.escape(str(value), quote=False)


def _label(value: str) -> str:
    return value.replace("_", " ").capitalize()


def format_lead_message(lead: LeadRecord, directory: ContractorDirectory, status_line: str | None = None) -> str:
    x = lead.extraction
    icon, urgency = URGENCY[x.urgency.value]
    where = ", ".join(_e(v) for v in (x.city, x.state) if v) or "City unknown"
    lines = [
        f"{icon} <b>{urgency}</b> · {_e(_label(x.service_category.value))} · {where}",
        f"<code>{_e(lead.id)}</code> · via {SOURCE.get(lead.source, _e(lead.source))}"
        + (f" · lang: {_e(x.language_detected)}" if x.language_detected != "en" else ""),
        "",
        f"<b>{_e(x.problem_summary)}</b>",
        f"<i>{_e(x.urgency_reason)}</i>",
        "",
    ]
    contact = [
        ("👤", x.customer_name), ("📞", x.phone), ("✉️", x.email), ("📍", x.address), ("🕒", x.preferred_time),
    ]
    lines += [f"{emoji} {_e(value)}" for emoji, value in contact if value]
    if x.has_photos:
        lines.append("📷 Customer has photos")
    if x.missing_info:
        lines += ["", f"⚠️ <b>Missing:</b> {_e(', '.join(x.missing_info))}"]
    if x.confidence < 0.6:
        lines.append(f"🤔 Low extraction confidence ({x.confidence:.0%}) - check the original message")

    lines.append("")
    contractor = directory.get(lead.contractor_id) if lead.contractor_id else None
    if contractor:
        lines += [
            f"🛠 <b>Suggested:</b> {_e(contractor.name)} ({contractor.rating}★) · {_e(contractor.phone)}",
            f"<i>{_e(lead.match_reason)}</i>",
        ]
    else:
        lines.append(f"❗ <b>Needs manual assignment</b> - {_e(lead.match_reason)}")

    if lead.draft_reply:
        lang = "" if x.language_detected == "en" else f" (in {_e(LANGUAGE_NAMES.get(x.language_detected, x.language_detected))})"
        lines += ["", f"💬 <b>Draft reply to customer{lang}:</b>", f"<blockquote>{_e(lead.draft_reply)}</blockquote>"]

    lines += ["", status_line or "⏳ <b>Awaiting your decision</b>"]
    return "\n".join(lines)


def lead_keyboard(lead: LeadRecord) -> dict | None:
    if lead.status == LeadStatus.CONFIRMED:
        return None  # decision made - remove buttons
    row = []
    if lead.contractor_id:
        row.append({"text": "✅ Confirm", "callback_data": f"confirm:{lead.id}"})
    row.append({"text": "🔄 Reassign", "callback_data": f"reassign:{lead.id}"})
    if lead.status != LeadStatus.CALL_FIRST:
        row.append({"text": "📞 Call first", "callback_data": f"call:{lead.id}"})
    return {"inline_keyboard": [row]}


class TelegramError(Exception):
    pass


class TelegramBot:
    def __init__(self, token: str, manager_chat_id: str, pipeline: LeadPipeline):
        self.manager_chat_id = int(manager_chat_id)
        self.pipeline = pipeline
        self._base = f"https://api.telegram.org/bot{token}/"
        self._client = httpx.AsyncClient(timeout=POLL_TIMEOUT + 10)
        self._offset = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, **params) -> dict | list | bool:
        for attempt in range(3):
            response = await self._client.post(self._base + method, json=params)
            data = response.json()
            if data.get("ok"):
                return data["result"]
            retry_after = data.get("parameters", {}).get("retry_after")
            if response.status_code == 429 and retry_after and attempt < 2:
                log.warning("Telegram rate limit, sleeping %ss", retry_after)
                await asyncio.sleep(retry_after)
                continue
            raise TelegramError(f"{method}: {data.get('error_code')} {data.get('description')}")
        raise TelegramError(f"{method}: rate limited")

    # ----- outgoing -----

    async def send_lead(self, lead: LeadRecord) -> int:
        """Notifier used by the pipeline. Returns the Telegram message id."""
        message = await self._call(
            "sendMessage",
            chat_id=self.manager_chat_id,
            text=format_lead_message(lead, self.pipeline.directory),
            parse_mode="HTML",
            reply_markup=lead_keyboard(lead),
            link_preview_options={"is_disabled": True},
        )
        return message["message_id"]

    async def _refresh_card(self, lead: LeadRecord, message_id: int, status_line: str) -> None:
        params = dict(
            chat_id=self.manager_chat_id,
            message_id=message_id,
            text=format_lead_message(lead, self.pipeline.directory, status_line),
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
        )
        keyboard = lead_keyboard(lead)
        if keyboard:
            params["reply_markup"] = keyboard
        try:
            await self._call("editMessageText", **params)
        except TelegramError as e:
            if "message is not modified" not in str(e):
                raise

    # ----- incoming -----

    async def run_polling(self) -> None:
        """Long-polling loop. Runs as a background task next to FastAPI."""
        await self._call("deleteWebhook")  # getUpdates doesn't work while a webhook is set
        me = await self._call("getMe")
        log.info("Telegram bot @%s polling for updates", me["username"])
        backoff = 1
        while True:
            try:
                updates = await self._call(
                    "getUpdates", offset=self._offset, timeout=POLL_TIMEOUT,
                    allowed_updates=["message", "callback_query", "my_chat_member"],
                )
                backoff = 1
                for update in updates:
                    self._offset = update["update_id"] + 1
                    try:
                        await self._handle_update(update)
                    except Exception:  # noqa: BLE001 - one bad update must not stop the bot
                        log.exception("Failed to handle update %s", update.get("update_id"))
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, TelegramError) as e:
                log.warning("Polling error: %s (retry in %ss)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self._on_button(update["callback_query"])
        elif "message" in update:
            await self._on_message(update["message"])
        elif "my_chat_member" in update:
            await self._on_membership(update["my_chat_member"])

    def _describe_chat(self, chat_id: int) -> str:
        return ("✅ This is the manager chat - new leads will appear here."
                if chat_id == self.manager_chat_id
                else "⚠️ This chat is not the manager chat. To receive leads here, set "
                     f"TELEGRAM_MANAGER_CHAT_ID={chat_id} in .env and restart.")

    async def _on_membership(self, update: dict) -> None:
        """Bot was added to a group: tell the admins the chat id to configure."""
        chat = update["chat"]
        if update["new_chat_member"]["status"] in {"member", "administrator"}:
            log.info("Bot added to chat %s (%s)", chat["id"], chat.get("title"))
            await self._call("sendMessage", chat_id=chat["id"], parse_mode="HTML",
                             text=f"👋 Lead router bot here.\nChat id: <code>{chat['id']}</code>\n"
                                  f"{self._describe_chat(chat['id'])}")

    async def _on_message(self, message: dict) -> None:
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        if chat_id != self.manager_chat_id:
            log.info("Message in chat %s (%s) - not the manager chat", chat_id, message["chat"].get("title", "private"))
        if message.get("migrate_to_chat_id"):
            log.warning("Chat %s was upgraded to supergroup %s - update TELEGRAM_MANAGER_CHAT_ID",
                        chat_id, message["migrate_to_chat_id"])
        if text.startswith("/start"):
            await self._call("sendMessage", chat_id=chat_id, parse_mode="HTML",
                             text=f"👋 Lead router bot.\nChat id: <code>{chat_id}</code>\n{self._describe_chat(chat_id)}")
        elif text.startswith("/pending") and chat_id == self.manager_chat_id:
            await self._send_pending()

    async def _send_pending(self) -> None:
        storage = self.pipeline.storage
        pending = [
            l for status in (LeadStatus.NEEDS_MANUAL_ASSIGNMENT, LeadStatus.MATCHED, LeadStatus.CALL_FIRST)
            for l in storage.list_leads(limit=20, status=status)
        ]
        if not pending:
            text = "✅ Nothing pending."
        else:
            text = "<b>Pending leads</b>\n" + "\n".join(
                f"{URGENCY[l.extraction.urgency.value][0]} <code>{_e(l.id)}</code> "
                f"{_e(_label(l.extraction.service_category.value))}, {_e(l.extraction.city or '?')} - "
                f"{_e(_label(l.status.value))}"
                for l in pending
            )
        await self._call("sendMessage", chat_id=self.manager_chat_id, text=text, parse_mode="HTML")

    async def _on_button(self, query: dict) -> None:
        message = query.get("message") or {}
        if message.get("chat", {}).get("id") != self.manager_chat_id:
            await self._call("answerCallbackQuery", callback_query_id=query["id"], text="Not allowed")
            return

        action, _, lead_id = (query.get("data") or "").partition(":")
        user = query.get("from", {})
        by = user.get("username") or user.get("first_name") or "manager"
        at = datetime.now().strftime("%H:%M")
        lead = self.pipeline.storage.get_lead(lead_id)
        if not lead:
            await self._call("answerCallbackQuery", callback_query_id=query["id"], text="Lead not found")
            return

        if action == "confirm":
            if lead.status == LeadStatus.CONFIRMED:
                toast, status_line = "Already confirmed", None
            else:
                lead = self.pipeline.confirm(lead_id, by)
                toast = f"Confirmed: {lead.contractor_name}"
                status_line = f"✅ <b>Confirmed</b> by {_e(by)} at {at} - dispatch {_e(lead.contractor_name)}"
        elif action == "call":
            lead = self.pipeline.call_first(lead_id, by)
            toast = "Marked: call customer first"
            status_line = f"📞 <b>{_e(by)} will call the customer first</b> ({at})"
        elif action == "reassign":
            previous = lead.contractor_name
            lead, match = self.pipeline.reassign(lead_id, by)
            if match.contractor:
                toast = f"Reassigned to {match.contractor.name}"
                status_line = f"🔄 Reassigned by {_e(by)} at {at}" + (f" (was {_e(previous)})" if previous else "")
            else:
                toast = "No other contractor available"
                status_line = f"🔄 {_e(by)} asked to reassign at {at} - no other candidates, assign manually"
        else:
            await self._call("answerCallbackQuery", callback_query_id=query["id"], text="Unknown action")
            return

        await self._call("answerCallbackQuery", callback_query_id=query["id"], text=toast)
        if status_line:
            await self._refresh_card(lead, message["message_id"], status_line)

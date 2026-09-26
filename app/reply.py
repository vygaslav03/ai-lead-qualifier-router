"""Draft SMS reply to the customer (second LLM call).

The facts (next step, what to ask for) are decided here in code; the LLM only writes the wording.
That keeps it from promising prices, ETAs or a contractor the manager hasn't confirmed yet."""
import logging
import re

from .llm import LLMClient, LLMError
from .models import LeadRecord, LeadStatus, Urgency

log = logging.getLogger(__name__)

MAX_SMS_CHARS = 320

LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French", "zh": "Chinese",
    "vi": "Vietnamese", "ko": "Korean", "ru": "Russian", "uk": "Ukrainian", "ar": "Arabic",
}

REPLY_SYSTEM = """\
You write SMS replies on behalf of a home repair dispatch office. Write ONE short, friendly, professional message in the language given as LANGUAGE.

Rules:
- At most 280 characters. Plain text only: no emojis, no markdown, no quotes around the message, no signature placeholders.
- Write the whole message in LANGUAGE, the language the customer wrote in (the facts below are in English - translate them). \
Use correct spelling with all accents, and the polite/formal form of address where the language has one \
(e.g. "usted" in Spanish).
- Greet the customer by first name if given, otherwise with a plain greeting.
- Briefly confirm what they need, in your own words (a few words, not the whole summary).
- State the NEXT STEP faithfully (translated if needed) - do not add times, prices, discounts or contractor names, \
and do not imply a visit is booked (no "until we arrive", "see you soon").
- If ASK FOR is not empty, ask for those details in one short question.
- Only if SAFETY LINE is "allowed", you may add one very short, obvious, generic safety line \
(e.g. keep the water shut off, keep the breaker off). Otherwise give no advice or tips at all.
- Never invent facts that are not in the input.
Output only the SMS text."""

NEXT_STEP = {
    Urgency.EMERGENCY: "We are treating this as urgent and a technician will call you shortly.",
    Urgency.WITHIN_48H: "We will call you to schedule a visit within the next day or two.",
    Urgency.THIS_WEEK: "We will contact you to schedule a visit this week.",
    Urgency.FLEXIBLE: "We will contact you to find a convenient time.",
}
NEXT_STEP_MANUAL = "A coordinator will call you to go over the options."

# missing_info keys -> how we ask for them in an SMS
ASK_PHRASES = {
    "address": "your street address",
    "city": "your city",
    "phone": "the best phone number to reach you",
    "email": "your email",
    "customer_name": "your name",
    "name": "your name",
    "problem details": "a few more details about the problem",
}


def _ask_for(lead: LeadRecord) -> list[str]:
    """Up to two most useful missing details, skipping the channel we're replying on."""
    skip = {"sms": {"phone"}, "email": {"email"}}.get(lead.source, set())
    has_contact = lead.extraction.phone or lead.extraction.email
    asks = []
    for item in lead.extraction.missing_info:
        key = item.strip().lower()
        if key in skip or (key == "email" and has_contact):
            continue
        phrase = ASK_PHRASES.get(key)
        if phrase and phrase not in asks:
            asks.append(phrase)
    return asks[:2]


def build_reply_facts(lead: LeadRecord, company_name: str = "") -> dict:
    x = lead.extraction
    first_name = x.customer_name.split()[0] if x.customer_name else None
    matched = lead.status != LeadStatus.NEEDS_MANUAL_ASSIGNMENT
    return {
        "first_name": first_name,
        "language": LANGUAGE_NAMES.get(x.language_detected.lower()[:2], x.language_detected or "English"),
        "company": company_name or None,
        "request": x.problem_summary,
        "city": x.city,
        "urgency": x.urgency.value,
        "next_step": NEXT_STEP[x.urgency] if matched else NEXT_STEP_MANUAL,
        "ask_for": _ask_for(lead),
        "safety_line": x.urgency == Urgency.EMERGENCY,
    }


def fallback_reply(facts: dict) -> str:
    """Template used when the LLM is unavailable or keeps exceeding the limit (English only)."""
    hi = f"Hi {facts['first_name']}," if facts["first_name"] else "Hi,"
    ask = f" Could you send {' and '.join(facts['ask_for'])}?" if facts["ask_for"] else ""
    text = f"{hi} thanks for contacting {facts['company'] or 'us'}. We got your request. {facts['next_step']}{ask}"
    return text[:MAX_SMS_CHARS]


_OPENING_Q_RE = re.compile(r"(^|[\s.,;:])\?(?=\w)")


def _clean(text: str, language: str = "English") -> str:
    text = " ".join(text.split())
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if language == "Spanish":
        # small models sometimes write "?podría" instead of "¿podría"
        text = _OPENING_Q_RE.sub(lambda m: m.group(1) + "¿", text)
    return text


async def draft_reply(lead: LeadRecord, llm: LLMClient, company_name: str = "") -> str:
    facts = build_reply_facts(lead, company_name)
    prompt = "\n".join([
        f"LANGUAGE: {facts['language']}",
        f"CUSTOMER FIRST NAME: {facts['first_name'] or 'unknown'}",
        f"COMPANY: {facts['company'] or '(do not mention a company name)'}",
        f"REQUEST: {facts['request']}",
        f"CITY: {facts['city'] or 'unknown'}",
        f"URGENCY: {facts['urgency']}",
        f"NEXT STEP: {facts['next_step']}",
        f"ASK FOR: {', '.join(facts['ask_for']) or 'nothing'}",
        f"SAFETY LINE: {'allowed' if facts['safety_line'] else 'not allowed'}",
    ])
    try:
        text = _clean(await llm.generate_text(REPLY_SYSTEM, prompt), facts["language"])
        if len(text) > MAX_SMS_CHARS:
            log.info("Reply too long (%d chars), asking to shorten", len(text))
            text = _clean(await llm.generate_text(
                REPLY_SYSTEM, f"{prompt}\n\nYour previous draft was {len(text)} characters. "
                              f"Rewrite it under 280 characters:\n{text}"), facts["language"])
        if text and len(text) <= MAX_SMS_CHARS:
            return text
        log.warning("Reply still invalid (%d chars), using template", len(text))
    except LLMError as e:
        log.warning("Reply generation failed, using template: %s", e)
    return fallback_reply(facts)

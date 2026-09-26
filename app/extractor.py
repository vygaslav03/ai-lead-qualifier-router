"""Turn free-text customer messages into a validated LeadExtraction."""
import logging
import re
from datetime import date

from pydantic import ValidationError

from .llm import InvalidJSONError, LLMClient
from .models import EXTRACTION_JSON_SCHEMA, LeadExtraction
from .prompts import EXTRACTION_RETRY_SUFFIX, EXTRACTION_SYSTEM, build_extraction_prompt

log = logging.getLogger(__name__)


class ExtractionError(Exception):
    pass


async def extract_lead(text: str, llm: LLMClient, source: str = "web_form") -> LeadExtraction:
    """LLM extraction + Pydantic validation, with one corrective retry on bad output."""
    prompt = build_extraction_prompt(text, source, date.today().isoformat())
    last_error: Exception | None = None

    for attempt in range(2):
        user = prompt if attempt == 0 else prompt + EXTRACTION_RETRY_SUFFIX.format(error=str(last_error)[:500])
        try:
            data = await llm.generate_json(EXTRACTION_SYSTEM, user, schema=EXTRACTION_JSON_SCHEMA)
            lead = LeadExtraction.model_validate(data)
            return _post_process(lead, text)
        except (InvalidJSONError, ValidationError) as e:
            last_error = e
            log.warning("Extraction attempt %d produced invalid output: %s", attempt + 1, str(e)[:200])

    raise ExtractionError(f"Could not extract lead after retry: {last_error}")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _post_process(lead: LeadExtraction, text: str) -> LeadExtraction:
    """Deterministic guardrails on top of the model output."""
    lowered = text.lower()

    # Anti-hallucination: contact details must literally appear in the source text.
    if lead.phone and (len(_digits(lead.phone)) < 7 or _digits(lead.phone)[-7:] not in _digits(text)):
        log.info("Dropping phone not found in source text: %s", lead.phone)
        lead.phone = None
    if lead.email and lead.email.lower() not in lowered:
        log.info("Dropping email not found in source text: %s", lead.email)
        lead.email = None

    # A city must be stated in the text (or implied by a ZIP code), never guessed from an area code.
    if lead.city and lead.city.lower() not in lowered and not re.search(r"\b\d{5}\b", text):
        log.info("Dropping city not found in source text: %s", lead.city)
        lead.city = lead.state = None
        lead.confidence = min(lead.confidence, 0.5)

    # Make sure the fields a dispatcher needs are flagged when missing.
    missing = list(lead.missing_info)
    have = {m.lower() for m in missing}

    def require(ok: bool, key: str, *aliases: str) -> None:
        if not ok and not any(a in m for m in have for a in (key, *aliases)):
            missing.append(key)

    require(bool(lead.phone or lead.email), "phone", "contact", "email")
    require(bool(lead.city), "city", "location")
    require(bool(lead.address), "address", "location")
    lead.missing_info = missing
    return lead

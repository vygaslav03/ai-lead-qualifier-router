import asyncio

from app.models import LeadExtraction, LeadRecord, LeadStatus
from app.reply import MAX_SMS_CHARS, build_reply_facts, draft_reply
from fakes import FakeLLM


def make_lead(source="web_form", status=LeadStatus.MATCHED, **ex):
    fields = dict(problem_summary="Water heater is leaking.", urgency="within_48h", customer_name="Rosa Martinez",
                  phone="214-555-0177", missing_info=["address"])
    fields.update(ex)
    return LeadRecord(id="LD-1", created_at="t", updated_at="t", source=source, raw_text="x",
                      extraction=LeadExtraction(**fields), status=status)


def run(coro):
    return asyncio.run(coro)


def test_facts_decided_in_code():
    facts = build_reply_facts(make_lead())
    assert facts["first_name"] == "Rosa"
    assert facts["next_step"] == "We will call you to schedule a visit within the next day or two."
    assert facts["ask_for"] == ["your street address"]


def test_reply_language_follows_customer():
    assert build_reply_facts(make_lead(language_detected="es"))["language"] == "Spanish"
    assert build_reply_facts(make_lead())["language"] == "English"


def test_manual_assignment_does_not_promise_a_visit():
    facts = build_reply_facts(make_lead(status=LeadStatus.NEEDS_MANUAL_ASSIGNMENT))
    assert facts["next_step"] == "A coordinator will call you to go over the options."


def test_does_not_ask_for_the_channel_we_reply_on():
    lead = make_lead(source="sms", phone=None, missing_info=["phone", "address", "city", "email"])
    assert build_reply_facts(lead)["ask_for"] == ["your street address", "your city"]   # max two, no phone


def test_llm_reply_is_cleaned():
    llm = FakeLLM(['  "Hi Rosa, thanks for reaching out.\n We will call you soon."  '])
    assert run(draft_reply(make_lead(), llm)) == "Hi Rosa, thanks for reaching out. We will call you soon."


def test_too_long_reply_is_shortened_once():
    llm = FakeLLM(["x" * 400, "Hi Rosa, we will call you to schedule a visit."])
    assert run(draft_reply(make_lead(), llm)) == "Hi Rosa, we will call you to schedule a visit."
    assert "Rewrite it under 280 characters" in llm.prompts[1]


def test_falls_back_to_template():
    for llm in (FakeLLM([]), FakeLLM(["x" * 400, "y" * 400])):
        text = run(draft_reply(make_lead(), llm, company_name="Acme Repairs"))
        assert text.startswith("Hi Rosa, thanks for contacting Acme Repairs.")
        assert "your street address" in text and len(text) <= MAX_SMS_CHARS


def test_spanish_opening_question_mark_is_fixed():
    llm = FakeLLM(["Hola Rosa, le llamaremos. Para avanzar, ?podría indicarnos su dirección? Gracias."])
    text = run(draft_reply(make_lead(language_detected="es"), llm))
    assert "¿podría indicarnos su dirección?" in text and text.endswith("Gracias.")

from app.extractor import _post_process
from app.llm.base import parse_json_response
from app.models import LeadExtraction


def test_parse_json_strips_fences_and_preamble():
    assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_response('Sure! {"a": 2} Hope this helps') == {"a": 2}


def test_hallucinated_contacts_and_city_are_dropped():
    ex = LeadExtraction(problem_summary="x", city="Phoenix", phone="555-0199", email="a@b.com")
    out = _post_process(ex, "need work. call 602-555-0188")
    assert (out.city, out.phone, out.email) == (None, None, None)
    assert {"phone", "city", "address"} <= set(out.missing_info)


def test_zip_code_keeps_inferred_city():
    ex = LeadExtraction(problem_summary="x", city="Atlanta")
    assert _post_process(ex, "toilet running 30310").city == "Atlanta"


def test_enum_normalisation():
    ex = LeadExtraction.model_validate(
        {"problem_summary": "x", "service_category": "Appliance Repair", "urgency": "Within 48h", "state": "tx"}
    )
    assert (ex.service_category.value, ex.urgency.value, ex.state) == ("appliance_repair", "within_48h", "TX")

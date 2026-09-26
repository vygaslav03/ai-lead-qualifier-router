"""API tests with a fake LLM, so they run offline and don't touch the free-tier quota."""
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import DATA_DIR
from app.llm import LLMError
from app.matcher import ContractorDirectory
from app.pipeline import LeadPipeline
from app.storage import SQLiteStorage
from fakes import FakeLLM

LEAK = {
    "customer_name": "Mark", "phone": "713-555-0142", "email": None, "address": "1 Main St",
    "city": "Houston", "state": "TX", "service_category": "plumbing",
    "problem_summary": "Water leaking through ceiling.", "urgency": "emergency",
    "urgency_reason": "Active leak.", "preferred_time": None, "has_photos": False,
    "language_detected": "en", "missing_info": [], "confidence": 0.9,
}


def client_with(responses, tmp_path):
    pipeline = LeadPipeline(
        llm=FakeLLM(responses),
        directory=ContractorDirectory(DATA_DIR / "contractors.csv"),
        storage=SQLiteStorage(tmp_path / "api.db"),
    )
    return TestClient(create_app(pipeline, enable_telegram=False))


def test_create_list_and_get_lead(tmp_path):
    reply = "Hi Mark, sorry about the leak! A technician will call you shortly."
    with client_with([LEAK, reply], tmp_path) as client:
        r = client.post("/api/leads", json={"text": "water coming through ceiling 713-555-0142 Houston", "source": "sms"})
        assert r.status_code == 201
        body = r.json()
        assert body["lead"]["status"] == "matched"
        assert body["match"]["contractor"]["id"] == "C001"
        assert body["lead"]["draft_reply"] == reply

        lead_id = body["lead"]["id"]
        assert [l["id"] for l in client.get("/api/leads").json()] == [lead_id]
        detail = client.get(f"/api/leads/{lead_id}").json()
        assert detail["events"][0]["event"] == "created"


def test_invalid_json_is_retried_once(tmp_path):
    with client_with(["not json at all", LEAK], tmp_path) as client:
        r = client.post("/api/leads", json={"text": "water coming through ceiling 713-555-0142"})
        assert r.status_code == 201


def test_llm_outage_returns_503(tmp_path):
    with client_with([LLMError("quota exhausted")], tmp_path) as client:
        r = client.post("/api/leads", json={"text": "water coming through ceiling"})
        assert r.status_code == 503 and "quota" in r.json()["detail"]


@pytest.mark.parametrize("payload", [{"text": "hi"}, {"text": "long enough text here", "source": "fax"}])
def test_input_validation(tmp_path, payload):
    with client_with([], tmp_path) as client:
        assert client.post("/api/leads", json=payload).status_code == 422


def test_form_and_unknown_lead(tmp_path):
    with client_with([], tmp_path) as client:
        assert "Qualify &amp; route" in client.get("/").text
        assert client.get("/api/leads/nope").status_code == 404

from app.models import LeadExtraction, LeadRecord, LeadStatus
from app.storage import SQLiteStorage, now_iso


def make_lead(lead_id="LD-1", status=LeadStatus.MATCHED):
    ts = now_iso()
    return LeadRecord(
        id=lead_id, created_at=ts, updated_at=ts, source="sms", raw_text="leak!",
        extraction=LeadExtraction(problem_summary="Leak", city="Houston", missing_info=["address"]),
        status=status, contractor_id="C001", contractor_name="Bayou City Plumbing Co",
    )


def test_roundtrip_update_and_events(tmp_path):
    store = SQLiteStorage(tmp_path / "t.db")
    store.save_lead(make_lead())
    assert store.get_lead("LD-1").extraction.missing_info == ["address"]

    updated = store.update_lead("LD-1", status=LeadStatus.CONFIRMED, event="manager_confirmed")
    assert updated.status == LeadStatus.CONFIRMED
    assert [e["event"] for e in store.get_events("LD-1")] == ["created", "manager_confirmed"]


def test_list_filter_and_unknown_lead(tmp_path):
    store = SQLiteStorage(tmp_path / "t.db")
    store.save_lead(make_lead("LD-1"))
    store.save_lead(make_lead("LD-2", LeadStatus.NEEDS_MANUAL_ASSIGNMENT))
    assert [l.id for l in store.list_leads(status=LeadStatus.NEEDS_MANUAL_ASSIGNMENT)] == ["LD-2"]
    assert store.update_lead("nope", status=LeadStatus.CONFIRMED) is None


def test_rejects_non_updatable_fields(tmp_path):
    store = SQLiteStorage(tmp_path / "t.db")
    store.save_lead(make_lead())
    try:
        store.update_lead("LD-1", raw_text="hacked")
    except ValueError:
        return
    raise AssertionError("expected ValueError")

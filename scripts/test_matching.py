"""Phase 2 check: match + store the leads extracted in Phase 1, without new LLM calls.

Reads data/extraction_results.json (produced by scripts/test_extraction.py).

Usage:
    python scripts/test_matching.py            # writes to a throwaway data/leads_test.db
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app.config import DATA_DIR  # noqa: E402
from app.matcher import ContractorDirectory  # noqa: E402
from app.models import LeadExtraction, LeadStatus  # noqa: E402
from app.pipeline import LeadPipeline  # noqa: E402
from app.storage import SQLiteStorage  # noqa: E402

URGENCY_ICON = {"emergency": "🔴", "within_48h": "🟠", "this_week": "🟡", "flexible": "🟢"}


def main() -> None:
    results_path = DATA_DIR / "extraction_results.json"
    if not results_path.exists():
        sys.exit("Run scripts/test_extraction.py first to produce data/extraction_results.json")
    extractions = json.loads(results_path.read_text(encoding="utf-8"))
    samples = {l["id"]: l for l in json.loads((DATA_DIR / "sample_leads.json").read_text(encoding="utf-8"))}

    db_path = DATA_DIR / "leads_test.db"
    db_path.unlink(missing_ok=True)
    storage = SQLiteStorage(db_path)
    pipeline = LeadPipeline(llm=None, directory=ContractorDirectory(DATA_DIR / "contractors.csv"), storage=storage)

    rows, saved = [], {}
    for sample_id, data in extractions.items():
        ex = LeadExtraction.model_validate(data)
        sample = samples[sample_id]
        lead, match = pipeline.route(ex, sample["text"], sample["source"])
        saved[sample_id] = lead.id
        busy = f"  (skipped busy: {', '.join(c.name for c in match.unavailable)})" if match.unavailable else ""
        print(f"{sample_id} {URGENCY_ICON[ex.urgency.value]} {ex.service_category.value:<16} {ex.city or '?':<11} -> "
              f"{lead.contractor_name or '⚠️  NEEDS MANUAL ASSIGNMENT'}")
        print(f"     {match.reason}{busy}")
        if match.alternatives:
            print(f"     backups: {', '.join(f'{c.name} ({c.rating}★)' for c in match.alternatives)}")
        rows.append((sample_id, lead.id, lead.status.value, lead.contractor_id or "-"))

    # Round-trip + status update + audit trail
    demo_id = saved["L01"]
    storage.update_lead(demo_id, status=LeadStatus.CONFIRMED, event="manager_confirmed")
    lead = storage.get_lead(demo_id)
    print(f"\nStatus update check: {demo_id} -> {lead.status.value}")
    for e in storage.get_events(demo_id):
        print(f"  {e['at']}  {e['event']:<18} {e['detail']}")

    matched = sum(1 for r in rows if r[2] != LeadStatus.NEEDS_MANUAL_ASSIGNMENT.value)
    manual = [r for r in storage.list_leads(status=LeadStatus.NEEDS_MANUAL_ASSIGNMENT)]
    print(f"\n{len(rows)} leads stored in {db_path.relative_to(DATA_DIR.parent)}: "
          f"{matched} auto-matched, {len(manual)} need manual assignment")


if __name__ == "__main__":
    main()

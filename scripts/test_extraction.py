"""Phase 1 check: run LLM extraction over sample leads and print the results.

Usage:
    python scripts/test_extraction.py              # all leads
    python scripts/test_extraction.py --only L02 L03
    python scripts/test_extraction.py --limit 3 --verbose
"""
import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app.config import DATA_DIR, get_settings  # noqa: E402
from app.extractor import ExtractionError, extract_lead  # noqa: E402
from app.llm import LLMError, get_llm_client  # noqa: E402

URGENCY_ICON = {"emergency": "🔴", "within_48h": "🟠", "this_week": "🟡", "flexible": "🟢"}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", help="lead ids to run")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--verbose", action="store_true", help="print full JSON per lead")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")
    settings = get_settings()
    llm = get_llm_client(settings)
    print(f"Provider: {llm.provider} | model: {llm.model} | delay: {settings.llm_request_delay}s\n")

    leads = json.loads((DATA_DIR / "sample_leads.json").read_text(encoding="utf-8"))
    if args.only:
        leads = [l for l in leads if l["id"] in args.only]
    if args.limit:
        leads = leads[: args.limit]

    results, rows = {}, []
    for i, lead in enumerate(leads):
        if i:
            await asyncio.sleep(settings.llm_request_delay)
        started = time.perf_counter()
        try:
            ex = await extract_lead(lead["text"], llm, source=lead["source"])
        except (ExtractionError, LLMError) as e:
            print(f"{lead['id']}  ❌ {e}\n")
            rows.append((lead["id"], "ERROR", "", "", "", "", ""))
            continue
        elapsed = time.perf_counter() - started
        results[lead["id"]] = ex.model_dump(mode="json")

        print(f"{lead['id']} ({lead['note']}) - {elapsed:.1f}s")
        print(f"  {URGENCY_ICON[ex.urgency.value]} {ex.urgency.value:<11} {ex.service_category.value:<16} "
              f"{ex.city or '?'}, {ex.state or '?'}  [{ex.language_detected}]  conf={ex.confidence:.2f}")
        print(f"  summary : {ex.problem_summary}")
        print(f"  why     : {ex.urgency_reason}")
        print(f"  contact : {ex.customer_name or '-'} | {ex.phone or '-'} | {ex.email or '-'} | {ex.address or '-'}")
        print(f"  missing : {', '.join(ex.missing_info) or '-'}   photos={ex.has_photos}   time={ex.preferred_time or '-'}")
        if args.verbose:
            print(json.dumps(results[lead["id"]], indent=2, ensure_ascii=False))
        print()
        rows.append((lead["id"], ex.urgency.value, ex.service_category.value, ex.city or "?",
                     ex.language_detected, f"{ex.confidence:.2f}", str(len(ex.missing_info))))

    # Merge so partial runs (--only / --limit) don't wipe earlier results
    out = DATA_DIR / "extraction_results.json"
    merged = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    merged.update(results)
    out.write_text(json.dumps(dict(sorted(merged.items())), indent=2, ensure_ascii=False), encoding="utf-8")

    header = ("ID", "URGENCY", "SERVICE", "CITY", "LANG", "CONF", "MISSING")
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    print("Summary")
    for r in [header, *rows]:
        print("  " + "  ".join(str(v).ljust(w) for v, w in zip(r, widths)))
    print(f"\n{len(results)}/{len(leads)} extracted. Full results: {out.relative_to(DATA_DIR.parent)}")


if __name__ == "__main__":
    asyncio.run(main())

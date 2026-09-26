"""Send all sample leads through the full pipeline and print a summary.

    python run_demo.py                  # all 15 leads, Telegram cards if configured
    python run_demo.py --limit 3        # first three only
    python run_demo.py --only L02 L05   # specific leads
    python run_demo.py --no-telegram    # don't post to Telegram

Run `python main.py` in another terminal at the same time to handle the Telegram button clicks.
Each lead makes 2 LLM calls (extraction + reply); a pause between leads keeps us inside free-tier limits.
"""
import argparse
import asyncio
import json
import logging
import sys
import time
from collections import Counter

from app.config import DATA_DIR, get_settings
from app.extractor import ExtractionError
from app.llm import LLMError
from app.models import LeadStatus
from app.pipeline import LeadPipeline
from app.telegram_bot import URGENCY, TelegramBot

sys.stdout.reconfigure(encoding="utf-8")


def table(header: tuple, rows: list[tuple]) -> str:
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    line = lambda r: "  ".join(str(v).ljust(w) for v, w in zip(r, widths))  # noqa: E731
    return "\n".join([line(header), "  ".join("-" * w for w in widths), *map(line, rows)])


def short(text: str | None, n: int) -> str:
    text = text or "-"
    return text if len(text) <= n else text[: n - 1] + "…"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="*", help="sample ids to run, e.g. L01 L02")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--delay", type=float, help="seconds between leads (default: LLM_REQUEST_DELAY)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="    [%(levelname)s] %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = get_settings()
    delay = settings.llm_request_delay if args.delay is None else args.delay
    pipeline = LeadPipeline.from_settings(settings)

    bot = None
    if not args.no_telegram and settings.telegram_bot_token and settings.telegram_manager_chat_id:
        bot = TelegramBot(settings.telegram_bot_token, settings.telegram_manager_chat_id, pipeline)
        pipeline.notifier = bot.send_lead

    samples = json.loads((DATA_DIR / "sample_leads.json").read_text(encoding="utf-8"))
    if args.only:
        samples = [s for s in samples if s["id"] in args.only]
    if args.limit:
        samples = samples[: args.limit]

    sheets = type(pipeline.storage).__name__ == "MirroredStorage"
    print(f"LLM: {pipeline.llm.provider} / {pipeline.llm.model}   Telegram: {'on' if bot else 'off'}   "
          f"Sheets: {'on' if sheets else 'off'}   "
          f"DB: {settings.database_path}   leads: {len(samples)}   pause: {delay}s\n")

    rows, replies, failures, timings = [], [], [], []
    urgencies, statuses = Counter(), Counter()
    started_all = time.perf_counter()
    try:
        for i, sample in enumerate(samples):
            if i:
                await asyncio.sleep(delay)
            print(f"[{i + 1}/{len(samples)}] {sample['id']} ({sample['note']}) ...", end=" ", flush=True)
            started = time.perf_counter()
            try:
                lead, _ = await pipeline.process(sample["text"], sample["source"])
            except (ExtractionError, LLMError) as e:
                print(f"FAILED: {e}")
                failures.append((sample["id"], str(e)))
                continue
            elapsed = time.perf_counter() - started
            timings.append(elapsed)

            x = lead.extraction
            urgencies[x.urgency.value] += 1
            statuses[lead.status.value] += 1
            sent = "sent to Telegram" if lead.telegram_message_id else ("Telegram failed" if bot else "saved")
            print(f"{elapsed:.1f}s, {sent}")
            rows.append((
                sample["id"], lead.id, f"{URGENCY[x.urgency.value][0]} {x.urgency.value}", x.service_category.value,
                short(x.city, 12), short(lead.contractor_name, 26) if lead.contractor_name else "⚠ manual",
                len(lead.draft_reply or ""), f"{elapsed:.1f}s",
            ))
            replies.append((sample["id"], x.language_detected, lead.draft_reply or ""))
    finally:
        if bot:
            await bot.close()
        pipeline.storage.close()  # flush pending Google Sheets writes

    total = time.perf_counter() - started_all
    print("\n" + table(("#", "LEAD ID", "URGENCY", "SERVICE", "CITY", "CONTRACTOR", "SMS", "TIME"), rows))

    print("\nDraft replies")
    for sample_id, lang, text in replies:
        print(f"  {sample_id} [{lang}] ({len(text)} chars) {text}")

    ok = len(rows)
    if ok:
        manual = statuses[LeadStatus.NEEDS_MANUAL_ASSIGNMENT.value]
        print("\nResults")
        print(f"  processed        {ok}/{len(samples)}" + (f"  ({len(failures)} failed)" if failures else ""))
        print("  urgency          " + ", ".join(f"{URGENCY[u][0]} {u}: {urgencies[u]}" for u in URGENCY if urgencies[u]))
        print(f"  auto-matched     {ok - manual}/{ok} ({(ok - manual) / ok:.0%}), manual assignment: {manual}")
        print(f"  per lead         avg {sum(timings) / ok:.1f}s, max {max(timings):.1f}s "
              f"(message in -> extracted, routed, reply drafted{', manager notified' if bot else ''})")
        print(f"  total run        {total:.0f}s incl. {delay}s pauses for free-tier rate limits")
    for sample_id, error in failures:
        print(f"  ✗ {sample_id}: {error}")


if __name__ == "__main__":
    asyncio.run(main())

"""Baseline: handle each complaint with ONE prompt and no tools.

This is what the agent is compared against. It gets the same complaint text and
the same label lists, but no regulation lookup, no company history, no critic
and no revision loop.

Usage:
    python eval/run_baseline.py --limit 22      # small trial run first
    python eval/run_baseline.py                 # all complaints

Output:
    results/baseline.jsonl    one line per complaint; rerunning resumes
"""
import argparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic
from tqdm import tqdm

from common import MODEL, RESULTS, append_result, check_model, done_ids, load_sample
from shared.spec import BASELINE_SYSTEM, BaselineResult

OUT = RESULTS / "baseline.jsonl"

# The prompt and output format are shared with the demo pages (shared/spec.py).
SYSTEM, Result = BASELINE_SYSTEM, BaselineResult

client: anthropic.Anthropic | None = None   # created in main(), reads ANTHROPIC_API_KEY from .env
stop = threading.Event()                     # set when the whole run should stop


def run_one(row) -> str:
    """Handle one complaint with a single call. Returns 'ok', 'failed' or 'skipped'."""
    if stop.is_set():
        return "skipped"
    user = f"Company: {row.company}\nState: {row.state}\n\nComplaint:\n{row.narrative}"
    start = time.time()
    try:
        resp = client.messages.parse(
            model=MODEL,
            max_tokens=8000,
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_format=Result,
        )
    except anthropic.RateLimitError:
        # The SDK already retried. Stop rather than hammer the API.
        if not stop.is_set():
            print("\nRate limited. Rerun later or with fewer --workers; finished complaints are kept.")
        stop.set()
        return "skipped"
    except anthropic.APIStatusError as e:
        append_result(OUT, {"complaint_id": row.complaint_id, "error": f"{e.status_code}: {e.message}"})
        return "failed"
    except anthropic.APIConnectionError as e:
        if not stop.is_set():
            print(f"\nConnection problem: {e}. Rerun to resume.")
        stop.set()
        return "skipped"

    if resp.stop_reason != "end_turn" or resp.parsed_output is None:
        append_result(OUT, {"complaint_id": row.complaint_id, "error": f"stopped: {resp.stop_reason}"})
        return "failed"
    append_result(OUT, {
        "complaint_id": row.complaint_id,
        **resp.parsed_output.model_dump(),
        "llm_calls": 1,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "seconds": round(time.time() - start, 1),
    })
    return "ok"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="only run this many complaints")
    ap.add_argument("--workers", type=int, default=4, help="complaints sent at the same time")
    args = ap.parse_args()

    global client
    client = anthropic.Anthropic()
    check_model(OUT)
    df = load_sample(args.limit)
    todo = df[~df["complaint_id"].isin(done_ids(OUT))]
    print(f"Model: {MODEL} | {len(todo)} to run, {len(df) - len(todo)} already done, "
          f"{args.workers} at a time")

    counts = {"ok": 0, "failed": 0, "skipped": 0}
    with ThreadPoolExecutor(args.workers) as pool:
        futures = [pool.submit(run_one, row) for row in todo.itertuples()]
        for fut in tqdm(as_completed(futures), total=len(futures)):
            counts[fut.result()] += 1

    print(f"Done: {counts['ok']} ok, {counts['failed']} failed, {counts['skipped']} not sent. "
          f"Results in {OUT}. Rerun to retry failures.")


if __name__ == "__main__":
    main()

"""Send each test complaint to the n8n agent and save what it returns.

n8n must be running with the workflow published (see README.md).

Usage:
    python eval/run_agent.py --limit 22      # small trial run first
    python eval/run_agent.py                 # all complaints
    python eval/run_agent.py --workers 2     # fewer at once if you hit rate limits

Output:
    results/agent.jsonl    one line per complaint; rerunning resumes
"""
import argparse
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from tqdm import tqdm

from common import RESULTS, append_result, check_model, done_ids, load_sample

OUT = RESULTS / "agent.jsonl"
URL = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/complaint")
stop = threading.Event()   # set when something is wrong enough to stop the whole run


def run_one(row) -> str:
    """Send one complaint. Returns 'ok', 'failed' or 'skipped'."""
    if stop.is_set():
        return "skipped"
    payload = {"complaint_id": row.complaint_id, "company": row.company,
               "state": row.state, "narrative": row.narrative}
    start = time.time()
    try:
        r = requests.post(URL, json=payload, timeout=600)
    except requests.ConnectionError:
        if not stop.is_set():
            print(f"\nCannot reach n8n at {URL}. Is it running and is the workflow published?")
        stop.set()
        return "skipped"
    except requests.Timeout:
        append_result(OUT, {"complaint_id": row.complaint_id, "error": "timeout after 600s"})
        return "failed"

    result = None
    if r.ok:
        try:
            result = r.json()
        except ValueError:
            pass
    if not isinstance(result, dict) or "product" not in result:
        append_result(OUT, {"complaint_id": row.complaint_id,
                            "error": f"HTTP {r.status_code}: {r.text[:300]}"})
        return "failed"

    result["complaint_id"] = row.complaint_id
    result["seconds"] = round(time.time() - start, 1)
    append_result(OUT, result)
    return "ok"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="only run this many complaints")
    ap.add_argument("--workers", type=int, default=4, help="complaints sent at the same time")
    args = ap.parse_args()

    check_model(OUT)
    df = load_sample(args.limit)
    todo = df[~df["complaint_id"].isin(done_ids(OUT))]
    print(f"Agent at {URL} | {len(todo)} to run, {len(df) - len(todo)} already done, "
          f"{args.workers} at a time")

    counts = {"ok": 0, "failed": 0, "skipped": 0}
    with ThreadPoolExecutor(args.workers) as pool:
        futures = [pool.submit(run_one, row) for row in todo.itertuples()]
        for fut in tqdm(as_completed(futures), total=len(futures)):
            status = fut.result()
            counts[status] += 1
            # Many failures early on means something is broken, not one bad complaint.
            if counts["failed"] >= 5 and counts["failed"] > counts["ok"] and not stop.is_set():
                print("\nMost requests are failing. Stopping; open the n8n Executions tab to see why.")
                stop.set()

    print(f"Done: {counts['ok']} ok, {counts['failed']} failed, {counts['skipped']} not sent. "
          f"Results in {OUT}. Rerun to retry failures.")


if __name__ == "__main__":
    main()

"""Send each test complaint to the n8n agent and save what it returns.

n8n must be running with the workflow published (see README.md).

Usage:
    python eval/run_agent.py --limit 22      # small trial run first
    python eval/run_agent.py                 # all complaints

Output:
    results/agent.jsonl    one line per complaint; rerunning resumes
"""
import argparse
import os
import time

import requests
from tqdm import tqdm

from common import RESULTS, append_result, done_ids, load_sample

OUT = RESULTS / "agent.jsonl"
URL = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/complaint")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="only run this many complaints")
    args = ap.parse_args()

    df = load_sample(args.limit)
    todo = df[~df["complaint_id"].isin(done_ids(OUT))]
    print(f"Agent at {URL} | {len(todo)} to run, {len(df) - len(todo)} already done")

    failures_in_a_row = 0
    for row in tqdm(todo.itertuples(), total=len(todo)):
        payload = {
            "complaint_id": row.complaint_id,
            "company": row.company,
            "state": row.state,
            "narrative": row.narrative,
        }
        start = time.time()
        try:
            r = requests.post(URL, json=payload, timeout=600)
        except requests.ConnectionError:
            print(f"\nCannot reach n8n at {URL}. Is it running and is the workflow published?")
            break
        except requests.Timeout:
            append_result(OUT, {"complaint_id": row.complaint_id, "error": "timeout after 600s"})
            continue

        result = None
        if r.ok:
            try:
                result = r.json()
            except ValueError:
                pass
        if not isinstance(result, dict) or "product" not in result:
            append_result(OUT, {"complaint_id": row.complaint_id,
                                "error": f"HTTP {r.status_code}: {r.text[:300]}"})
            failures_in_a_row += 1
            if failures_in_a_row >= 3:
                print("\nThree failures in a row. Stopping; open the n8n Executions tab to see why.")
                break
            continue

        failures_in_a_row = 0
        result["complaint_id"] = row.complaint_id
        result["seconds"] = round(time.time() - start, 1)
        append_result(OUT, result)

    print(f"Results in {OUT}")


if __name__ == "__main__":
    main()

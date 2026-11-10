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
import time
from typing import Literal

import anthropic
from pydantic import BaseModel
from tqdm import tqdm

from common import MODEL, PRODUCTS, RESULTS, append_result, done_ids, load_sample, taxonomy_text

OUT = RESULTS / "baseline.jsonl"

SYSTEM = f"""You work in the complaints team of a US consumer finance company. \
For the complaint you are given, do the whole job in one pass:

1. Classify it. Choose the product, then choose one issue from that product's list. \
Copy both exactly as written below.
2. Predict how the company will close it: explanation_only (the company explains but \
changes nothing), non_monetary_relief (it fixes something, such as correcting a record), \
or monetary_relief (it refunds or credits money).
3. Decide whether a human must review the case before any reply is sent.
4. Draft the reply to the consumer. Name any US consumer finance law that applies.

Complaint text has personal details replaced with XXXX.

{taxonomy_text()}"""


class Result(BaseModel):
    product: Literal[tuple(PRODUCTS)]  # type: ignore[valid-type]
    issue: str
    predicted_outcome: Literal["explanation_only", "non_monetary_relief", "monetary_relief"]
    needs_human_review: bool
    regulations_cited: list[str]
    reply: str


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="only run this many complaints")
    args = ap.parse_args()

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from .env
    df = load_sample(args.limit)
    todo = df[~df["complaint_id"].isin(done_ids(OUT))]
    print(f"Model: {MODEL} | {len(todo)} to run, {len(df) - len(todo)} already done")

    for row in tqdm(todo.itertuples(), total=len(todo)):
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
            print("Rate limited. Wait a minute and rerun; finished complaints are kept.")
            break
        except anthropic.APIStatusError as e:
            append_result(OUT, {"complaint_id": row.complaint_id, "error": f"{e.status_code}: {e.message}"})
            continue
        except anthropic.APIConnectionError as e:
            print(f"Connection problem: {e}. Rerun to resume.")
            break

        if resp.stop_reason != "end_turn" or resp.parsed_output is None:
            append_result(OUT, {"complaint_id": row.complaint_id, "error": f"stopped: {resp.stop_reason}"})
            continue
        append_result(OUT, {
            "complaint_id": row.complaint_id,
            **resp.parsed_output.model_dump(),
            "llm_calls": 1,
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "seconds": round(time.time() - start, 1),
        })

    print(f"Results in {OUT}")


if __name__ == "__main__":
    main()

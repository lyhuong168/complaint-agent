"""Grade the quality of the drafted replies with an LLM judge.

Classification can be scored against real labels, but there is no "correct
reply" in the data. So a judge model grades each reply against a fixed rubric.
The judge sees one reply at a time and is not told which system wrote it.

Usage:
    python eval/judge_replies.py --limit 55     # 5 complaints per product
    python eval/judge_replies.py --summary      # only print the averages

Output:
    results/judge.jsonl    one line per (complaint, system); rerunning resumes

Limits to mention in the report: the judge is the same model family that wrote
the replies, and its "legal accuracy" check is only as good as the regulation
summaries in n8n/knowledge/regulations.json. Check a few grades by hand.
"""
import argparse
import json

import anthropic
import pandas as pd
from pydantic import BaseModel, Field
from tqdm import tqdm

from common import MODEL, RESULTS, ROOT, append_result, load_results, load_sample

OUT = RESULTS / "judge.jsonl"
CRITERIA = ["grounded", "legally_accurate", "responsive", "no_overpromising", "tone"]

regulations = json.loads((ROOT / "n8n" / "knowledge" / "regulations.json").read_text(encoding="utf-8"))
REG_TEXT = "\n\n".join(
    f"[{r['id']}] {r['name']} ({r['citation']})\n" + "\n".join(f"- {x}" for x in r["rules"])
    for r in regulations
)

SYSTEM = f"""You grade draft replies that a US consumer finance company would send to a consumer \
who filed a complaint. Grade the reply you are given on each criterion from 1 (fails) to 5 (excellent):

- grounded: every statement about the consumer's case comes from the complaint; nothing is invented.
- legally_accurate: rules the reply names are real, apply to this situation, and are stated \
correctly. Use the reference below. A reply that names no rule where one clearly applies scores 3 at most.
- responsive: it addresses what the consumer asked for and gives a concrete next step.
- no_overpromising: it does not admit legal fault or promise a refund, correction or outcome \
that has not been decided.
- tone: plain, respectful, not defensive, understandable by a non-lawyer.

Be strict and consistent: 5 means you would send it unchanged. Give one sentence of reasons.

Regulation reference:
{REG_TEXT}"""


class Grade(BaseModel):
    grounded: int = Field(ge=1, le=5)
    legally_accurate: int = Field(ge=1, le=5)
    responsive: int = Field(ge=1, le=5)
    no_overpromising: int = Field(ge=1, le=5)
    tone: int = Field(ge=1, le=5)
    reasons: str


def summary() -> None:
    if not OUT.exists():
        print("No judge results yet.")
        return
    df = pd.read_json(OUT, lines=True)
    df = df[df.get("error").isna()] if "error" in df else df
    df["overall"] = df[CRITERIA].mean(axis=1)
    print(df.groupby("system")[CRITERIA + ["overall"]].mean().round(2).T.to_string())
    print(f"\nReplies graded per system:\n{df['system'].value_counts().to_string()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=55, help="complaints to grade (per system)")
    ap.add_argument("--summary", action="store_true", help="only print averages")
    args = ap.parse_args()
    if args.summary:
        summary()
        return

    systems = {name: load_results(RESULTS / f"{name}.jsonl") for name in ("baseline", "agent")}
    df = load_sample()
    # Grade only complaints that both systems answered, so the comparison is like for like.
    both = df[df["complaint_id"].isin(systems["baseline"]) & df["complaint_id"].isin(systems["agent"])]
    per = max(1, args.limit // both["product"].nunique()) if len(both) else 0
    both = both.groupby("product").head(per).head(args.limit)
    if both.empty:
        print("No complaint has been answered by both systems yet. Run both first.")
        return

    done = set()
    if OUT.exists():
        with open(OUT, encoding="utf-8") as f:
            done = {(j["complaint_id"], j["system"]) for j in map(json.loads, f) if "error" not in j}
    jobs = [(row, name) for row in both.itertuples() for name in systems
            if (row.complaint_id, name) not in done]
    print(f"Judge model: {MODEL} | {len(jobs)} replies to grade")

    client = anthropic.Anthropic()
    for row, name in tqdm(jobs):
        reply = systems[name][row.complaint_id]["reply"]
        try:
            resp = client.messages.parse(
                model=MODEL,
                max_tokens=4000,
                # The long rubric is identical on every call, so cache it.
                system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user",
                           "content": f"Complaint:\n{row.narrative}\n\nDraft reply:\n{reply}"}],
                output_format=Grade,
            )
        except anthropic.RateLimitError:
            print("Rate limited. Wait a minute and rerun; finished grades are kept.")
            break
        except anthropic.APIStatusError as e:
            append_result(OUT, {"complaint_id": row.complaint_id, "system": name,
                                "error": f"{e.status_code}: {e.message}"})
            continue
        except anthropic.APIConnectionError as e:
            print(f"Connection problem: {e}. Rerun to resume.")
            break
        if resp.parsed_output is None:
            append_result(OUT, {"complaint_id": row.complaint_id, "system": name,
                                "error": f"stopped: {resp.stop_reason}"})
            continue
        append_result(OUT, {"complaint_id": row.complaint_id, "system": name,
                            **resp.parsed_output.model_dump()})
    summary()


if __name__ == "__main__":
    main()

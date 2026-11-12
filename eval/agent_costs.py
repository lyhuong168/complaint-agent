"""Add up token usage and cost for the agent runs, from n8n's own records.

n8n stores every run (an "execution") in its database, including the token
counts of each Claude call. This script reads that database (read-only) and
adds the totals to results/agent_costs.csv, one row per complaint.

Usage:
    python eval/agent_costs.py

Prices are per million tokens. Update them if you change the model.
"""
import json
import sqlite3

import pandas as pd

from common import MODEL, RESULTS, ROOT, load_results

PRICES = {  # USD per million tokens: (input, output)
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
}
DB = ROOT / "n8n" / "data" / ".n8n" / "database.sqlite"


def parse_execution(raw: str) -> tuple[str | None, int, int, int]:
    """Return (complaint_id, input tokens, output tokens, number of Claude calls).

    n8n stores run data in a compact format where values are references to
    other positions in one big list, so values are looked up by index.
    """
    arr = json.loads(raw)

    def val(x):
        return arr[int(x)] if isinstance(x, str) and x.isdigit() else x

    complaint_id, tokens_in, tokens_out, calls = None, 0, 0, 0
    for item in arr:
        if not isinstance(item, dict):
            continue
        if "promptTokens" in item and "completionTokens" in item:
            tokens_in += int(val(item["promptTokens"]) or 0)
            tokens_out += int(val(item["completionTokens"]) or 0)
            calls += 1
        elif complaint_id is None and "complaint_id" in item and "narrative" in item:
            complaint_id = str(val(item["complaint_id"]))
    return complaint_id, tokens_in, tokens_out, calls


def main() -> None:
    if not DB.exists():
        print(f"No n8n database at {DB}")
        return
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "select e.id, e.status, d.data from execution_entity e "
        "join execution_data d on d.executionId = e.id "
        "where e.workflowId = 'ComplaintAgent01' order by e.id"
    ).fetchall()

    records = []
    for exec_id, status, raw in rows:
        cid, t_in, t_out, calls = parse_execution(raw)
        records.append({
            "execution_id": exec_id, "complaint_id": cid, "status": status,
            "claude_calls": calls, "input_tokens": t_in, "output_tokens": t_out,
        })
    df = pd.DataFrame(records)

    # Keep the latest successful run for each complaint in the results file,
    # priced at the rates of the model recorded for that complaint.
    answered = load_results(RESULTS / "agent.jsonl")
    scored = df[df["complaint_id"].isin(answered) & (df["status"] == "success")]
    scored = scored.drop_duplicates("complaint_id", keep="last").copy()
    scored["model"] = scored["complaint_id"].map(lambda c: answered[c].get("model") or MODEL)
    rates = scored["model"].map(lambda m: PRICES.get(m, PRICES["claude-opus-5-5"]))
    scored["cost_usd"] = [round(i * p[0] / 1e6 + o * p[1] / 1e6, 4)
                          for i, o, p in zip(scored["input_tokens"], scored["output_tokens"], rates)]
    RESULTS.mkdir(exist_ok=True)
    scored.to_csv(RESULTS / "agent_costs.csv", index=False)

    print(f"All n8n runs recorded: {len(df)} (includes tests and earlier runs)")
    for model, (p_in, p_out) in PRICES.items():
        if model in set(scored["model"]):
            print(f"Prices for {model}: ${p_in}/M input, ${p_out}/M output")
    if len(scored):
        print(f"Complaints in results/agent.jsonl: {len(scored)}")
        print(f"  cost per complaint: ${scored['cost_usd'].mean():.3f} "
              f"(min ${scored['cost_usd'].min():.3f}, max ${scored['cost_usd'].max():.3f})")
        print(f"  Claude calls per complaint: {scored['claude_calls'].mean():.1f}")
        print(f"  total: ${scored['cost_usd'].sum():.2f}")


if __name__ == "__main__":
    main()

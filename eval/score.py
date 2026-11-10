"""Score the baseline and the agent against the real CFPB labels.

Usage:
    python eval/score.py

Reads   results/baseline.jsonl and results/agent.jsonl (whichever exist)
Writes  results/metrics.json and results/errors_<system>.csv

What is measured, and against which real label:
    product      the product the consumer filed under        (11 classes)
    issue        the issue the consumer filed under          (85 product/issue pairs)
    outcome      how the company actually closed the case    (3 classes)
    relief       outcome simplified to "got relief" yes/no
    human review how many cases are sent to a person, and how many of the cases
                 that really ended in monetary relief were among them
"""
import argparse
import json
from pathlib import Path

import pandas as pd
from sklearn.metrics import accuracy_score, f1_score, precision_recall_fscore_support

from common import OUTCOMES, RESULTS, load_results, load_sample

RELIEF = {"Closed with monetary relief", "Closed with non-monetary relief"}


def score(name: str, path: Path, truth: pd.DataFrame) -> dict | None:
    results = load_results(path)
    if not results:
        return None
    pred = pd.DataFrame(results.values())
    df = truth.merge(pred, on="complaint_id", suffixes=("_true", "_pred"))
    n = len(df)

    outcome_pred = df["predicted_outcome"].map(OUTCOMES)
    relief_true = df["company_response"].isin(RELIEF)
    relief_pred = outcome_pred.isin(RELIEF)
    p, r, f, _ = precision_recall_fscore_support(relief_true, relief_pred, average="binary",
                                                 zero_division=0)
    human = df["needs_human_review"].astype(bool)
    money = df["company_response"] == "Closed with monetary relief"

    metrics = {
        "complaints_scored": n,
        "product_accuracy": accuracy_score(df["product_true"], df["product_pred"]),
        "product_macro_f1": f1_score(df["product_true"], df["product_pred"], average="macro",
                                     zero_division=0),
        "issue_accuracy": accuracy_score(df["issue_true"], df["issue_pred"]),
        "issue_accuracy_when_product_right": accuracy_score(
            df.loc[df["product_true"] == df["product_pred"], "issue_true"],
            df.loc[df["product_true"] == df["product_pred"], "issue_pred"]),
        "outcome_accuracy": accuracy_score(df["company_response"], outcome_pred),
        "outcome_macro_f1": f1_score(df["company_response"], outcome_pred, average="macro",
                                     zero_division=0),
        "relief_precision": p,
        "relief_recall": r,
        "relief_f1": f,
        "human_review_rate": human.mean(),
        "monetary_cases_sent_to_human": human[money].mean() if money.any() else None,
        "avg_seconds": df["seconds"].mean() if "seconds" in df else None,
    }
    if "input_tokens" in df:
        metrics["avg_input_tokens"] = df["input_tokens"].mean()
        metrics["avg_output_tokens"] = df["output_tokens"].mean()
    if "revisions" in df:
        metrics["avg_revisions"] = df["revisions"].mean()
        metrics["critic_approval_rate"] = df["critic_approved"].astype(bool).mean()
        metrics["avg_agent_runs"] = df["agent_runs"].mean()

    wrong = df[(df["product_true"] != df["product_pred"]) | (df["issue_true"] != df["issue_pred"])
               | (df["company_response"] != outcome_pred)]
    cols = ["complaint_id", "product_true", "product_pred", "issue_true", "issue_pred",
            "company_response", "predicted_outcome", "narrative"]
    wrong[cols].to_csv(path.parent / f"errors_{name}.csv", index=False, encoding="utf-8")
    return metrics


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", type=Path, default=RESULTS)
    args = ap.parse_args()

    truth = load_sample()
    # A useful floor: always predicting the most common outcome.
    majority = (truth["company_response"] == "Closed with explanation").mean()

    table = {}
    for name in ("baseline", "agent"):
        m = score(name, args.results_dir / f"{name}.jsonl", truth)
        if m:
            table[name] = m
    if not table:
        print(f"No results in {args.results_dir}. Run run_baseline.py or run_agent.py first.")
        return

    (args.results_dir / "metrics.json").write_text(json.dumps(table, indent=2), encoding="utf-8")
    out = pd.DataFrame(table)
    pd.set_option("display.float_format", lambda v: f"{v:.3f}")
    print(out.to_string())
    print(f"\nFloor for outcome_accuracy (always guess 'explanation'): {majority:.3f}")
    if len(table) == 2 and table["baseline"]["complaints_scored"] != table["agent"]["complaints_scored"]:
        print("Note: the two systems were scored on different numbers of complaints.")
    print(f"Saved {args.results_dir / 'metrics.json'} and errors_<system>.csv")


if __name__ == "__main__":
    main()

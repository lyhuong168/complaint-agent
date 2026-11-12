"""Streamlit demo: the same complaint goes to the one-prompt baseline and to the agent.

Run locally:   streamlit run webapp/streamlit_app.py
Hosted:        Streamlit Community Cloud, main file webapp/streamlit_app.py (see README)

Secrets: ANTHROPIC_API_KEY and DEMO_PASSCODE, from .env locally or from the
app's Secrets settings on Streamlit Cloud. CLAUDE_MODEL is optional.
"""
import csv
import hmac
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# On Streamlit Cloud the secrets live in st.secrets; copy them into the
# environment before shared/spec.py reads CLAUDE_MODEL. Locally, .env is used.
try:
    for key in ("ANTHROPIC_API_KEY", "DEMO_PASSCODE", "CLAUDE_MODEL"):
        if key in st.secrets and not os.getenv(key):
            os.environ[key] = str(st.secrets[key])
except Exception:   # no secrets file: running locally on .env
    pass

import anthropic  # noqa: E402

from shared.spec import EXAMPLE_IDS, MODEL  # noqa: E402
from webapp.agent import AgentError, run_agent, run_baseline  # noqa: E402

OUTCOME_LABEL = {"explanation_only": "Explanation only",
                 "non_monetary_relief": "Non-monetary relief",
                 "monetary_relief": "Monetary relief"}
TRUE_TO_OUTCOME = {"Closed with explanation": "explanation_only",
                   "Closed with non-monetary relief": "non_monetary_relief",
                   "Closed with monetary relief": "monetary_relief"}

st.set_page_config(page_title="Complaint Agent", page_icon="📨", layout="wide")


@st.cache_data
def load_examples() -> list[dict]:
    """Real complaints from the test sample, with their real CFPB labels."""
    with open(ROOT / "data" / "complaints_sample.csv", encoding="utf-8") as f:
        by_id = {r["complaint_id"]: r for r in csv.DictReader(f)}
    return [{"label": label, "complaint_id": cid, **by_id[cid]} for label, cid in EXAMPLE_IDS]


@st.cache_resource
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


examples = load_examples()

# --- sidebar: passcode and what this is -----------------------------------
with st.sidebar:
    st.header("Access")
    expected = os.getenv("DEMO_PASSCODE", "")
    passcode = st.text_input("Passcode", type="password",
                             help="Each submission makes real Claude calls, so the demo is locked.")
    unlocked = bool(expected) and hmac.compare_digest(passcode, expected)
    if not expected:
        st.error("DEMO_PASSCODE is not set, so the demo stays locked. Add it to .env or the app's secrets.")
    elif passcode and not unlocked:
        st.error("Wrong passcode.")
    if not os.getenv("ANTHROPIC_API_KEY"):
        st.error("ANTHROPIC_API_KEY is not set.")
    st.divider()
    st.caption(f"Model: `{MODEL}`")
    st.caption("The agent here is a Python copy of the n8n workflow, with the same prompts, "
               "tools and rules. The evaluated version runs in n8n.")

# --- input -----------------------------------------------------------------
st.title("Complaint Triage and Resolution Agent")
st.caption("The same complaint goes to a one-prompt baseline and to the multi-agent workflow.")


def use_example():
    ex = next((e for e in examples if e["label"] == st.session_state.example), None)
    st.session_state.company = ex["company"] if ex else ""
    st.session_state.state = ex["state"] if ex else ""
    st.session_state.narrative = ex["narrative"] if ex else ""


st.selectbox("Example complaint", ["Write your own…"] + [e["label"] for e in examples],
             key="example", on_change=use_example)
c1, c2 = st.columns([3, 1])
c1.text_input("Company", key="company", placeholder="e.g. WELLS FARGO & COMPANY")
c2.text_input("State", key="state", placeholder="e.g. CA", max_chars=20)
st.text_area("Complaint text", key="narrative", height=180,
             placeholder="Describe the problem as the consumer would.")

example = next((e for e in examples if e["label"] == st.session_state.get("example")), None)
if example and st.session_state.get("narrative") == example["narrative"]:
    st.info(f"**Real CFPB record:** {example['product']} · {example['issue']} · "
            f"{example['company_response']}")
else:
    example = None   # edited or own complaint: nothing to compare against

submitted = st.button("Submit to both", type="primary", disabled=not unlocked)
if not unlocked:
    st.caption("Enter the passcode in the sidebar to submit.")


# --- output ----------------------------------------------------------------
def check(value: str, truth: str | None) -> str:
    if truth is None:
        return value
    return f"{value} ✅" if value == truth else f"{value} ❌ (real: {truth})"


def show_result(r: dict, kind: str):
    truth = example
    st.markdown(f"**Product:** {check(r['product'], truth and truth['product'])}")
    st.markdown(f"**Issue:** {check(r['issue'], truth and truth['issue'])}")
    real_outcome = truth and TRUE_TO_OUTCOME.get(truth["company_response"])
    outcome = OUTCOME_LABEL[r["predicted_outcome"]]
    st.markdown(f"**Predicted outcome:** "
                f"{check(outcome, real_outcome and OUTCOME_LABEL[real_outcome])}")
    if kind == "agent":
        st.markdown(f"**Urgency:** {r['urgency']} · **Risk flags:** "
                    f"{', '.join(r['risk_flags']) or 'none'}")
    if r["needs_human_review"]:
        st.warning("**Sent to a person for review**" + (
            "".join(f"\n- {x}" for x in r.get("review_reasons", [])) if kind == "agent" else ""))
    else:
        st.success("**Ready to send**")
    st.markdown(f"**Regulations cited:** {', '.join(r['regulations_cited']) or 'none'}")
    st.markdown("**Reply to the consumer**")
    st.markdown(f"> {r['reply']}".replace("\n", "\n> "))
    if kind == "agent":
        with st.expander("What the agent did"):
            for line in r["trace"]:
                st.markdown(f"- {line}")
            if r["critic_suggestions"]:
                st.markdown("**Critic suggestions (not blocking):**")
                for s in r["critic_suggestions"]:
                    st.markdown(f"- {s}")
        st.caption(f"{r['seconds']} s · {r['revisions']} revision"
                   f"{'' if r['revisions'] == 1 else 's'} · {r['llm_calls']} Claude calls · "
                   f"{r['input_tokens'] + r['output_tokens']:,} tokens")
    else:
        st.caption(f"{r['seconds']} s · 1 Claude call · "
                   f"{r['input_tokens'] + r['output_tokens']:,} tokens")


left, right = st.columns(2)
left.subheader("Baseline")
left.caption("One Claude call. No tools, no review, no rules.")
right.subheader("Agent")
right.caption("Triage agent → resolution agent with tools → compliance critic → routing rules.")

if submitted:
    args = (st.session_state.company.strip() or "Unknown",
            st.session_state.state.strip() or "Unknown",
            st.session_state.narrative.strip())
    if not args[2]:
        st.error("Enter the complaint text.")
        st.stop()
    client = get_client()
    with ThreadPoolExecutor(1) as pool:
        baseline_job = pool.submit(run_baseline, client, *args)   # runs while the agent works
        with right:
            try:
                with st.status("Agent working…", expanded=True) as status:
                    agent_result = run_agent(client, *args, on_step=st.write)
                    status.update(label="Agent finished", state="complete", expanded=False)
                show_result(agent_result, "agent")
            except AgentError as e:
                st.error(str(e))
        with left:
            try:
                show_result(baseline_job.result(), "baseline")
            except AgentError as e:
                st.error(str(e))
else:
    left.info("Waiting for a complaint.")
    right.info("Waiting for a complaint.")

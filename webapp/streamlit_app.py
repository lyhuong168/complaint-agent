"""Streamlit demo: the same complaint goes to the one-prompt baseline and to the agent.

Run locally:   streamlit run webapp/streamlit_app.py
Hosted:        Streamlit Community Cloud, main file webapp/streamlit_app.py (see README)

Secrets: ANTHROPIC_API_KEY and DEMO_PASSCODE, from .env locally or from the
app's Secrets settings on Streamlit Cloud. CLAUDE_MODEL is optional.

How a run works: Submit starts the agent and the baseline in two background
threads. The agent reports every step as an event; a fragment of the page
reruns every 0.6 s while they work and redraws the pipeline strip and the live
timeline from those events. The job is kept in the session, so clicking around
the page during a run does not stop it, and the last result stays on screen.
"""
import csv
import hmac
import html
import os
import sys
import threading
import time
from pathlib import Path

import streamlit as st

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
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

from shared.spec import EXAMPLE_IDS, MAX_REVISIONS, MODEL  # noqa: E402
from webapp.agent import AgentError, run_agent, run_baseline  # noqa: E402

OUTCOME_LABEL = {"explanation_only": "Explanation only",
                 "non_monetary_relief": "Non-monetary relief",
                 "monetary_relief": "Monetary relief"}
TRUE_TO_OUTCOME = {"Closed with explanation": "explanation_only",
                   "Closed with non-monetary relief": "non_monetary_relief",
                   "Closed with monetary relief": "monetary_relief"}

# The six steps of the workflow, in order: (key, name, agentic concept, what it does).
STEPS = [
    ("triage", "Triage agent", "Specialised agent",
     "Picks the product, issue, urgency and risk flags"),
    ("labels", "Label check", "Guardrail",
     "Code rejects labels that are not on the CFPB lists"),
    ("resolution", "Resolution agent", "Tool use",
     "Searches regulations and company history, predicts the outcome, drafts the reply"),
    ("citations", "Citation check", "Guardrail",
     "Code flags any cited rule that is not in the knowledge base"),
    ("critic", "Compliance critic", "Reflection",
     "A second agent reviews the draft and can send it back"),
    ("routing", "Routing rules", "Human in the loop",
     "Fixed rules send urgent, risky or money cases to a person"),
]
CONCEPT = {key: concept for key, _, concept, _ in STEPS}
STATUS_STAMP = {"ok": ("stamp-ok", "✓ Done"), "warn": ("stamp-warn", "! Flagged"),
                "fail": ("stamp-fail", "✗ Failed"), "run": ("stamp-run", "Working"),
                "wait": ("stamp-wait", "Waiting")}
TICK = 0.6          # seconds between live redraws while a run is going

st.set_page_config(page_title="Complaint Agent", page_icon="🧾", layout="wide")
st.html(HERE / "static" / "style.css")
e = html.escape


# ----------------------------------------------------------------------------
# Data and the background job
# ----------------------------------------------------------------------------
@st.cache_data
def load_examples() -> list[dict]:
    """Real complaints from the test sample, with their real CFPB labels."""
    with open(ROOT / "data" / "complaints_sample.csv", encoding="utf-8") as f:
        by_id = {r["complaint_id"]: r for r in csv.DictReader(f)}
    return [{"label": label, "complaint_id": cid, **by_id[cid]} for label, cid in EXAMPLE_IDS]


@st.cache_resource
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


class Job:
    """One submission: the agent and the baseline, each in its own thread."""

    def __init__(self, args: tuple[str, str, str], truth: dict | None):
        self.args, self.truth = args, truth
        self.started = time.time()
        self.events: list[tuple[float, dict]] = []    # (seconds since start, event)
        self.agent = self.baseline = None
        self.agent_error = self.baseline_error = None
        self.agent_done = self.baseline_done = False
        self.ended: float | None = None

    @property
    def running(self) -> bool:
        return not (self.agent_done and self.baseline_done)

    def elapsed(self) -> float:
        return (self.ended or time.time()) - self.started

    def start(self, client: anthropic.Anthropic):
        threading.Thread(target=self._agent, args=(client,), daemon=True).start()
        threading.Thread(target=self._baseline, args=(client,), daemon=True).start()

    def _finish(self):
        if not self.running:
            self.ended = time.time()

    def _agent(self, client):
        try:
            self.agent = run_agent(client, *self.args, on_event=lambda ev: self.events.append(
                (time.time() - self.started, ev)))
        except AgentError as ex:
            self.agent_error = str(ex)
        except Exception as ex:   # show it on the page instead of losing the thread
            self.agent_error = f"Unexpected error: {ex}"
        self.agent_done = True
        self._finish()

    def _baseline(self, client):
        try:
            self.baseline = run_baseline(client, *self.args)
        except AgentError as ex:
            self.baseline_error = str(ex)
        except Exception as ex:
            self.baseline_error = f"Unexpected error: {ex}"
        self.baseline_done = True
        self._finish()


def build_nodes(job: Job) -> list[dict]:
    """Turn the agent's events into timeline entries, one per step run."""
    nodes = []
    for t, ev in list(job.events):
        if ev["type"] == "start":
            nodes.append({"step": ev["step"], "title": ev["title"], "t0": t, "t1": None,
                          "status": "run", "summary": "", "items": [], "tools": []})
        elif ev["type"] == "tool" and nodes:
            nodes[-1]["tools"].append(ev)
        elif ev["type"] == "end" and nodes:
            nodes[-1].update(t1=t, status=ev["status"], summary=ev["summary"], items=ev["items"])
    if job.agent_error:
        if nodes and nodes[-1]["status"] == "run":
            nodes[-1].update(status="fail", summary=job.agent_error, t1=job.elapsed())
        elif not nodes:
            nodes.append({"step": "triage", "title": "Agent", "t0": 0, "t1": job.elapsed(),
                          "status": "fail", "summary": job.agent_error, "items": [], "tools": []})
    return nodes


# ----------------------------------------------------------------------------
# HTML pieces (everything from Claude or the user is escaped)
# ----------------------------------------------------------------------------
def stamp(kind: str, text: str, big: bool = False) -> str:
    dot = '<span class="dot-live"></span>' if kind == "stamp-run" else ""
    return f'<span class="stamp {kind}{" stamp-big" if big else ""}">{dot}{e(text)}</span>'


def clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def pipeline_html(nodes: list[dict]) -> str:
    cells = []
    for i, (key, name, concept, desc) in enumerate(STEPS, 1):
        runs = [n for n in nodes if n["step"] == key]
        status = "wait" if not runs else runs[-1]["status"]
        cls, label = STATUS_STAMP[status]
        times = f" ×{len(runs)}" if len(runs) > 1 else ""
        cells.append(
            f'<div class="pipe-step is-{status}"><div class="n">{i:02d} · {e(concept)}</div>'
            f'<div class="t">{e(name)}</div><div class="d">{e(desc)}</div>'
            f'<div class="s">{stamp(cls, label + times)}</div></div>')
    return (f'<div class="pipe">{"".join(cells)}</div>'
            f'<div class="pipe-loop">↺ the critic can send the draft back to the resolution agent, '
            f'at most {MAX_REVISIONS} times</div>')


def timeline_html(job: Job, nodes: list[dict]) -> str:
    rows = []
    for n in nodes:
        cls, label = STATUS_STAMP[n["status"]]
        dur = (f"{n['t1'] - n['t0']:.1f} s" if n["t1"] is not None else "") if n["step"] not in (
            "labels", "citations", "routing") else "code"
        body = [f'<div class="tl-row"><span class="tl-title">{e(n["title"])}</span>'
                f'<span class="tl-concept">{e(CONCEPT.get(n["step"], ""))}</span>'
                f'{stamp(cls, label)}<span class="tl-dur">{dur}</span></div>']
        for tool in n["tools"]:
            body.append(f'<div class="tl-tool">🔧 <b>{e(tool["name"])}</b>(“{e(tool["query"])}”)'
                        f' → <span class="found">{e(tool["found"])}</span></div>')
        if n["status"] == "run":
            body.append('<div class="tl-wait">'
                        + ("Claude is reading the complaint…" if not n["tools"]
                           else "Claude is reading the results…") + "</div>")
        if n["summary"]:
            body.append(f'<div class="tl-sum">{e(n["summary"])}</div>')
        if n["items"]:
            body.append('<ul class="tl-items">' + "".join(f"<li>{e(x)}</li>" for x in n["items"])
                        + "</ul>")
        rows.append(f'<li class="tl-item is-{n["status"]}"><div class="tl-time">+{clock(n["t0"])}'
                    f'</div><div class="tl-rail"><span class="tl-dot"></span></div>'
                    f'<div class="tl-body">{"".join(body)}</div></li>')
    if not rows:
        rows.append('<li class="tl-item is-run"><div class="tl-time">+0:00.0</div><div class="tl-rail">'
                    '<span class="tl-dot"></span></div><div class="tl-body"><div class="tl-wait">'
                    'Starting…</div></div></li>')
    head = stamp("stamp-run", "Live") if not job.agent_done else stamp(
        "stamp-fail" if job.agent_error else "stamp-ok", "Failed" if job.agent_error else "Finished")
    return (f'<div class="tl-wrap"><div class="tl-head"><span class="kicker">Agent timeline · '
            f'{len(nodes)} step{"" if len(nodes) == 1 else "s"}</span>{head}</div>'
            f'<ol class="tl">{"".join(rows)}</ol></div>')


def checked_line(key: str, value: str, truth: str | None) -> str:
    line = (f'<div class="line"><span class="k">{e(key)}</span><span class="fill"></span>'
            f'<span class="v">{e(value)}</span></div>')
    if truth is None:
        return line
    if value == truth:
        note = stamp("stamp-ok", "✓ matches the CFPB record")
    else:
        note = stamp("stamp-fail", "✗ real: " + truth)
    return line + f'<div class="line-note">{note}</div>'


def plain_line(key: str, value: str) -> str:
    return (f'<div class="line"><span class="k">{e(key)}</span><span class="fill"></span>'
            f'<span class="v">{e(value)}</span></div>')


def receipt_html(r: dict, kind: str, truth: dict | None) -> str:
    agent = kind == "agent"
    real_outcome = truth and TRUE_TO_OUTCOME.get(truth["company_response"])
    if r["needs_human_review"]:
        decision = stamp("stamp-warn", "⚑ Human review", big=True)
    else:
        decision = stamp("stamp-ok", "✓ Ready to send", big=True)
    parts = [
        f'<div class="receipt-head"><div><div class="kicker">'
        f'{"Multi-agent workflow" if agent else "One prompt · no tools · no review"}</div>'
        f'<div class="receipt-title">{"Agent" if agent else "Baseline"}</div></div>{decision}</div>',
        checked_line("Product", r["product"], truth and truth["product"]),
        checked_line("Issue", r["issue"], truth and truth["issue"]),
        checked_line("Outcome", OUTCOME_LABEL[r["predicted_outcome"]],
                     real_outcome and OUTCOME_LABEL[real_outcome]),
    ]
    if agent:
        parts.append(plain_line("Urgency", r["urgency"]))
        parts.append(plain_line("Risk flags", ", ".join(r["risk_flags"]) or "none"))
    parts.append(plain_line("Rules cited", ", ".join(r["regulations_cited"]) or "none"))
    if agent and r.get("review_reasons"):
        parts.append('<ul class="reasons">' + "".join(f"<li>{e(x)}</li>" for x in r["review_reasons"])
                     + "</ul>")
    parts.append('<div class="rule"></div><div class="kicker">Reply to the consumer</div>')
    parts.append('<div class="reply">' + e(r["reply"]).replace("\n", "<br>") + "</div>")
    parts.append('<div class="rule"></div>')
    tokens = f"{r['input_tokens'] + r['output_tokens']:,} tokens"
    if agent:
        revs = f"{r['revisions']} revision{'' if r['revisions'] == 1 else 's'}"
        foot = [f"{r['seconds']} s", revs, f"{r['llm_calls']} Claude calls", tokens]
    else:
        foot = [f"{r['seconds']} s", "1 Claude call", tokens]
    parts.append('<div class="receipt-foot">' + "".join(f"<span>{e(x)}</span>" for x in foot)
                 + "</div>")
    return f'<div class="receipt">{"".join(parts)}</div>'


def placeholder(text: str, error: bool = False) -> str:
    if error:
        return f'<div class="placeholder">{stamp("stamp-fail", "✗ Error")}<br><br>{e(text)}</div>'
    return f'<div class="placeholder">{e(text)}</div>'


# ----------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------
examples = load_examples()
expected = os.getenv("DEMO_PASSCODE", "")

st.html(
    '<div class="rc-top">'
    '<svg width="26" height="31" viewBox="0 0 22 26" aria-hidden="true">'
    '<path d="M2 1h18v21.5l-3-2.2-3 2.2-3-2.2-3 2.2-3-2.2-3 2.2z" fill="#1c1a16"/>'
    '<path d="M6 7h10M6 11h10M6 15h5" stroke="#f5f1e8" stroke-width="1.6" stroke-linecap="round"/>'
    '<path d="M13 15.2l1.7 1.7 3.2-3.6" stroke="#f6d860" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" fill="none"/></svg>'
    '<div><div class="rc-word">Complaint Agent</div>'
    '<div class="kicker">CFPB complaint triage · one prompt vs. a multi-agent workflow</div></div>'
    f'<div class="rc-tag"><span class="kicker">Model</span><br><span class="mono">{e(MODEL)}</span>'
    '</div></div>')

with st.sidebar:
    st.header("Access")
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
    st.caption("The agent here is a Python copy of the n8n workflow, with the same prompts, "
               "tools and rules. The evaluated version runs in n8n.")


def use_example():
    ex = next((x for x in examples if x["label"] == st.session_state.example), None)
    st.session_state.company = ex["company"] if ex else ""
    st.session_state.state = ex["state"] if ex else ""
    st.session_state.narrative = ex["narrative"] if ex else ""


def current_example() -> dict | None:
    """The chosen example, unless its text was edited (then there is nothing to compare)."""
    ex = next((x for x in examples if x["label"] == st.session_state.get("example")), None)
    return ex if ex and st.session_state.get("narrative") == ex["narrative"] else None


def submit():
    job = st.session_state.get("job")
    if job is not None and job.running:
        return
    narrative = st.session_state.narrative.strip()
    if not narrative:
        st.session_state.form_error = "Enter the complaint text."
        return
    st.session_state.form_error = None
    ex = current_example()
    truth = {k: ex[k] for k in ("product", "issue", "company_response")} if ex else None
    job = Job((st.session_state.company.strip() or "Unknown",
               st.session_state.state.strip() or "Unknown", narrative), truth)
    job.start(get_client())
    st.session_state.job = job


job = st.session_state.get("job")
busy = job is not None and job.running

with st.container(border=True):
    st.selectbox("Example complaint", ["Write your own…"] + [x["label"] for x in examples],
                 key="example", on_change=use_example)
    c1, c2 = st.columns([3, 1])
    c1.text_input("Company", key="company", placeholder="e.g. WELLS FARGO & COMPANY")
    c2.text_input("State", key="state", placeholder="e.g. CA", max_chars=20)
    st.text_area("Complaint text", key="narrative", height=160,
                 placeholder="Describe the problem as the consumer would.")
    ex = current_example()
    if ex:
        st.html('<div class="kicker">Real CFPB record</div>'
                f'<div>{e(ex["product"])} · {e(ex["issue"])} · <b>{e(ex["company_response"])}</b></div>')
    b1, b2 = st.columns([1, 3], vertical_alignment="center")
    b1.button("Submit to both", type="primary", disabled=not unlocked or busy, on_click=submit,
              width="stretch")
    if not unlocked:
        b2.caption("Enter the passcode in the sidebar to submit.")
    elif busy:
        b2.caption("Running. You can scroll and click around; the run keeps going.")
    if st.session_state.get("form_error"):
        st.error(st.session_state.form_error)


@st.fragment(run_every=TICK if busy else None)
def live_view():
    """Pipeline strip, live timeline and the two receipts. Reruns every TICK s during a run."""
    job = st.session_state.get("job")
    nodes = build_nodes(job) if job else []

    st.subheader("How the agent works")
    st.html(pipeline_html(nodes))

    if job:
        if job.running:
            line = (f'{stamp("stamp-run", "Running")} <span class="mono">{clock(job.elapsed())}'
                    f'</span> elapsed')
        else:
            line = (f'{stamp("stamp-ok", "Done")} <span class="mono">{clock(job.elapsed())}</span>'
                    f' total')
        if job.baseline_done and job.baseline:
            line += f' · baseline answered in {job.baseline["seconds"]} s'
        st.html(f'<div>{line}</div>')

    left, right = st.columns([5, 6], gap="large")
    with left:
        st.subheader("Baseline")
        if not job:
            st.html(placeholder("The baseline's receipt prints here."))
        elif job.baseline_error:
            st.html(placeholder(job.baseline_error, error=True))
        elif job.baseline:
            st.html(receipt_html(job.baseline, "baseline", job.truth))
        else:
            st.html(placeholder("One Claude call, usually a few seconds…"))
    with right:
        st.subheader("Agent")
        if not job:
            st.html(placeholder("Submit a complaint to watch the agent work, step by step."))
        else:
            if job.agent:
                st.html(receipt_html(job.agent, "agent", job.truth))
                if job.agent["critic_suggestions"]:
                    with st.expander("Critic suggestions (not blocking)"):
                        for s in job.agent["critic_suggestions"]:
                            st.markdown(f"- {s}")
            elif job.agent_error:
                st.html(placeholder(job.agent_error, error=True))
            st.html(timeline_html(job, nodes))

    if busy and not job.running:
        st.rerun()      # run finished: redraw the whole page once and stop the timer


live_view()

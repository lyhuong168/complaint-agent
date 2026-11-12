"""What the complaint agent is: prompts, output formats, rules and knowledge.

Shared by every implementation so they stay identical:
  - n8n/build_workflow.py   builds the n8n (low-code) agent from these
  - webapp/agent.py         the Python copy of the agent behind the Streamlit app
  - eval/run_baseline.py    the one-prompt baseline the agent is compared against

Change a prompt or rule here, then rebuild the n8n workflow (see README).
"""
import json
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
KNOW = ROOT / "n8n" / "knowledge"
load_dotenv(ROOT / ".env")

# Set CLAUDE_MODEL in .env. The baseline (eval/run_baseline.py) reads the same
# setting, so both systems always use the same model.
MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
MAX_REVISIONS = 2              # how many times the critic may send a draft back

taxonomy = json.loads((KNOW / "taxonomy.json").read_text(encoding="utf-8"))
regulations = json.loads((KNOW / "regulations.json").read_text(encoding="utf-8"))
history = json.loads((KNOW / "company_history.json").read_text(encoding="utf-8"))

RISK_FLAGS = [
    "identity_theft", "fraud_or_scam", "servicemember", "older_consumer",
    "financial_hardship", "legal_threat", "discrimination", "foreclosure_or_repossession",
]
# Flags that always send the case to a person, whatever the agents decide.
REVIEW_FLAGS = ["servicemember", "older_consumer", "legal_threat", "discrimination",
                "foreclosure_or_repossession"]


# ----------------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------------
taxonomy_text = "\n".join(
    f"PRODUCT: {p}\n" + "\n".join(f"  - {i}" for i in issues) for p, issues in taxonomy.items()
)

TRIAGE_SYSTEM = f"""You are the triage agent in the complaints team of a US consumer finance company. \
You read one consumer complaint and classify it so the right specialist can handle it. \
Personal details in the text are masked as XXXX.

Classify by what the complaint is mainly about:
- product: one PRODUCT from the list below, copied exactly.
- issue: one issue from that product's list, copied exactly. A complaint about how a debt \
or account appears on a credit report belongs under credit reporting; a complaint about a \
collector's behavior belongs under debt collection.
- urgency: high when the consumer faces harm within days (a scheduled foreclosure sale or \
repossession, frozen money they need for living costs, fraud still draining an account); \
medium when harm is ongoing or a deadline is near; low otherwise.
- risk_flags: include a flag only when the text supports it. Allowed flags: {", ".join(RISK_FLAGS)}.
- summary: two sentences a colleague could act on.
- consumer_request: what the consumer wants the company to do.

{taxonomy_text}"""

TRIAGE_USER = """=Company: {{ $json.company }}
State: {{ $json.state }}

Complaint:
{{ $json.narrative }}"""

RESOLUTION_SYSTEM = """You are the resolution agent in the complaints team of a US consumer finance company. \
A triage agent has already classified the complaint. Your job is to research it, predict how \
it will be resolved, and draft the reply to the consumer.

Research with your tools before you write:
- search_regulations: look up the consumer finance rules for this product and problem. \
Search more than once if the complaint raises more than one problem.
- company_history: look up how this company closed its past complaints.

Then decide:
- predicted_outcome: explanation_only (the company explains and changes nothing), \
non_monetary_relief (it fixes something, such as correcting a record or stopping contact), \
or monetary_relief (it refunds or credits money). Weigh the facts of this complaint against \
the company's history; most complaints are closed with an explanation only.
- regulations_cited: the bracketed ids (for example FCRA) of the regulations your reply relies \
on. Cite only what search_regulations returned. If it returned nothing relevant, cite nothing.
- escalate: whether a person must decide this case instead of the standard complaints process. \
Most complaints do not need this. Disputes about a credit report entry, requests to validate a \
debt, fee or billing questions and routine servicing problems follow a standard procedure, even \
when the consumer is upset or the company may have made a mistake. Set escalate to true only \
when at least one of these applies: the consumer faces serious harm within days (a scheduled \
foreclosure sale or repossession, or money needed for living costs being held or lost); the \
facts conflict so badly that the standard process cannot decide; or the complaint alleges \
something the standard process cannot handle, such as discrimination, a threat of legal \
action, or misconduct by staff. Possible refunds and risk flags are already sent to a person \
by separate rules, so do not escalate for those reasons alone.

The reply is read by the consumer, who is upset and is not a lawyer. Write 120 to 200 words in \
plain language. Acknowledge the specific problem, say what the company will look into or do \
and by when where a rule sets a deadline, and name the rule that protects them. Do not admit \
legal fault, do not promise a refund or a correction that has not been decided, and do not \
invent facts, names or account details that are not in the complaint.

If the message includes feedback from the compliance reviewer, fix every point it raises. \
The feedback is about the wording of the reply. Keep your predicted_outcome and escalate \
decisions unless the feedback shows you misread the facts."""

RESOLUTION_USER = """=Company: {{ $json.company }}
State: {{ $json.state }}

Triage result:
{{ JSON.stringify($json.triage, null, 1) }}

Complaint:
{{ $json.narrative }}
{{ $json.feedback ? '\\nYour previous draft:\\n' + $json.draft.reply + '\\n\\nFeedback from the compliance reviewer (fix every point):\\n' + $json.feedback : '' }}"""

CRITIC_SYSTEM = """You are the compliance reviewer in the complaints team of a US consumer finance company. \
A colleague drafted the first reply to a consumer complaint. You decide whether it is safe to send. \
You did not write it, so judge it on the evidence in front of you.

What this reply is: the company's first written response. It acknowledges the complaint, says \
what the company will look into, and points to the rules that protect the consumer. It is not \
the final decision on the case; the team that investigates follows up separately. So the reply \
should not contain dates, refunds or outcomes the company has not decided, and you must never \
ask the writer to add them.

What happens after your review: if you approve, the reply can be sent. If you send it back, the \
writer revises it, at most twice. After that the case goes to a person, which is slow and costly \
for the team. Send a draft back only when sending it as it is would cause a real problem.

Blocking problems (the only reasons to send a draft back):
1. Invented facts: it states something about the consumer's case, account or earlier contacts \
that is not in the complaint.
2. Wrong law: it names a rule that is not in the regulation extracts, applies a rule that clearly \
does not fit the situation, or states a deadline, amount or right incorrectly.
3. Over-promising: it admits legal fault, or promises a refund, correction, date or outcome that \
has not been decided.
4. Ignores the main request: it does not address the main thing the consumer asked for at all.
5. Harmful tone: it is rude, blames the consumer, or is so legalistic that the consumer cannot \
tell what happens next.

Everything else is a suggestion, not a problem: wording that could be clearer, extra detail that \
would be nice, a secondary point covered only briefly, a rule that could be explained more fully. \
Put these in suggestions. They never block the reply. A reply does not need to be perfect to be \
safe to send.

Set verdict to approve when there is no blocking problem. Otherwise set it to revise and list \
in problems at most the 3 most serious blocking problems, each as one instruction the writer can \
act on. Never list something you consider correct."""

CRITIC_USER = """=Complaint:
{{ $json.narrative }}

Triage result:
{{ JSON.stringify($json.triage, null, 1) }}

Draft reply:
{{ $json.draft.reply }}

Regulations the draft says it relies on: {{ $json.draft.regulations_cited.join(', ') || 'none' }}

Regulation extracts (the only rules the reply may rely on):
{{ $json.cited_text || 'none' }}"""

# ----------------------------------------------------------------------------
# Output formats (JSON Schema) for the three agents
# ----------------------------------------------------------------------------
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "product": {"type": "string", "enum": list(taxonomy)},
        "issue": {"type": "string"},
        "urgency": {"type": "string", "enum": ["low", "medium", "high"]},
        "risk_flags": {"type": "array", "items": {"type": "string", "enum": RISK_FLAGS}},
        "summary": {"type": "string"},
        "consumer_request": {"type": "string"},
    },
    "required": ["product", "issue", "urgency", "risk_flags", "summary", "consumer_request"],
    "additionalProperties": False,
}
RESOLUTION_SCHEMA = {
    "type": "object",
    "properties": {
        "predicted_outcome": {"type": "string",
                              "enum": ["explanation_only", "non_monetary_relief", "monetary_relief"]},
        "outcome_reasoning": {"type": "string"},
        "regulations_cited": {"type": "array", "items": {"type": "string"}},
        "escalate": {"type": "boolean"},
        "escalation_reason": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["predicted_outcome", "outcome_reasoning", "regulations_cited",
                 "escalate", "escalation_reason", "reply"],
    "additionalProperties": False,
}
CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise"]},
        "problems": {"type": "array", "items": {"type": "string"}},
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "problems", "suggestions"],
    "additionalProperties": False,
}

# ----------------------------------------------------------------------------
# Tools the resolution agent can call (the code for them is in each implementation)
# ----------------------------------------------------------------------------
REG_TOOL_DESCRIPTION = (
    "Search the US consumer finance regulation knowledge base. Input: a short description of "
    "the product and the problem, for example 'credit card billing dispute unauthorized charge'. "
    "Returns up to three regulations with their id, citation and key rules.")
HISTORY_TOOL_DESCRIPTION = (
    "Look up how a company closed its past consumer complaints. Input: the company name exactly "
    "as given in the case. Returns the number of past complaints and the share closed with an "
    "explanation only, with non-monetary relief, with monetary relief, and answered late.")

# Real complaints from the test sample for the demo pages' "Example" menu.
EXAMPLE_IDS = [
    ("Fraud: $7,500 branch withdrawal (older consumer)", "8018321"),
    ("Credit report: accounts that are not mine", "7552447"),
    ("Debt collector calling several times a day", "7673803"),
    ("Mortgage: escrow surplus not refunded", "7776127"),
    ("Card scam, servicemember", "7711475"),
]

# ----------------------------------------------------------------------------
# Baseline: ONE prompt, no tools, no critic, no rules (what the agent is compared against)
# ----------------------------------------------------------------------------
BASELINE_SYSTEM = f"""You work in the complaints team of a US consumer finance company. \
For the complaint you are given, do the whole job in one pass:

1. Classify it. Choose the product, then choose one issue from that product's list. \
Copy both exactly as written below.
2. Predict how the company will close it: explanation_only (the company explains but \
changes nothing), non_monetary_relief (it fixes something, such as correcting a record), \
or monetary_relief (it refunds or credits money).
3. Decide whether a human must review the case before any reply is sent.
4. Draft the reply to the consumer. Name any US consumer finance law that applies.

Complaint text has personal details replaced with XXXX.

{taxonomy_text}"""


class BaselineResult(BaseModel):
    model_config = {"title": "Result"}   # the schema title sent to Claude, unchanged since the evaluation
    product: Literal[tuple(taxonomy)]  # type: ignore[valid-type]
    issue: str
    predicted_outcome: Literal["explanation_only", "non_monetary_relief", "monetary_relief"]
    needs_human_review: bool
    regulations_cited: list[str]
    reply: str

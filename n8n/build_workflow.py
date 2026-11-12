"""Generate the n8n workflow file for the complaint agent.

The workflow is written here in Python (prompts, tool code, wiring) and saved
as JSON that n8n can import. Edit this file and rerun it rather than editing
the JSON by hand; then re-import the JSON into n8n.

Usage:
    python n8n/build_workflow.py

Output:
    n8n/complaint_agent.workflow.json

Flow (one complaint per run):

    Webhook -> Prepare case -> TRIAGE AGENT -> Validate labels -> Case state
            -> RESOLUTION AGENT (tools: search_regulations, company_history)
            -> Check citations -> COMPLIANCE CRITIC -> Decide next
            -> Revise?  yes: back to Case state with the critic's feedback
                        no:  Human review? -> queue for a person / ready to send
            -> Respond to Webhook
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
KNOW = HERE / "knowledge"
OUT = HERE / "complaint_agent.workflow.json"
load_dotenv(HERE.parent / ".env")

# Set CLAUDE_MODEL in .env. The baseline (eval/run_baseline.py) reads the same
# setting, so both systems always use the same model.
MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
MAX_REVISIONS = 2              # how many times the critic may send a draft back
WORKFLOW_ID = "ComplaintAgent01"
CREDENTIAL = {"id": "AnthropicCred001", "name": "Anthropic account"}
# The POST webhooks only answer requests that send the header X-Passcode with
# the value of DEMO_PASSCODE (stored in n8n by setup_agent.ps1 or deploy/start.sh).
# This stops strangers from spending API credit when the app is online.
PASSCODE = {"id": "DemoPasscode001", "name": "Demo passcode"}
PASSCODE_AUTH = {"authentication": "headerAuth"}

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


def js(value) -> str:
    """A Python value as a JavaScript literal."""
    return json.dumps(value, ensure_ascii=False)


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
# Tool code (JavaScript run by n8n when the resolution agent calls a tool)
# ----------------------------------------------------------------------------
REG_TOOL_JS = """// Keyword search over the regulation knowledge base (n8n/knowledge/regulations.json).
const KB = __KB__;
const STOP = new Set(['the','and','for','with','that','this','from','have','has','was','were','not','are','but','they','their','consumer','company','complaint','about','account']);
const q = String(query || '').toLowerCase();
const words = [...new Set(q.split(/[^a-z0-9]+/).filter(w => w.length > 3 && !STOP.has(w)))];
const scored = KB.map(r => {
  let score = 0;
  if (words.includes(r.id.toLowerCase())) score += 5;   // the agent asked for it by id, e.g. "fcra"
  for (const k of r.keywords) if (q.includes(k)) score += 3;
  for (const p of r.products) if (q.includes(p.toLowerCase())) score += 2;
  const text = (r.name + ' ' + r.rules.join(' ')).toLowerCase();
  for (const w of words) if (text.includes(w)) score += 1;
  return { r, score };
}).filter(x => x.score >= 2).sort((a, b) => b.score - a.score).slice(0, 3);
if (!scored.length) {
  return 'No matching regulation in the knowledge base. Do not cite a regulation for this point.';
}
return scored.map(({ r }) =>
  '[' + r.id + '] ' + r.name + ' (' + r.citation + ')\\n' + r.rules.map(x => '- ' + x).join('\\n')
).join('\\n\\n');
""".replace("__KB__", js(regulations))

# company -> [past complaints, % monetary relief, % non-monetary relief, % late responses]
history_compact = {
    name: [h["past_complaints"], h["pct_monetary_relief"], h["pct_non_monetary_relief"],
           h["pct_late_response"]]
    for name, h in history.items()
}
HISTORY_TOOL_JS = """// How a company closed its complaints before Sept 2023 (n8n/knowledge/company_history.json).
const HISTORY = __HISTORY__;
const norm = s => String(s || '').toUpperCase().replace(/[^A-Z0-9 ]/g, ' ').replace(/\\s+/g, ' ').trim();
const q = norm(query);
if (!q) return 'Give the company name.';
let name = Object.keys(HISTORY).find(k => norm(k) === q);
if (!name) {
  const hits = Object.keys(HISTORY).filter(k => norm(k).includes(q) || q.includes(norm(k)));
  hits.sort((a, b) => HISTORY[b][0] - HISTORY[a][0]);
  name = hits[0];
}
if (!name) return 'No complaint history on file for "' + query + '". Use the general pattern: about 76% of complaints close with an explanation only, 19% with non-monetary relief and 4% with monetary relief.';
const [n, money, nonMoney, late] = HISTORY[name];
const explain = Math.max(0, 100 - money - nonMoney).toFixed(1);
return name + ': ' + n + ' past complaints. Closed with explanation only: ' + explain +
  '%. Non-monetary relief: ' + nonMoney + '%. Monetary relief: ' + money +
  '%. Late responses: ' + late + '%.';
""".replace("__HISTORY__", js(history_compact))

# ----------------------------------------------------------------------------
# Code nodes (plain JavaScript steps between the agents)
# ----------------------------------------------------------------------------
PREPARE_JS = """// Input guardrail: accept the request only if it has complaint text.
const body = $input.first().json.body || {};
const narrative = String(body.narrative || '').trim();
if (!narrative) throw new Error('Request must include "narrative" (the complaint text).');
return [{ json: {
  complaint_id: String(body.complaint_id || ''),
  company: String(body.company || 'Unknown'),
  state: String(body.state || 'Unknown'),
  narrative,
  started_at: Date.now(),
} }];
"""

VALIDATE_JS = """// Guardrail: the triage labels must come from the official CFPB lists.
const TAXONOMY = __TAXONOMY__;
const kase = $('Prepare case').first().json;
const triage = { ...$input.first().json.output };
const allowed = TAXONOMY[triage.product] || [];
let labelValid = allowed.includes(triage.issue);
if (!labelValid && allowed.length) {
  // Repair near-misses (case or punctuation differences) instead of failing.
  const norm = s => String(s).toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const match = allowed.find(i => norm(i) === norm(triage.issue));
  if (match) { triage.issue = match; labelValid = true; }
}
return [{ json: {
  ...kase,
  triage,
  label_valid: labelValid,
  revision: 0,
  feedback: '',
  draft: null,
  trace: ['triage: ' + triage.product + ' / ' + triage.issue + ' (urgency ' + triage.urgency + ')'],
} }];
""".replace("__TAXONOMY__", js(taxonomy))

STATE_JS = """// The case as it enters the resolution agent. Runs once for the first draft
// and again each time the critic sends the draft back.
return $input.all();
"""

CITATIONS_JS = """// Guardrail: every regulation the draft cites must exist in the knowledge base.
// The matching extracts are passed to the critic so it can check the wording.
const KB = __KB__;
const state = $('Case state').last().json;
const draft = $input.first().json.output;
const byId = Object.fromEntries(KB.map(r => [r.id, r]));
const cited = (draft.regulations_cited || []).map(c => String(c).replace(/[\\[\\]]/g, '').trim().toUpperCase());
const known = cited.filter(c => byId[c]);
const unknown = cited.filter(c => !byId[c]);
const citedText = known.map(c => '[' + c + '] ' + byId[c].name + ' (' + byId[c].citation + ')\\n' +
  byId[c].rules.map(x => '- ' + x).join('\\n')).join('\\n\\n');
return [{ json: {
  ...state,
  draft: { ...draft, regulations_cited: cited },
  unknown_citations: unknown,
  cited_text: citedText,
  trace: [...state.trace, 'draft ' + (state.revision + 1) + ': predicts ' + draft.predicted_outcome +
    ', cites ' + (cited.join(', ') || 'nothing') + (unknown.length ? ' (NOT IN KNOWLEDGE BASE: ' + unknown.join(', ') + ')' : '')],
} }];
""".replace("__KB__", js(regulations))

DECIDE_JS = """// Routing: revise the draft, send the case to a person, or mark it ready to send.
const MAX_REVISIONS = __MAX__;
const REVIEW_FLAGS = __REVIEW_FLAGS__;
const state = $('Check citations').last().json;
const critic = $input.first().json.output;
// Only blocking problems count. A "revise" verdict with no problem listed is
// treated as an approval, since the writer would have nothing to fix.
const problems = [...(critic.problems || [])].slice(0, 3);
if (state.unknown_citations.length) {
  problems.push('Remove or replace these citations, which are not in the knowledge base: ' + state.unknown_citations.join(', '));
}
const approved = problems.length === 0;
const trace = [...state.trace, 'critic on draft ' + (state.revision + 1) + ': ' +
  (approved ? 'approved' : 'revise (' + problems.length + ' problem' + (problems.length === 1 ? '' : 's') + ')')];

if (!approved && state.revision < MAX_REVISIONS) {
  return [{ json: { ...state, next: 'revise', revision: state.revision + 1,
    feedback: problems.map((p, i) => (i + 1) + '. ' + p).join('\\n'), trace } }];
}

// Final decision. These rules are fixed code, not left to the model.
const reasons = [];
if (!approved) reasons.push('Compliance critic did not approve the reply after ' + MAX_REVISIONS + ' revisions');
if (state.draft.escalate) reasons.push('Resolution agent escalated: ' + state.draft.escalation_reason);
if (state.triage.urgency === 'high') reasons.push('High urgency');
if (state.draft.predicted_outcome === 'monetary_relief') reasons.push('Money may be owed to the consumer');
for (const f of state.triage.risk_flags) if (REVIEW_FLAGS.includes(f)) reasons.push('Risk flag: ' + f);
if (!state.label_valid) reasons.push('Triage issue label is not in the official list');
const needsHuman = reasons.length > 0;
trace.push(needsHuman ? 'routed to human review' : 'ready to send');

return [{ json: {
  next: needsHuman ? 'human_review' : 'ready_to_send',
  complaint_id: state.complaint_id,
  status: needsHuman ? 'needs_human_review' : 'ready_to_send',
  product: state.triage.product,
  issue: state.triage.issue,
  urgency: state.triage.urgency,
  risk_flags: state.triage.risk_flags,
  summary: state.triage.summary,
  consumer_request: state.triage.consumer_request,
  predicted_outcome: state.draft.predicted_outcome,
  outcome_reasoning: state.draft.outcome_reasoning,
  regulations_cited: state.draft.regulations_cited,
  needs_human_review: needsHuman,
  review_reasons: reasons,
  reply: state.draft.reply,
  critic_approved: approved,
  critic_problems: approved ? [] : problems,
  critic_suggestions: critic.suggestions || [],
  revisions: state.revision,
  agent_runs: 1 + 2 * (state.revision + 1),
  seconds: Math.round((Date.now() - state.started_at) / 100) / 10,
  trace,
} }];
""".replace("__MAX__", str(MAX_REVISIONS)).replace("__REVIEW_FLAGS__", js(REVIEW_FLAGS))

# ----------------------------------------------------------------------------
# Nodes
# ----------------------------------------------------------------------------
nodes = []
connections = {}


def node(name, type_, version, pos, params, **extra):
    nodes.append({
        "parameters": params, "id": f"node-{len(nodes) + 1:02d}", "name": name,
        "type": type_, "typeVersion": version, "position": list(pos), **extra,
    })
    return name


def link(src, dst, kind="main", out=0):
    outputs = connections.setdefault(src, {}).setdefault(kind, [])
    while len(outputs) <= out:
        outputs.append([])
    outputs[out].append({"node": dst, "type": kind, "index": 0})


def code(name, pos, source):
    return node(name, "n8n-nodes-base.code", 2, pos, {"jsCode": source})


def agent(name, pos, system, user, max_iterations):
    return node(name, "@n8n/n8n-nodes-langchain.agent", 3.1, pos, {
        "promptType": "define", "text": user, "hasOutputParser": True,
        "options": {"systemMessage": system, "maxIterations": max_iterations},
    })


def parser(name, pos, schema):
    return node(name, "@n8n/n8n-nodes-langchain.outputParserStructured", 1.3, pos, {
        "schemaType": "manual", "inputSchema": json.dumps(schema, indent=2),
    })


def tool(name, pos, description, source):
    return node(name, "@n8n/n8n-nodes-langchain.toolCode", 1.3, pos, {
        "description": description, "language": "javaScript", "jsCode": source,
    })


def note(text, pos, width, height, color):
    node(f"Note {len(nodes) + 1}", "n8n-nodes-base.stickyNote", 1, pos,
         {"content": text, "width": width, "height": height, "color": color})


def is_equal(name, pos, field, value):
    return node(name, "n8n-nodes-base.if", 2.2, pos, {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
            "conditions": [{
                "id": f"cond-{name}", "leftValue": f"={{{{ $json.{field} }}}}", "rightValue": value,
                "operator": {"type": "string", "operation": "equals"},
            }],
            "combinator": "and",
        },
        "options": {},
    })


Y = 300
webhook = node("Webhook", "n8n-nodes-base.webhook", 2, (0, Y), {
    "httpMethod": "POST", "path": "complaint", "responseMode": "responseNode", "options": {},
    **PASSCODE_AUTH,
}, webhookId="complaint-agent-webhook", credentials={"httpHeaderAuth": PASSCODE})
prepare = code("Prepare case", (220, Y), PREPARE_JS)
triage = agent("Triage Agent", (440, Y), TRIAGE_SYSTEM, TRIAGE_USER, 3)
validate = code("Validate labels", (800, Y), VALIDATE_JS)
state = code("Case state", (1020, Y), STATE_JS)
resolution = agent("Resolution Agent", (1240, Y), RESOLUTION_SYSTEM, RESOLUTION_USER, 8)
citations = code("Check citations", (1680, Y), CITATIONS_JS)
critic = agent("Compliance Critic", (1900, Y), CRITIC_SYSTEM, CRITIC_USER, 3)
decide = code("Decide next", (2260, Y), DECIDE_JS)
revise = is_equal("Revise?", (2480, Y), "next", "revise")
human = is_equal("Human review?", (2700, Y + 100), "next", "human_review")
queue = node("Queue for human review", "n8n-nodes-base.noOp", 1, (2940, Y), {})
ready = node("Ready to send", "n8n-nodes-base.noOp", 1, (2940, Y + 200), {})
respond = node("Respond to Webhook", "n8n-nodes-base.respondToWebhook", 1.1, (3180, Y + 100), {
    "respondWith": "firstIncomingItem", "options": {},
})

model = node("Claude", "@n8n/n8n-nodes-langchain.lmChatAnthropic", 1.6, (1240, Y + 520), {
    "model": {"__rl": True, "mode": "id", "value": MODEL},
    "options": {"maxTokensToSample": 8000, "thinkingMode": "adaptive", "effort": "medium"},
}, credentials={"anthropicApi": CREDENTIAL})
triage_out = parser("Triage format", (600, Y + 220), TRIAGE_SCHEMA)
resolution_out = parser("Resolution format", (1500, Y + 220), RESOLUTION_SCHEMA)
critic_out = parser("Critic format", (2060, Y + 220), CRITIC_SCHEMA)
reg_tool = tool(
    "search_regulations", (1240, Y + 220),
    "Search the US consumer finance regulation knowledge base. Input: a short description of "
    "the product and the problem, for example 'credit card billing dispute unauthorized charge'. "
    "Returns up to three regulations with their id, citation and key rules.",
    REG_TOOL_JS)
history_tool = tool(
    "company_history", (1370, Y + 220),
    "Look up how a company closed its past consumer complaints. Input: the company name exactly "
    "as given in the case. Returns the number of past complaints and the share closed with an "
    "explanation only, with non-monetary relief, with monetary relief, and answered late.",
    HISTORY_TOOL_JS)

for a, b in [(webhook, prepare), (prepare, triage), (triage, validate), (validate, state),
             (state, resolution), (resolution, citations), (citations, critic),
             (critic, decide), (decide, revise), (queue, respond), (ready, respond)]:
    link(a, b)
link(revise, state, out=0)      # true: back for another draft
link(revise, human, out=1)      # false: finished drafting
link(human, queue, out=0)
link(human, ready, out=1)
for a in (triage, resolution, critic):
    link(model, a, kind="ai_languageModel")
link(triage_out, triage, kind="ai_outputParser")
link(resolution_out, resolution, kind="ai_outputParser")
link(critic_out, critic, kind="ai_outputParser")
link(reg_tool, resolution, kind="ai_tool")
link(history_tool, resolution, kind="ai_tool")

note("## 1. Triage\nClassifies the complaint (product, issue, urgency, risk flags). "
     "A code guardrail then checks the labels against the official CFPB lists.",
     (380, Y - 200), 560, 160, 4)
note("## 2. Resolution (tool use)\nResearches with two tools, predicts the outcome and drafts the reply. "
     "Runs again with the critic's feedback when a draft is sent back.",
     (980, Y - 200), 620, 160, 5)
note("## 3. Reflection\nA separate critic checks the draft against the complaint and the cited rules. "
     f"Up to {MAX_REVISIONS} revisions.",
     (1640, Y - 200), 760, 160, 6)
note("## 4. Human in the loop\nFixed rules decide whether a person must approve: "
     "urgency, risk flags, money owed, or a reply the critic never approved.",
     (2440, Y - 200), 900, 160, 3)

workflow = {
    "id": WORKFLOW_ID,
    "name": "Complaint Triage and Resolution Agent",
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1"},
    "active": False,
    "pinData": {},
}
OUT.write_text(json.dumps(workflow, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Wrote {OUT.name}: {len(nodes)} nodes, {OUT.stat().st_size // 1024} KB")


# ============================================================================
# Second workflow: the demo page and the baseline it compares against.
#
# Kept separate so the agent's canvas keeps a single entry point, and clicking
# "Execute workflow" there always listens for the agent request.
#
#   GET  /webhook/app        the input page (n8n/ui/index.html)
#   POST /webhook/baseline   one Claude call, same request as eval/run_baseline.py
# ============================================================================
import csv
import sys

sys.path.insert(0, str(HERE.parent / "eval"))
from anthropic.lib._parse._transform import transform_schema  # noqa: E402  (what the SDK itself sends)
from run_baseline import SYSTEM as BASELINE_SYSTEM, Result as BaselineResult  # noqa: E402

DEMO_OUT = HERE / "demo_ui.workflow.json"
DEMO_ID = "ComplaintDemoUI01"
API_VERSION = "2026-10-21"   # the anthropic-version header the Python SDK sends

# Real complaints from the test sample for the page's "Example" menu.
EXAMPLE_IDS = [
    ("Fraud: $7,500 branch withdrawal (older consumer)", "8018321"),
    ("Credit report: accounts that are not mine", "7552447"),
    ("Debt collector calling several times a day", "7673803"),
    ("Mortgage: escrow surplus not refunded", "7776127"),
    ("Card scam, servicemember", "7711475"),
]
with open(HERE.parent / "data" / "complaints_sample.csv", encoding="utf-8") as f:
    by_id = {r["complaint_id"]: r for r in csv.DictReader(f)}
examples = [{
    "label": label, "complaint_id": cid, "company": by_id[cid]["company"],
    "state": by_id[cid]["state"], "narrative": by_id[cid]["narrative"],
    "product": by_id[cid]["product"], "issue": by_id[cid]["issue"],
    "outcome": by_id[cid]["company_response"], "tags": by_id[cid]["tags"],
} for label, cid in EXAMPLE_IDS]

page = (HERE / "ui" / "index.html").read_text(encoding="utf-8")
page = page.replace("/*__EXAMPLES__*/[]", js(examples).replace("</", "<\\/"))

BASELINE_PREPARE_JS = """// Build the same request eval/run_baseline.py sends with the Anthropic SDK.
const SYSTEM = __SYSTEM__;
const SCHEMA = __SCHEMA__;
const body = $input.first().json.body || {};
const narrative = String(body.narrative || '').trim();
if (!narrative) throw new Error('Request must include "narrative" (the complaint text).');
const user = 'Company: ' + String(body.company || '') + '\\nState: ' + String(body.state || '') +
  '\\n\\nComplaint:\\n' + narrative;
return [{ json: {
  complaint_id: String(body.complaint_id || ''),
  started_at: Date.now(),
  request: {
    model: __MODEL__,
    max_tokens: 8000,
    system: SYSTEM,
    messages: [{ role: 'user', content: user }],
    output_config: { format: { type: 'json_schema', schema: SCHEMA } },
  },
} }];
""".replace("__SYSTEM__", js(BASELINE_SYSTEM)) \
   .replace("__SCHEMA__", js(transform_schema(BaselineResult.model_json_schema()))) \
   .replace("__MODEL__", js(MODEL))

BASELINE_READ_JS = """// Turn Claude's answer into the same fields eval/run_baseline.py saves.
const res = $input.first().json;
const prep = $('Prepare baseline request').first().json;
if (res.stop_reason !== 'end_turn') throw new Error('Claude stopped early: ' + res.stop_reason);
const block = (res.content || []).find(b => b.type === 'text');
if (!block) throw new Error('Claude returned no text.');
return [{ json: {
  ...JSON.parse(block.text),
  complaint_id: prep.complaint_id,
  llm_calls: 1,
  input_tokens: res.usage.input_tokens,
  output_tokens: res.usage.output_tokens,
  seconds: Math.round((Date.now() - prep.started_at) / 100) / 10,
  model: res.model,
} }];
"""

nodes, connections = [], {}
Y = 200
app_hook = node("Open page", "n8n-nodes-base.webhook", 2, (0, Y), {
    "httpMethod": "GET", "path": "app", "responseMode": "responseNode", "options": {},
}, webhookId="complaint-demo-page")
app_reply = node("Send page", "n8n-nodes-base.respondToWebhook", 1.1, (240, Y), {
    "respondWith": "text", "responseBody": page,
    "options": {"responseHeaders": {"entries": [
        {"name": "Content-Type", "value": "text/html; charset=utf-8"},
        {"name": "Cache-Control", "value": "no-store"},
    ]}},
})
link(app_hook, app_reply)

Y = 480
base_hook = node("Baseline request", "n8n-nodes-base.webhook", 2, (0, Y), {
    "httpMethod": "POST", "path": "baseline", "responseMode": "responseNode", "options": {},
    **PASSCODE_AUTH,
}, webhookId="complaint-demo-baseline", credentials={"httpHeaderAuth": PASSCODE})
base_prep = code("Prepare baseline request", (240, Y), BASELINE_PREPARE_JS)
base_call = node("Claude (one call)", "n8n-nodes-base.httpRequest", 4.2, (480, Y), {
    "method": "POST",
    "url": "https://api.anthropic.com/v1/messages",
    "authentication": "predefinedCredentialType",
    "nodeCredentialType": "anthropicApi",
    "sendHeaders": True,
    "headerParameters": {"parameters": [{"name": "anthropic-version", "value": API_VERSION}]},
    "sendBody": True,
    "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify($json.request) }}",
    "options": {"timeout": 300000},
}, credentials={"anthropicApi": CREDENTIAL})
base_read = code("Read answer", (720, Y), BASELINE_READ_JS)
base_reply = node("Send baseline answer", "n8n-nodes-base.respondToWebhook", 1.1, (960, Y), {
    "respondWith": "firstIncomingItem", "options": {},
})
for a, b in [(base_hook, base_prep), (base_prep, base_call), (base_call, base_read), (base_read, base_reply)]:
    link(a, b)

note("## Demo page\nOpen /webhook/app in a browser (locally: http://localhost:5678/webhook/app). "
     "Enter the passcode (DEMO_PASSCODE) once.", (-40, 40), 520, 120, 4)
note("## Baseline for the side-by-side\nOne Claude call with the same prompt and output format as "
     "eval/run_baseline.py. No tools, no critic, no rules.", (-40, 320), 1240, 130, 5)

demo = {
    "id": DEMO_ID, "name": "Complaint Demo Page and Baseline", "nodes": nodes,
    "connections": connections, "settings": {"executionOrder": "v1"},
    "active": False, "pinData": {},
}
DEMO_OUT.write_text(json.dumps(demo, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Wrote {DEMO_OUT.name}: {len(nodes)} nodes, {DEMO_OUT.stat().st_size // 1024} KB")

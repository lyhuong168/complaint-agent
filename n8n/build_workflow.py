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
from pathlib import Path

HERE = Path(__file__).resolve().parent
KNOW = HERE / "knowledge"
OUT = HERE / "complaint_agent.workflow.json"

MODEL = "claude-opus-5-5"      # keep the same as CLAUDE_MODEL used by eval/run_baseline.py
MAX_REVISIONS = 2              # how many times the critic may send a draft back
WORKFLOW_ID = "ComplaintAgent01"
CREDENTIAL = {"id": "AnthropicCred001", "name": "Anthropic account"}

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
- escalate: true when a person should decide, for example the facts are unclear, the consumer \
may be owed money, or the consumer is at risk of serious harm.

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
You check a colleague's draft reply before it can be sent to a consumer. You did not write it, \
so judge it on the evidence in front of you.

Check each point:
1. Grounded: every factual statement about the consumer's case appears in the complaint. \
Nothing is invented.
2. Citations: every rule the reply names is in the regulation extracts provided, and the reply \
states it correctly (deadlines, amounts, who it applies to). A rule that is named but missing \
from the extracts fails this check.
3. No over-promising: the reply does not admit legal fault or promise a refund, correction or \
outcome that has not been decided.
4. Responsive: the reply addresses what the consumer actually asked for and gives a next step.
5. Tone: plain, respectful, not defensive, and understandable by a non-lawyer.

Set verdict to approve only when all five pass. Otherwise set it to revise and list each \
problem as an instruction the writer can act on."""

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
    },
    "required": ["verdict", "problems"],
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
const problems = [...(critic.problems || [])];
if (state.unknown_citations.length) {
  problems.push('Remove or replace these citations, which are not in the knowledge base: ' + state.unknown_citations.join(', '));
}
const approved = critic.verdict === 'approve' && state.unknown_citations.length === 0;
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
}, webhookId="complaint-agent-webhook")
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

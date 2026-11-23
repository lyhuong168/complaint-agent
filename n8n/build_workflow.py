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
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "complaint_agent.workflow.json"

# Prompts, output formats, rules and knowledge live in shared/spec.py, so the
# Python copy of the agent (webapp/) uses exactly the same ones.
sys.path.insert(0, str(HERE.parent))
from shared.spec import (  # noqa: E402
    CRITIC_SCHEMA, CRITIC_SYSTEM, CRITIC_USER, EXAMPLE_IDS, HISTORY_TOOL_DESCRIPTION,
    MAX_REVISIONS, MODEL, REG_TOOL_DESCRIPTION, RESOLUTION_SCHEMA, RESOLUTION_SYSTEM,
    RESOLUTION_USER, REVIEW_FLAGS, TRIAGE_SCHEMA, TRIAGE_SYSTEM, TRIAGE_USER,
    history, regulations, taxonomy,
)

WORKFLOW_ID = "ComplaintAgent01"
CREDENTIAL = {"id": "AnthropicCred001", "name": "Anthropic account"}
# The POST webhooks only answer requests that send the header X-Passcode with
# the value of DEMO_PASSCODE (stored in n8n by setup_agent.ps1).
# This stops strangers from spending API credit when the app is online.
PASSCODE = {"id": "DemoPasscode001", "name": "Demo passcode"}
PASSCODE_AUTH = {"authentication": "headerAuth"}


def js(value) -> str:
    """A Python value as a JavaScript literal."""
    return json.dumps(value, ensure_ascii=False)


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
    "search_regulations", (1240, Y + 220), REG_TOOL_DESCRIPTION, REG_TOOL_JS)
history_tool = tool(
    "company_history", (1370, Y + 220), HISTORY_TOOL_DESCRIPTION, HISTORY_TOOL_JS)

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

from anthropic.lib._parse._transform import transform_schema  # noqa: E402  (what the SDK itself sends)

from shared.spec import BASELINE_SYSTEM, BaselineResult  # noqa: E402

DEMO_OUT = HERE / "demo_ui.workflow.json"
DEMO_ID = "ComplaintDemoUI01"
API_VERSION = "2026-10-21"   # the anthropic-version header the Python SDK sends

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

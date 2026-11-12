// Complaint Agent page. Plain JavaScript, no build step.
// Sends the complaint to POST /api/run and reads the Server-Sent Events stream:
// every agent step is added to the timeline the moment it arrives, without redrawing
// anything else. Everything that comes from Claude or the user is set with textContent.
"use strict";

const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

const STEPS = [
  ["triage", "Triage agent", "Specialised agent", "Picks the product, issue, urgency and risk flags"],
  ["labels", "Label check", "Guardrail", "Code rejects labels that are not on the CFPB lists"],
  ["resolution", "Resolution agent", "Tool use", "Searches regulations and company history, predicts the outcome, drafts the reply"],
  ["citations", "Citation check", "Guardrail", "Code flags any cited rule that is not in the knowledge base"],
  ["critic", "Compliance critic", "Reflection", "A second agent reviews the draft and can send it back"],
  ["routing", "Routing rules", "Human in the loop", "Fixed rules send urgent, risky or money cases to a person"],
];
const CONCEPT = Object.fromEntries(STEPS.map((s) => [s[0], s[2]]));
const CODE_STEPS = new Set(["labels", "citations", "routing"]);
const STATUS = {
  ok: ["stamp-ok", "✓ Done"], warn: ["stamp-warn", "! Flagged"], fail: ["stamp-fail", "✗ Failed"],
  run: ["stamp-run", "Working"], wait: ["stamp-wait", "Waiting"],
};
const OUTCOME = { explanation_only: "Explanation only", non_monetary_relief: "Non-monetary relief", monetary_relief: "Monetary relief" };
const TRUE_TO_OUTCOME = {
  "Closed with explanation": "explanation_only",
  "Closed with non-monetary relief": "non_monetary_relief",
  "Closed with monetary relief": "monetary_relief",
};
const STORE_KEY = "complaint-agent.passcode";

const app = { cfg: null, passcode: "", unlocked: false, running: false, run: null, ticker: null };

// ---------------------------------------------------------------- helpers
function stamp(kind, text, big) {
  const s = el("span", "stamp " + kind + (big ? " stamp-big" : ""));
  if (kind === "stamp-run") s.append(el("span", "dot-live"));
  s.append(document.createTextNode(text));
  return s;
}
function clock(sec) {
  const m = Math.floor(sec / 60);
  return `${m}:${(sec % 60).toFixed(1).padStart(4, "0")}`;
}
function storeGet() { try { return localStorage.getItem(STORE_KEY) || ""; } catch { return ""; } }
function storeSet(v) { try { v ? localStorage.setItem(STORE_KEY, v) : localStorage.removeItem(STORE_KEY); } catch { /* blocked */ } }
function showError(msg) { const a = $("formError"); a.textContent = msg || ""; a.hidden = !msg; }
async function errorText(res) {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return "Check the complaint: " + body.detail.map((d) => d.msg).join("; ");
  } catch { /* not JSON */ }
  return `The server answered ${res.status}.`;
}

// ---------------------------------------------------------------- passcode
function setLock(state, note) {
  const holder = $("lockState");
  holder.replaceChildren();
  $("lock").classList.toggle("is-open", state === "open");
  if (state === "open") holder.append(stamp("stamp-ok", "✓ Unlocked"));
  if (state === "bad") holder.append(stamp("stamp-fail", note || "✗ Wrong passcode"));
  $("lockBtn").textContent = state === "open" ? "Lock" : "Unlock";
  app.unlocked = state === "open";
  refreshForm();
}
async function tryUnlock(code, quiet) {
  if (!code) { setLock("closed"); return; }
  try {
    const res = await fetch("/api/unlock", { method: "POST", headers: { "X-Passcode": code } });
    if (res.status === 204) { app.passcode = code; storeSet(code); setLock("open"); return; }
    storeSet("");
    if (quiet) { setLock("closed"); return; }
    setLock("bad", res.status === 403 ? "✗ Wrong passcode" : "✗ " + await errorText(res));
  } catch {
    if (!quiet) setLock("bad", "✗ Server not reachable");
  }
}
$("lock").addEventListener("submit", (e) => {
  e.preventDefault();
  if (app.unlocked) { app.passcode = ""; storeSet(""); $("passcode").value = ""; setLock("closed"); return; }
  tryUnlock($("passcode").value.trim(), false);
});

// ---------------------------------------------------------------- form
function currentExample() {
  const i = $("example").value;
  if (i === "" || !app.cfg) return null;
  const ex = app.cfg.examples[Number(i)];
  return ex && $("narrative").value === ex.narrative ? ex : null;
}
function refreshForm() {
  const ex = currentExample();
  const truth = $("truth");
  truth.replaceChildren();
  if (ex) {
    truth.append(el("span", "kicker", "Real CFPB record "), document.createElement("br"),
      document.createTextNode(`${ex.truth.product} · ${ex.truth.issue} · `), el("b", null, ex.truth.company_response));
  }
  truth.hidden = !ex;
  const n = $("narrative").value.length, max = app.cfg ? app.cfg.max_chars : 8000;
  $("count").textContent = n ? `${n.toLocaleString()} / ${max.toLocaleString()} characters` : "";
  $("submit").disabled = !app.unlocked || app.running || !app.cfg;
  for (const id of ["example", "company", "state", "narrative"]) $(id).disabled = app.running;
  $("hint").textContent = !app.unlocked ? "Enter the passcode at the top to submit."
    : app.running ? "Running. Each step appears below as it happens." : "Both answers usually take under a minute.";
}
$("example").addEventListener("change", () => {
  const ex = app.cfg && $("example").value !== "" ? app.cfg.examples[Number($("example").value)] : null;
  $("company").value = ex ? ex.company : "";
  $("state").value = ex ? ex.state : "";
  $("narrative").value = ex ? ex.narrative : "";
  showError("");
  refreshForm();
});
$("narrative").addEventListener("input", refreshForm);

// ---------------------------------------------------------------- pipeline strip
const pipeCells = {};
function buildPipe() {
  const grid = el("div", "pipe");
  STEPS.forEach(([key, name, concept, desc], i) => {
    const cell = el("div", "pipe-step is-wait");
    const s = el("div", "s");
    cell.append(el("div", "n", `${String(i + 1).padStart(2, "0")} · ${concept}`), el("div", "t", name), el("div", "d", desc), s);
    pipeCells[key] = { cell, s };
    grid.append(cell);
  });
  const loop = el("div", "pipe-loop", "↺ the critic can send the draft back to the resolution agent, at most "
    + (app.cfg ? app.cfg.max_revisions : 2) + " times");
  $("pipe").replaceChildren(grid, loop);
  renderPipe();
}
function renderPipe() {
  const nodes = app.run ? app.run.nodes : [];
  for (const [key] of STEPS) {
    const runs = nodes.filter((n) => n.step === key);
    const status = runs.length ? runs[runs.length - 1].status : "wait";
    const { cell, s } = pipeCells[key];
    const cls = "pipe-step is-" + status;
    if (cell.className !== cls) cell.className = cls;
    const [kind, label] = STATUS[status];
    const text = label + (runs.length > 1 ? ` ×${runs.length}` : "");
    if (s.dataset.text !== text) { s.dataset.text = text; s.replaceChildren(stamp(kind, text)); }
  }
}

// ---------------------------------------------------------------- timeline
function buildTimeline() {
  const wrap = el("div", "tl-wrap rise");
  const head = el("div", "tl-head");
  const label = el("span", "kicker", "Agent timeline · starting");
  const state = el("span");
  state.append(stamp("stamp-run", "Live"));
  head.append(label, state);
  const ol = el("ol", "tl");
  wrap.append(head, ol);
  $("timeline").replaceChildren(wrap);
  app.run.tl = { label, state, ol };
}
function addNode(ev) {
  const node = { step: ev.step, title: ev.title, t0: ev.t, t1: null, status: "run", tools: 0 };
  const li = el("li", "tl-item is-run rise");
  const body = el("div", "tl-body scan");
  const row = el("div", "tl-row");
  const stampHold = el("span");
  stampHold.append(stamp("stamp-run", "Working"));
  const dur = el("span", "tl-dur", CODE_STEPS.has(ev.step) ? "code" : "0.0 s");
  row.append(el("span", "tl-title", ev.title), el("span", "tl-concept", CONCEPT[ev.step] || ""), stampHold, dur);
  const wait = el("div", "tl-wait", ev.step === "triage" ? "Claude is reading the complaint…"
    : ev.step === "critic" ? "The critic is checking the draft against the rules…"
    : ev.step === "resolution" ? "Claude is deciding what to look up…" : "Checking…");
  const rail = el("div", "tl-rail");
  rail.append(el("span", "tl-dot"));
  body.append(row, wait);
  li.append(el("div", "tl-time", "+" + clock(ev.t)), rail, body);
  app.run.tl.ol.append(li);
  Object.assign(node, { li, body, stampHold, dur, wait });
  app.run.nodes.push(node);
  const n = app.run.nodes.length;
  app.run.tl.label.textContent = `Agent timeline · ${n} step${n === 1 ? "" : "s"}`;
  renderPipe();
}
function addTool(ev) {
  const node = app.run.nodes[app.run.nodes.length - 1];
  if (!node) return;
  const line = el("div", "tl-tool rise");
  line.append(document.createTextNode("🔧 "), el("b", null, ev.name), document.createTextNode(`(“${ev.query}”) → `),
    el("span", "found", ev.found));
  node.body.insertBefore(line, node.wait);
  node.tools += 1;
  node.wait.textContent = "Claude is reading the results and writing the draft…";
}
function endNode(ev) {
  const node = app.run.nodes[app.run.nodes.length - 1];
  if (!node) return;
  closeNode(node, ev.status, ev.t, ev.summary, ev.items);
}
function closeNode(node, status, t1, summary, items) {
  node.status = status;
  node.t1 = t1;
  node.li.className = "tl-item is-" + status;
  node.body.classList.remove("scan");
  node.stampHold.replaceChildren(stamp(...STATUS[status]));
  if (!CODE_STEPS.has(node.step)) node.dur.textContent = `${(t1 - node.t0).toFixed(1)} s`;
  node.wait.remove();
  if (summary) node.body.append(el("div", "tl-sum rise", summary));
  if (items && items.length) {
    const ul = el("ul", "tl-items rise");
    for (const x of items) ul.append(el("li", null, x));
    node.body.append(ul);
  }
  renderPipe();
}
function failOpenNodes(message) {
  const now = elapsed();
  for (const n of app.run.nodes) if (n.status === "run") closeNode(n, "fail", now, message, []);
}

// ---------------------------------------------------------------- receipts
function line(key, value) {
  const d = el("div", "line");
  d.append(el("span", "k", key), el("span", "fill"), el("span", "v", value));
  return d;
}
function checked(parent, key, value, truth) {
  parent.append(line(key, value));
  if (truth == null) return;
  const note = el("div", "line-note");
  note.append(value === truth ? stamp("stamp-ok", "✓ matches the CFPB record") : stamp("stamp-fail", "✗ real: " + truth));
  parent.append(note);
}
function receipt(r, kind, truth) {
  const agent = kind === "agent";
  const box = el("div", "receipt print");
  const head = el("div", "receipt-head");
  const titles = el("div");
  titles.append(el("div", "kicker", agent ? "Multi-agent workflow" : "One prompt · no tools · no review"),
    el("div", "receipt-title", agent ? "Agent" : "Baseline"));
  head.append(titles, r.needs_human_review ? stamp("stamp-warn", "⚑ Human review", true) : stamp("stamp-ok", "✓ Ready to send", true));
  box.append(head);
  const realOutcome = truth && TRUE_TO_OUTCOME[truth.company_response];
  checked(box, "Product", r.product, truth && truth.product);
  checked(box, "Issue", r.issue, truth && truth.issue);
  checked(box, "Outcome", OUTCOME[r.predicted_outcome] || r.predicted_outcome, realOutcome ? OUTCOME[realOutcome] : null);
  if (agent) {
    box.append(line("Urgency", r.urgency), line("Risk flags", r.risk_flags.join(", ") || "none"));
  }
  box.append(line("Rules cited", r.regulations_cited.join(", ") || "none"));
  if (agent && r.review_reasons && r.review_reasons.length) {
    const ul = el("ul", "reasons");
    for (const x of r.review_reasons) ul.append(el("li", null, x));
    box.append(ul);
  }
  box.append(el("div", "rule"), el("div", "kicker", "Reply to the consumer"));
  const reply = el("div", "reply");
  reply.style.whiteSpace = "pre-wrap";
  reply.textContent = r.reply;
  box.append(reply, el("div", "rule"));
  const tokens = `${(r.input_tokens + r.output_tokens).toLocaleString()} tokens`;
  const foot = el("div", "receipt-foot");
  const parts = agent
    ? [`${r.seconds} s`, `${r.revisions} revision${r.revisions === 1 ? "" : "s"}`, `${r.llm_calls} Claude calls`, tokens]
    : [`${r.seconds} s`, "1 Claude call", tokens];
  for (const p of parts) foot.append(el("span", null, p));
  box.append(foot);
  if (agent && r.critic_suggestions && r.critic_suggestions.length) {
    const det = el("details", "details");
    det.append(el("summary", null, "Critic suggestions (not blocking)"));
    const ul = el("ul");
    for (const s of r.critic_suggestions) ul.append(el("li", null, s));
    det.append(ul);
    box.append(det);
  }
  return box;
}
function errorBox(msg) {
  const d = el("div", "placeholder rise");
  d.append(stamp("stamp-fail", "✗ Error"), document.createElement("br"), document.createElement("br"), document.createTextNode(msg));
  return d;
}

// ---------------------------------------------------------------- status line
function elapsed() { return app.run ? (performance.now() - app.run.startedAt) / 1000 : 0; }
function buildStatus() {
  const box = $("statusline");
  const st = el("span"), time = el("span", "mono", "0:00.0"), word = el("span", null, "elapsed"), base = el("span");
  st.append(stamp("stamp-run", "Running"));
  box.replaceChildren(st, time, word, base);
  app.run.status = { st, time, word, base };
}
function tick() {
  if (!app.run) return;
  const t = elapsed();
  app.run.status.time.textContent = clock(t);
  for (const n of app.run.nodes) {
    if (n.status === "run" && !CODE_STEPS.has(n.step)) n.dur.textContent = `${Math.max(0, t - n.t0).toFixed(1)} s`;
  }
}

// ---------------------------------------------------------------- the run
function handleEvent(type, data) {
  const run = app.run;
  switch (type) {
    case "hello": break;
    case "step":
      if (data.type === "start") addNode(data);
      else if (data.type === "tool") addTool(data);
      else if (data.type === "end") endNode(data);
      break;
    case "baseline":
      $("baseline").replaceChildren(receipt(data, "baseline", run.truth));
      run.status.base.textContent = `(baseline answered in ${data.seconds} s)`;
      break;
    case "baseline_error":
      run.hadError = true;
      $("baseline").replaceChildren(errorBox(data.message));
      break;
    case "agent":
      $("agent").replaceChildren(receipt(data, "agent", run.truth));
      run.tl.state.replaceChildren(stamp("stamp-ok", "Finished"));
      break;
    case "agent_error":
      run.hadError = true;
      failOpenNodes(data.message);
      $("agent").replaceChildren(errorBox(data.message));
      run.tl.state.replaceChildren(stamp("stamp-fail", "Failed"));
      break;
    case "done":
      run.gotDone = true;
      break;
  }
}
function parseFrame(frame) {
  let type = "message";
  const data = [];
  for (const raw of frame.split("\n")) {
    const lineText = raw.replace(/\r$/, "");
    if (!lineText || lineText.startsWith(":")) continue;          // keep-alive comment
    if (lineText.startsWith("event:")) type = lineText.slice(6).trim();
    else if (lineText.startsWith("data:")) data.push(lineText.slice(5).replace(/^ /, ""));
  }
  if (!data.length) return;
  handleEvent(type, JSON.parse(data.join("\n")));
}

async function submit(e) {
  e.preventDefault();
  if (app.running || !app.unlocked) return;
  const narrative = $("narrative").value.trim();
  if (!narrative) { showError("Enter the complaint text."); return; }
  if (narrative.length > app.cfg.max_chars) { showError(`The complaint is longer than ${app.cfg.max_chars} characters.`); return; }
  showError("");
  const ex = currentExample();
  app.run = { nodes: [], truth: ex ? ex.truth : null, startedAt: performance.now(), gotDone: false };
  app.running = true;
  refreshForm();
  buildPipe();
  buildStatus();
  buildTimeline();
  $("agent").replaceChildren();
  $("baseline").replaceChildren(el("div", "placeholder scan", "One Claude call, usually a few seconds…"));
  document.querySelector(".section-head").scrollIntoView({ behavior: "smooth", block: "start" });
  app.ticker = setInterval(tick, 100);

  let failed = null;
  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Passcode": app.passcode },
      body: JSON.stringify({ company: $("company").value, state: $("state").value, narrative }),
    });
    if (!res.ok) {
      failed = await errorText(res);
      if (res.status === 403) { storeSet(""); setLock("bad", "✗ Wrong passcode"); }
    } else {
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, i);
          buf = buf.slice(i + 2);
          parseFrame(frame);
        }
      }
      if (!app.run.gotDone) failed = "The connection closed before the run finished. Try again.";
    }
  } catch (err) {
    failed = "Lost the connection to the server. Check the internet and try again.";
  }
  finish(failed);
}

function finish(failed) {
  clearInterval(app.ticker);
  const run = app.run;
  tick();
  if (failed) {
    showError(failed);
    failOpenNodes(failed);
    if (!run.nodes.length) $("timeline").replaceChildren(errorBox(failed));
    else run.tl.state.replaceChildren(stamp("stamp-fail", "Stopped"));
    if (!$("baseline").querySelector(".receipt")) $("baseline").replaceChildren(errorBox(failed));
    run.status.st.replaceChildren(stamp("stamp-fail", "Stopped"));
  } else {
    run.status.st.replaceChildren(run.hadError ? stamp("stamp-warn", "Done, with an error") : stamp("stamp-ok", "Done"));
    run.status.word.textContent = "total";
  }
  app.running = false;
  refreshForm();
}

$("form").addEventListener("submit", submit);

// ---------------------------------------------------------------- start
(async function init() {
  buildPipe();
  try {
    const res = await fetch("/api/config");
    app.cfg = await res.json();
  } catch {
    showError("Could not reach the server. Reload the page.");
    return;
  }
  $("model").textContent = app.cfg.model;
  app.cfg.examples.forEach((ex, i) => {
    const o = el("option", null, ex.label);
    o.value = String(i);
    $("example").append(o);
  });
  buildPipe();
  if (!app.cfg.passcode_set) setLock("bad", "✗ No passcode set on the server");
  else if (!app.cfg.api_key_set) showError("ANTHROPIC_API_KEY is not set on the server.");
  const saved = storeGet();
  if (saved && app.cfg.passcode_set) { $("passcode").value = saved; await tryUnlock(saved, true); }
  refreshForm();
})();

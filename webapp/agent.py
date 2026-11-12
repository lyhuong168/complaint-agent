"""The complaint agent in plain Python: a copy of the n8n workflow for the Streamlit app.

Same steps, prompts, output formats, tools and rules as n8n/build_workflow.py
(all shared through shared/spec.py). Each function below names the n8n node it
copies, so the two can be checked side by side.

    Prepare case -> TRIAGE AGENT -> Validate labels
      -> RESOLUTION AGENT (tools: search_regulations, company_history)
      -> Check citations -> COMPLIANCE CRITIC -> Decide next
      -> revise (at most MAX_REVISIONS times) or finish: human review / ready to send

The n8n version remains the one that was evaluated (results/). Small differences
are possible because n8n drives Claude through LangChain, while this file calls
the Claude API directly.
"""
from __future__ import annotations

import json
import re
import time
from typing import Callable

import anthropic

from shared.spec import (
    BASELINE_SYSTEM, CRITIC_SCHEMA, CRITIC_SYSTEM, HISTORY_TOOL_DESCRIPTION, MAX_REVISIONS,
    MODEL, REG_TOOL_DESCRIPTION, RESOLUTION_SCHEMA, RESOLUTION_SYSTEM, REVIEW_FLAGS,
    TRIAGE_SCHEMA, TRIAGE_SYSTEM, BaselineResult, history, regulations, taxonomy,
)

MAX_TOKENS = 8000                   # same as the n8n Claude node
EFFORT = "medium"                   # same as the n8n Claude node (adaptive thinking)

# If Claude's safety checks decline a request, the API retries it on another
# model instead of failing. Only these models accept the option.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
                   "claude-fable-5-2", "claude-fable-5-1"}

Event = Callable[[dict], None]      # called with each progress event (see run_agent)
ToolSeen = Callable[[str, str, str], None]   # (tool name, query, result)


class AgentError(RuntimeError):
    """Something went wrong that the person using the app should see."""


# ----------------------------------------------------------------------------
# Claude calls
# ----------------------------------------------------------------------------
class Usage:
    def __init__(self):
        self.calls = self.input_tokens = self.output_tokens = 0

    def add(self, response):
        self.calls += 1
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens


def connection_message(e: Exception) -> str:
    """Say why a request never reached Claude, in words the person can act on."""
    cause = repr(getattr(e, "__cause__", None) or "")
    if "Illegal header value" in cause:
        return ("The Anthropic API key contains a space or line break, so it cannot be sent. "
                "Paste it again without extra characters.")
    if "Timeout" in type(e).__name__ or "Timeout" in cause:
        return "Claude took too long to answer. Try again."
    return "Could not reach the Claude API. Check the internet connection."


def _create(client: anthropic.Anthropic, **kwargs):
    """One Messages API request, with the refusal fallback where the model allows it."""
    try:
        if MODEL in FALLBACK_MODELS:
            return client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        return client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        raise AgentError("The Anthropic API key is missing or wrong.")
    except anthropic.RateLimitError:
        raise AgentError("Claude is rate limited right now. Wait a minute and try again.")
    except anthropic.APIStatusError as e:
        raise AgentError(f"Claude API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError as e:
        raise AgentError(connection_message(e))


def run_agent_step(client, usage: Usage, name: str, system: str, user: str, schema: dict,
                   tools: dict[str, Callable[[str], str]] | None = None,
                   max_iterations: int = 3, on_tool: ToolSeen | None = None) -> dict:
    """One agent: Claude with an output format and optional tools, looping over tool calls.

    Copies an n8n "AI Agent" node with a "Structured Output Parser" attached.
    max_iterations matches the node's setting.
    """
    tool_defs = [{
        "name": tool_name,
        "description": {"search_regulations": REG_TOOL_DESCRIPTION,
                        "company_history": HISTORY_TOOL_DESCRIPTION}[tool_name],
        "strict": True,
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                         "required": ["query"], "additionalProperties": False},
    } for tool_name in (tools or {})]
    messages = [{"role": "user", "content": user}]

    for _ in range(max_iterations + 1):
        response = _create(
            client, model=MODEL, max_tokens=MAX_TOKENS, system=system, messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
            **({"tools": tool_defs} if tool_defs else {}),
        )
        usage.add(response)

        if response.stop_reason == "refusal":
            raise AgentError(f"{name}: Claude declined to handle this complaint.")
        if response.stop_reason == "max_tokens":
            raise AgentError(f"{name}: the answer was cut off (max_tokens).")
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason == "tool_use":
            # Keep the whole reply (thinking included), then answer every tool call at once.
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                query = str(block.input.get("query", ""))
                fn = (tools or {}).get(block.name)
                if fn is None:
                    result = f"Unknown tool {block.name}"
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": result, "is_error": True})
                else:
                    result = fn(query)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": result})
                if on_tool:
                    on_tool(block.name, query, result)
            messages.append({"role": "user", "content": results})
            continue

        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise AgentError(f"{name}: Claude returned no answer.")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise AgentError(f"{name}: Claude's answer was not valid JSON.")

    raise AgentError(f"{name}: still calling tools after {max_iterations} rounds.")


# ----------------------------------------------------------------------------
# Tools (copies of REG_TOOL_JS and HISTORY_TOOL_JS in n8n/build_workflow.py)
# ----------------------------------------------------------------------------
STOP_WORDS = {"the", "and", "for", "with", "that", "this", "from", "have", "has", "was", "were",
              "not", "are", "but", "they", "their", "consumer", "company", "complaint", "about",
              "account"}


def search_regulations(query: str) -> str:
    """Keyword search over the regulation knowledge base (n8n/knowledge/regulations.json)."""
    q = query.lower()
    words = list(dict.fromkeys(w for w in re.split(r"[^a-z0-9]+", q)
                               if len(w) > 3 and w not in STOP_WORDS))
    scored = []
    for r in regulations:
        score = 0
        if r["id"].lower() in words:          # the agent asked for it by id, e.g. "fcra"
            score += 5
        score += 3 * sum(k in q for k in r["keywords"])
        score += 2 * sum(p.lower() in q for p in r["products"])
        text = (r["name"] + " " + " ".join(r["rules"])).lower()
        score += sum(w in text for w in words)
        if score >= 2:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])          # stable, like JavaScript's sort
    if not scored:
        return "No matching regulation in the knowledge base. Do not cite a regulation for this point."
    return "\n\n".join(_regulation_text(r) for _, r in scored[:3])


def _regulation_text(r: dict) -> str:
    return f"[{r['id']}] {r['name']} ({r['citation']})\n" + "\n".join(f"- {x}" for x in r["rules"])


def _num(x) -> str:
    """Print a number the way JavaScript does (5.0 -> "5")."""
    x = float(x)
    return str(int(x)) if x.is_integer() else repr(x)


def _norm_company(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", str(s or "").upper())).strip()


def company_history(query: str) -> str:
    """How a company closed its complaints before Sept 2023 (n8n/knowledge/company_history.json)."""
    q = _norm_company(query)
    if not q:
        return "Give the company name."
    name = next((k for k in history if _norm_company(k) == q), None)
    if name is None:
        hits = [k for k in history if _norm_company(k) in q or q in _norm_company(k)]
        hits.sort(key=lambda k: -history[k]["past_complaints"])
        name = hits[0] if hits else None
    if name is None:
        return (f'No complaint history on file for "{query}". Use the general pattern: about 76% of '
                "complaints close with an explanation only, 19% with non-monetary relief and 4% "
                "with monetary relief.")
    h = history[name]
    money, non_money = h["pct_monetary_relief"], h["pct_non_monetary_relief"]
    explain = f"{max(0, 100 - money - non_money):.1f}"
    return (f"{name}: {_num(h['past_complaints'])} past complaints. Closed with explanation only: "
            f"{explain}%. Non-monetary relief: {_num(non_money)}%. Monetary relief: {_num(money)}%. "
            f"Late responses: {_num(h['pct_late_response'])}%.")


TOOLS = {"search_regulations": search_regulations, "company_history": company_history}


# ----------------------------------------------------------------------------
# The workflow
# ----------------------------------------------------------------------------
def _pretty(value) -> str:
    """JSON.stringify(value, null, 1), as the n8n prompts use."""
    return json.dumps(value, indent=1, ensure_ascii=False)


def tool_summary(name: str, result: str) -> str:
    """One line saying what a tool call found, for the timeline."""
    if name == "search_regulations":
        ids = re.findall(r"^\[([^\]]+)\]", result, re.M)
        return "found " + ", ".join(ids) if ids else "no matching regulation"
    return result if len(result) <= 200 else result[:197] + "..."


OUTCOME_WORDS = {"explanation_only": "explanation only",
                 "non_monetary_relief": "non-monetary relief",
                 "monetary_relief": "monetary relief"}


def run_agent(client: anthropic.Anthropic, company: str, state: str, narrative: str,
              on_event: Event = lambda _: None) -> dict:
    """Handle one complaint. Returns the same fields as the n8n agent's response.

    on_event receives, in order, for each step of the workflow:
      {"type": "start", "step": ..., "title": ...}
      {"type": "tool", "name": ..., "query": ..., "found": ...}    (resolution step only)
      {"type": "end", "step": ..., "status": "ok" | "warn" | "fail",
       "summary": ..., "items": [...]}
    step is one of: triage, labels, resolution, citations, critic, routing.
    """
    def start(step, title):
        on_event({"type": "start", "step": step, "title": title})

    def end(step, status, summary, items=()):
        on_event({"type": "end", "step": step, "status": status, "summary": summary,
                  "items": list(items)})

    def tool_seen(name, query, result):
        on_event({"type": "tool", "name": name, "query": query,
                  "found": tool_summary(name, result)})

    started = time.time()
    usage = Usage()

    # Prepare case: accept the request only if it has complaint text.
    narrative = str(narrative or "").strip()
    if not narrative:
        raise AgentError("Enter the complaint text.")
    company, state = str(company or "Unknown"), str(state or "Unknown")

    # Triage agent.
    start("triage", "Triage agent")
    triage = run_agent_step(
        client, usage, "Triage agent", TRIAGE_SYSTEM,
        f"Company: {company}\nState: {state}\n\nComplaint:\n{narrative}", TRIAGE_SCHEMA)

    end("triage", "ok", f"{triage['product']} · {triage['issue']}",
        [f"Urgency: {triage['urgency']}",
         "Risk flags: " + (", ".join(triage["risk_flags"]) or "none")])

    # Validate labels: the issue must come from the official CFPB list for the product.
    start("labels", "Label check")
    allowed = taxonomy.get(triage["product"], [])
    label_valid = triage["issue"] in allowed
    fixed = False
    if not label_valid and allowed:
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()  # noqa: E731
        match = next((i for i in allowed if norm(i) == norm(triage["issue"])), None)
        if match:
            triage["issue"], label_valid, fixed = match, True, True
    trace = [f"triage: {triage['product']} / {triage['issue']} (urgency {triage['urgency']})"]
    if label_valid:
        end("labels", "ok", "Product and issue are on the official CFPB list"
            + (" (spelling corrected to match it)" if fixed else ""))
    else:
        end("labels", "warn", "The issue is not on the official CFPB list for this product, "
            "so the case will go to a person")

    revision, feedback, draft = 0, "", None
    by_id = {r["id"]: r for r in regulations}
    while True:
        # Resolution agent: research with the tools, predict the outcome, draft the reply.
        start("resolution", f"Resolution agent · draft {revision + 1}")
        user = (f"Company: {company}\nState: {state}\n\nTriage result:\n{_pretty(triage)}\n\n"
                f"Complaint:\n{narrative}\n")
        if feedback:
            user += (f"\nYour previous draft:\n{draft['reply']}\n\n"
                     f"Feedback from the compliance reviewer (fix every point):\n{feedback}")
        draft = run_agent_step(client, usage, "Resolution agent", RESOLUTION_SYSTEM, user,
                               RESOLUTION_SCHEMA, tools=TOOLS, max_iterations=8,
                               on_tool=tool_seen)
        end("resolution", "ok", f"Predicts {OUTCOME_WORDS[draft['predicted_outcome']]}",
            ["Cites " + (", ".join(draft["regulations_cited"]) or "no regulation")]
            + (["Asks for a person: " + draft["escalation_reason"]] if draft["escalate"] else []))

        # Check citations: every cited regulation must be in the knowledge base.
        start("citations", "Citation check")
        cited = [re.sub(r"[\[\]]", "", str(c)).strip().upper() for c in draft["regulations_cited"]]
        draft["regulations_cited"] = cited
        unknown = [c for c in cited if c not in by_id]
        cited_text = "\n\n".join(_regulation_text(by_id[c]) for c in cited if c in by_id)
        trace.append(f"draft {revision + 1}: predicts {draft['predicted_outcome']}, cites "
                     f"{', '.join(cited) or 'nothing'}"
                     + (f" (NOT IN KNOWLEDGE BASE: {', '.join(unknown)})" if unknown else ""))
        if unknown:
            end("citations", "warn", "Not in the knowledge base: " + ", ".join(unknown))
        else:
            end("citations", "ok", "Every citation is in the knowledge base" if cited
                else "No regulation cited")

        # Compliance critic: a separate agent reviews the draft.
        start("critic", f"Compliance critic · draft {revision + 1}")
        critic = run_agent_step(
            client, usage, "Compliance critic", CRITIC_SYSTEM,
            f"Complaint:\n{narrative}\n\nTriage result:\n{_pretty(triage)}\n\n"
            f"Draft reply:\n{draft['reply']}\n\n"
            f"Regulations the draft says it relies on: {', '.join(cited) or 'none'}\n\n"
            f"Regulation extracts (the only rules the reply may rely on):\n{cited_text or 'none'}",
            CRITIC_SCHEMA)

        # Decide next. Only blocking problems count; a "revise" with none listed is an approval.
        problems = list(critic.get("problems") or [])[:3]
        if unknown:
            problems.append("Remove or replace these citations, which are not in the knowledge "
                            "base: " + ", ".join(unknown))
        approved = not problems
        trace.append(f"critic on draft {revision + 1}: " + (
            "approved" if approved else
            f"revise ({len(problems)} problem{'' if len(problems) == 1 else 's'})"))
        if approved:
            end("critic", "ok", "Approved")
        elif revision >= MAX_REVISIONS:
            end("critic", "fail", f"Still not approved after {MAX_REVISIONS} revisions", problems)
        else:
            end("critic", "warn", f"Sent back with {len(problems)} problem"
                f"{'' if len(problems) == 1 else 's'} to fix", problems)
        if approved or revision >= MAX_REVISIONS:
            break
        revision += 1
        feedback = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(problems))

    # Final decision. These rules are fixed code, not left to the model.
    reasons = []
    if not approved:
        reasons.append(f"Compliance critic did not approve the reply after {MAX_REVISIONS} revisions")
    if draft["escalate"]:
        reasons.append("Resolution agent escalated: " + draft["escalation_reason"])
    if triage["urgency"] == "high":
        reasons.append("High urgency")
    if draft["predicted_outcome"] == "monetary_relief":
        reasons.append("Money may be owed to the consumer")
    reasons += [f"Risk flag: {f}" for f in triage["risk_flags"] if f in REVIEW_FLAGS]
    if not label_valid:
        reasons.append("Triage issue label is not in the official list")
    needs_human = bool(reasons)
    start("routing", "Routing rules")
    trace.append("routed to human review" if needs_human else "ready to send")
    end("routing", "warn" if needs_human else "ok",
        "Sent to a person for review" if needs_human else "Ready to send", reasons)

    return {
        "status": "needs_human_review" if needs_human else "ready_to_send",
        "product": triage["product"], "issue": triage["issue"], "urgency": triage["urgency"],
        "risk_flags": triage["risk_flags"], "summary": triage["summary"],
        "consumer_request": triage["consumer_request"],
        "predicted_outcome": draft["predicted_outcome"],
        "outcome_reasoning": draft["outcome_reasoning"],
        "regulations_cited": draft["regulations_cited"],
        "needs_human_review": needs_human, "review_reasons": reasons, "reply": draft["reply"],
        "critic_approved": approved, "critic_problems": [] if approved else problems,
        "critic_suggestions": critic.get("suggestions") or [],
        "revisions": revision, "agent_runs": 1 + 2 * (revision + 1),
        "llm_calls": usage.calls, "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "seconds": round(time.time() - started, 1), "trace": trace,
    }


def run_baseline(client: anthropic.Anthropic, company: str, state: str, narrative: str) -> dict:
    """One Claude call, same request as eval/run_baseline.py: no tools, no critic, no rules."""
    started = time.time()
    narrative = str(narrative or "").strip()
    if not narrative:
        raise AgentError("Enter the complaint text.")
    try:
        response = client.messages.parse(
            model=MODEL, max_tokens=MAX_TOKENS, system=BASELINE_SYSTEM,
            messages=[{"role": "user", "content":
                       f"Company: {company or 'Unknown'}\nState: {state or 'Unknown'}\n\n"
                       f"Complaint:\n{narrative}"}],
            output_format=BaselineResult,
        )
    except anthropic.APIConnectionError as e:
        raise AgentError(connection_message(e))
    except anthropic.AuthenticationError:
        raise AgentError("The Anthropic API key is missing or wrong.")
    except anthropic.APIError as e:
        raise AgentError(f"Claude API error: {getattr(e, 'message', e)}")
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        raise AgentError(f"The baseline stopped early: {response.stop_reason}")
    return {
        **response.parsed_output.model_dump(),
        "llm_calls": 1, "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "seconds": round(time.time() - started, 1),
    }

"""Web demo with live updates: the same complaint goes to the baseline and to the agent.

Run locally:   uvicorn webapp.server:app --port 8010      (then open http://localhost:8010)
Hosted:        Render, see render.yaml and the README

The page (webapp/static/) sends a complaint to POST /api/run. The server runs the
agent and the baseline in two background threads and streams every step back to
the browser as Server-Sent Events, the moment it happens:

    hello  ->  step (start / tool / end, many)  ->  baseline  ->  agent  ->  done
    (baseline_error / agent_error instead of baseline / agent if a run fails)

Secrets come from the environment (Render) or from .env (locally, via shared/spec.py):
ANTHROPIC_API_KEY, DEMO_PASSCODE, and optionally CLAUDE_MODEL.
"""
from __future__ import annotations

import asyncio
import csv
import hmac
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

# Values pasted into a hosting dashboard often carry a trailing line break, spaces or
# quotes. A key with a line break cannot even be sent (the SDK reports it as a
# "Connection error"), so tidy them before anything reads them.
for _name in ("ANTHROPIC_API_KEY", "DEMO_PASSCODE", "CLAUDE_MODEL"):
    if _name in os.environ:
        os.environ[_name] = os.environ[_name].strip().strip("\"'").strip()

from shared.spec import EXAMPLE_IDS, MAX_REVISIONS, MODEL  # noqa: E402
from webapp.agent import AgentError, run_agent, run_baseline  # noqa: E402

log = logging.getLogger("complaint-agent")

MAX_PARALLEL_RUNS = int(os.getenv("MAX_PARALLEL_RUNS", "3"))   # protects API credit and memory
MAX_NARRATIVE_CHARS = 8000
KEEP_ALIVE_SECONDS = 10      # a comment line so proxies never close a quiet stream

app = FastAPI(title="Complaint Agent", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

_runs_free = threading.BoundedSemaphore(MAX_PARALLEL_RUNS)
_client: anthropic.Anthropic | None = None
_client_lock = threading.Lock()


def get_client() -> anthropic.Anthropic:
    global _client
    with _client_lock:
        if _client is None:
            _client = anthropic.Anthropic()
        return _client


def load_examples() -> list[dict]:
    """Real complaints from the test sample, with their real CFPB labels."""
    with open(ROOT / "data" / "complaints_sample.csv", encoding="utf-8") as f:
        by_id = {r["complaint_id"]: r for r in csv.DictReader(f)}
    return [{"label": label, "company": by_id[cid]["company"], "state": by_id[cid]["state"],
             "narrative": by_id[cid]["narrative"],
             "truth": {k: by_id[cid][k] for k in ("product", "issue", "company_response")}}
            for label, cid in EXAMPLE_IDS]


EXAMPLES = load_examples()


def check_passcode(request: Request) -> None:
    expected = os.getenv("DEMO_PASSCODE", "")
    if not expected:
        raise HTTPException(503, "DEMO_PASSCODE is not set on the server, so the demo is locked.")
    given = request.headers.get("x-passcode", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(403, "Wrong passcode.")


# ----------------------------------------------------------------------------
# Pages and small endpoints
# ----------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/healthz", include_in_schema=False)
def health():
    return {"ok": True}


@app.get("/api/config")
def config():
    return {"model": MODEL, "max_revisions": MAX_REVISIONS, "examples": EXAMPLES,
            "max_chars": MAX_NARRATIVE_CHARS,
            "passcode_set": bool(os.getenv("DEMO_PASSCODE")),
            "api_key_set": bool(os.getenv("ANTHROPIC_API_KEY"))}


@app.post("/api/unlock", status_code=204)
def unlock(request: Request):
    check_passcode(request)


# ----------------------------------------------------------------------------
# The live run
# ----------------------------------------------------------------------------
class Complaint(BaseModel):
    company: str = Field("", max_length=200)
    state: str = Field("", max_length=40)
    narrative: str = Field(..., max_length=MAX_NARRATIVE_CHARS)


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/run")
async def run(complaint: Complaint, request: Request):
    check_passcode(request)
    narrative = complaint.narrative.strip()
    if not narrative:
        raise HTTPException(422, "Enter the complaint text.")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY is not set on the server.")
    if not _runs_free.acquire(blocking=False):
        raise HTTPException(429, "Other people are running the demo right now. "
                                 "Try again in a minute.")

    args = (complaint.company.strip() or "Unknown", complaint.state.strip() or "Unknown", narrative)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    started = time.time()
    finished = {"n": 0}
    finished_lock = threading.Lock()

    def emit(kind: str, data) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, data))

    def thread_done(which: str) -> None:
        emit("_done", which)
        with finished_lock:          # the run slot is freed when both threads end,
            finished["n"] += 1       # even if the browser left early
            if finished["n"] == 2:
                _runs_free.release()

    def agent_thread():
        try:
            result = run_agent(get_client(), *args, on_event=lambda ev: emit(
                "step", {**ev, "t": round(time.time() - started, 2)}))
            emit("agent", result)
        except AgentError as ex:
            emit("agent_error", {"message": str(ex)})
        except Exception:
            log.exception("agent run failed")
            emit("agent_error", {"message": "Unexpected error in the agent. Check the server log."})
        finally:
            thread_done("agent")

    def baseline_thread():
        try:
            emit("baseline", run_baseline(get_client(), *args))
        except AgentError as ex:
            emit("baseline_error", {"message": str(ex)})
        except Exception:
            log.exception("baseline run failed")
            emit("baseline_error", {"message": "Unexpected error in the baseline. "
                                               "Check the server log."})
        finally:
            thread_done("baseline")

    async def stream():
        yield sse("hello", {"model": MODEL})
        done = 0
        while done < 2:
            try:
                kind, data = await asyncio.wait_for(queue.get(), timeout=KEEP_ALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if kind == "_done":
                done += 1
                continue
            yield sse(kind, data)
        yield sse("done", {"seconds": round(time.time() - started, 1)})

    try:
        threading.Thread(target=agent_thread, daemon=True).start()
        threading.Thread(target=baseline_thread, daemon=True).start()
    except Exception:
        _runs_free.release()
        raise
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

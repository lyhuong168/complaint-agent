# Complaint Triage and Resolution Agent

An LLM agent system that handles consumer finance complaints: it classifies each
complaint, researches the rules that apply, predicts the outcome, drafts a reply,
has a second agent check the reply, and sends risky cases to a person.

The agent is built in **n8n** (low-code). The evaluation is in **Python**.
Test data: 527 real complaints from the CFPB Consumer Complaint Database.

## How the agent works

```
Webhook -> Triage Agent -> label check -> Resolution Agent -> citation check
        -> Compliance Critic -> revise (max 2 times) or finish
        -> human review queue / ready to send -> response
```

| Step | What it does | Agentic concept |
|---|---|---|
| Triage Agent | Picks product, issue, urgency and risk flags | Specialised agent, structured output |
| Label check | Code that rejects labels not in the official CFPB lists | Guardrail |
| Resolution Agent | Calls `search_regulations` and `company_history`, predicts the outcome, drafts the reply | Tool use, retrieval |
| Citation check | Code that flags any cited regulation not in the knowledge base | Guardrail against invented citations |
| Compliance Critic | A separate agent reviews the draft and can send it back | Reflection loop, multi-agent |
| Routing | Fixed rules send urgent, risky or money-related cases to a person | Human in the loop |

## Folders

| Path | Contents |
|---|---|
| `data/complaints_sample.csv` | The 527 test complaints with their real labels |
| `n8n/build_workflow.py` | The agent's prompts, tools and wiring. Generates the workflow file |
| `n8n/complaint_agent.workflow.json` | The generated workflow that n8n imports |
| `n8n/knowledge/` | Regulation summaries, CFPB product/issue lists, company complaint history |
| `eval/` | Scripts that build the data, run both systems and score them |
| `results/` | Created by the evaluation scripts |
| `.tools/node24/` | A private copy of Node 24, used only to run n8n |

## Install (after downloading from GitHub)

Needs Windows and Python 3.12 or newer. In a PowerShell terminal in this folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

This creates the Python environment, downloads a private copy of Node 24, and
installs n8n. It takes about 10 minutes. Nothing is installed outside this folder.

## First-time setup

1. Open the file named `.env` in this folder (the install step creates it) and
   paste your Anthropic API key. Never commit this file; `.gitignore` excludes it.

   ```
   ANTHROPIC_API_KEY=your-key
   ```

2. In a PowerShell terminal in this folder, load the agent into n8n:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\n8n\setup_agent.ps1
   ```

3. Start n8n and leave this window open:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\n8n\start_n8n.ps1
   ```

4. Open http://localhost:5678. The first time, n8n asks you to create a local
   owner account (it stays on this computer). The workflow is called
   "Complaint Triage and Resolution Agent".

## Try one complaint

With n8n running, in a second PowerShell terminal:

```powershell
$body = @{ company = "WELLS FARGO & COMPANY"; state = "CA"
           narrative = "Someone used my debit card for charges I did not make and the bank refuses to refund me." } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:5678/webhook/complaint -ContentType "application/json" -Body $body
```

Each run appears under **Executions** in n8n, where you can click through every
step and see what each agent and tool did.

## Run the evaluation

Start small. Every run costs API money; rerunning a script resumes where it stopped.

```powershell
.\.venv\Scripts\Activate.ps1
python eval\run_baseline.py --limit 22    # one prompt, no tools
python eval\run_agent.py --limit 22       # the n8n agent (n8n must be running)
python eval\score.py                      # accuracy against the real labels
python eval\judge_replies.py --limit 22   # reply quality, graded by an LLM judge
```

Drop `--limit` to run all 527 complaints.

## Changing the agent

Edit `n8n/build_workflow.py`, then:

```powershell
python n8n\build_workflow.py
# stop n8n (Ctrl+C), then:
powershell -ExecutionPolicy Bypass -File .\n8n\setup_agent.ps1
powershell -ExecutionPolicy Bypass -File .\n8n\start_n8n.ps1
```

Changes made on the n8n canvas are overwritten by the next import, so copy
anything you want to keep back into `build_workflow.py`.

The model is set in two places that must match: `MODEL` in
`n8n/build_workflow.py` and `CLAUDE_MODEL` in `.env` (default `claude-opus-5-5`).

## Rebuilding the data

```powershell
python eval\build_sample.py       # downloads a 296 MB archive, draws the sample
python eval\build_reference.py    # product/issue lists and company history
```

## Known limits

- The CFPB no longer publishes complaint text, so the sample comes from a
  public-domain archive of the same database. Its newest complaints are from January 2024.
- The regulation summaries in `n8n/knowledge/regulations.json` were drafted by an
  AI and have not been checked by a lawyer. Verify them before quoting.
- `search_regulations` is a keyword search over 13 summaries, not a vector search.
- 2% of test complaints carry a product/issue pair too rare to be in the label
  list, so issue accuracy cannot reach 100%.
- Company history is drawn from one third of the archive, before September 2023.

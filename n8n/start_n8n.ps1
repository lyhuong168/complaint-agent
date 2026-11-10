# Start n8n for this project.  Run from anywhere:  .\n8n\start_n8n.ps1
# Then open http://localhost:5678 in a browser. Press Ctrl+C here to stop n8n.

$root = Split-Path -Parent $PSScriptRoot

# Use the project's own Node 24 (n8n does not install on the system's Node 26).
$env:Path = "$root\.tools\node24;$env:Path"

# Keep n8n's database and settings inside the project folder.
$env:N8N_USER_FOLDER = "$root\n8n\data"

# Local-only settings: no telemetry, no update or sign-up prompts.
$env:N8N_DIAGNOSTICS_ENABLED = "false"
$env:N8N_VERSION_NOTIFICATIONS_ENABLED = "false"
$env:N8N_PERSONALIZATION_ENABLED = "false"

& "$root\node_modules\.bin\n8n.cmd" @args

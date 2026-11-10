# Load the agent into n8n: store the API key, import the workflow, publish it.
# Run this while n8n is STOPPED, then start n8n with .\n8n\start_n8n.ps1
#
#   .\n8n\setup_agent.ps1
#
# Run it again whenever you change the API key or rebuild the workflow file.
param([string]$EnvFile)

$root = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = "$root\.env" }
$env:Path = "$root\.tools\node24;$env:Path"
$env:N8N_USER_FOLDER = "$root\n8n\data"
$env:N8N_DIAGNOSTICS_ENABLED = "false"
$n8n = "$root\node_modules\.bin\n8n.cmd"

if (Get-NetTCPConnection -LocalPort 5678 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "n8n is running. Stop it first (Ctrl+C in its window), then run this again." -ForegroundColor Red
    exit 1
}

# 1. Read the API key from .env
if (-not (Test-Path $EnvFile)) {
    Write-Host "No .env file at $EnvFile. Create it with one line: ANTHROPIC_API_KEY=your-key" -ForegroundColor Red
    exit 1
}
$line = Get-Content $EnvFile | Where-Object { $_ -match '^\s*ANTHROPIC_API_KEY\s*=' } | Select-Object -First 1
$key = if ($line) { ($line -split '=', 2)[1].Trim().Trim('"').Trim("'") } else { "" }
if (-not $key) {
    Write-Host "ANTHROPIC_API_KEY is missing or empty in $EnvFile" -ForegroundColor Red
    exit 1
}

# 2. Store the key as an n8n credential. n8n encrypts it in its own database;
#    the temporary file is deleted straight away.
$tmp = Join-Path $env:TEMP "n8n-cred-$PID.json"
$cred = @(@{ id = "AnthropicCred001"; name = "Anthropic account"; type = "anthropicApi"; data = @{ apiKey = $key } })
try {
    [IO.File]::WriteAllText($tmp, (ConvertTo-Json -InputObject $cred -Depth 5))
    & $n8n import:credentials --input=$tmp | Select-String -Pattern "import|error" | Write-Host
    if ($LASTEXITCODE -ne 0) { Write-Host "Credential import failed." -ForegroundColor Red; exit 1 }
} finally {
    Remove-Item $tmp -ErrorAction SilentlyContinue
}

# 3. Import the workflow and publish it so its webhook URL is live
& $n8n import:workflow --input="$root\n8n\complaint_agent.workflow.json" | Select-String -Pattern "import|error" | Write-Host
if ($LASTEXITCODE -ne 0) { Write-Host "Workflow import failed." -ForegroundColor Red; exit 1 }
& $n8n publish:workflow --id=ComplaintAgent01 | Select-String -Pattern "publish|cannot|fail|error" | Write-Host
if ($LASTEXITCODE -ne 0) { Write-Host "Publish failed." -ForegroundColor Red; exit 1 }

Write-Host "`nDone. Start n8n with .\n8n\start_n8n.ps1" -ForegroundColor Green
Write-Host "The agent will answer at http://localhost:5678/webhook/complaint"

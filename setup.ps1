# One-time install after downloading this project from GitHub (Windows).
# Needs Python 3.12 or newer already installed. Takes about 10 minutes.
#
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# Safe to run again: steps that are already done are skipped.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$nodeVersion = "v24.22.2"
$nodeSha256 = "ca1bff5d8ead13c9f2ea404a176db46bd430704d5b5113734049fa365e7799e2"

# 1. Python environment for the evaluation scripts
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    Write-Host "Creating the Python environment..."
    python -m venv "$root\.venv"
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv. Is Python installed and on PATH?" }
}
& "$root\.venv\Scripts\python.exe" -m pip install -q -r "$root\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }
Write-Host "Python packages: ok"

# 2. A private copy of Node 24 for n8n (n8n does not install on Node 26)
if (-not (Test-Path "$root\.tools\node24\node.exe")) {
    Write-Host "Downloading Node $nodeVersion..."
    $name = "node-$nodeVersion-win-x64"
    $zip = "$root\.tools\$name.zip"
    New-Item -ItemType Directory -Force "$root\.tools" | Out-Null
    Invoke-WebRequest "https://nodejs.org/dist/$nodeVersion/$name.zip" -OutFile $zip -UseBasicParsing
    if ((Get-FileHash $zip -Algorithm SHA256).Hash -ne $nodeSha256) {
        Remove-Item $zip
        throw "The Node download does not match its expected checksum."
    }
    Expand-Archive $zip -DestinationPath "$root\.tools"
    Rename-Item "$root\.tools\$name" "node24"
    Remove-Item $zip
}
Write-Host "Node 24: ok"

# 3. n8n itself (the slow step)
if (-not (Test-Path "$root\node_modules\.bin\n8n.cmd")) {
    Write-Host "Installing n8n (about 5 minutes, little output)..."
    $env:Path = "$root\.tools\node24;$env:Path"
    Push-Location $root
    try { & "$root\.tools\node24\npm.cmd" ci --no-fund --no-audit --loglevel=error } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw "n8n install failed." }
}
Write-Host "n8n: ok"

# 4. The API key file
if (-not (Test-Path "$root\.env")) {
    Copy-Item "$root\.env.example" "$root\.env"
    Write-Host "`nCreated .env. Open it and paste your Anthropic API key after ANTHROPIC_API_KEY=" -ForegroundColor Yellow
}
Write-Host "`nInstall finished. Next: add your key to .env, then follow 'First-time setup' in README.md" -ForegroundColor Green

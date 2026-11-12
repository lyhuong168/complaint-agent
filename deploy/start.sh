#!/bin/sh
# Start the hosted copy of the app (Render). Runs inside the n8n Docker image.
#
# Render's free plan does not keep files between restarts, so every boot starts
# with an empty n8n database. This script fills it again:
#   1. the owner account (from N8N_OWNER_EMAIL / N8N_OWNER_PASSWORD), so nobody
#      else can claim the editor
#   2. the Anthropic key and the demo passcode, as n8n credentials
#   3. both workflows, published so their web addresses are live
# and then starts n8n.
set -eu

for v in ANTHROPIC_API_KEY DEMO_PASSCODE N8N_OWNER_EMAIL N8N_OWNER_PASSWORD N8N_ENCRYPTION_KEY; do
  eval "val=\${$v:-}"
  if [ -z "$val" ]; then echo "Set $v in the Render dashboard (Environment)." >&2; exit 1; fi
done

DIR=/home/node/deploy

# Web addresses n8n shows and uses: the public https address Render gives us.
if [ -n "${RENDER_EXTERNAL_URL:-}" ]; then export WEBHOOK_URL="$RENDER_EXTERNAL_URL/"; fi

# 1. Owner account, applied by n8n itself at start.
N8N_PKG=$(dirname "$(dirname "$(readlink -f "$(command -v n8n)")")")
export N8N_INSTANCE_OWNER_MANAGED_BY_ENV=true
export N8N_INSTANCE_OWNER_EMAIL="$N8N_OWNER_EMAIL"
export N8N_INSTANCE_OWNER_PASSWORD_HASH="$(N8N_PKG="$N8N_PKG" node "$DIR/prepare.cjs" hash)"
unset N8N_OWNER_PASSWORD

# 2. Credentials. n8n encrypts them with N8N_ENCRYPTION_KEY; the file is deleted at once.
tmp=$(mktemp)
node "$DIR/prepare.cjs" credentials > "$tmp"
n8n import:credentials --input="$tmp"
rm -f "$tmp"

# 3. Workflows.
n8n import:workflow --input="$DIR/complaint_agent.workflow.json"
n8n publish:workflow --id=ComplaintAgent01
n8n import:workflow --input="$DIR/demo_ui.workflow.json"
n8n publish:workflow --id=ComplaintDemoUI01

exec n8n start

#!/usr/bin/env bash
# Pulls secrets from Bitwarden Secrets Manager into environment.
# Run as init container; writes to /run/secrets/env so main containers source it.
set -euo pipefail

SECRETS_OUT="${SECRETS_OUT:-/run/secrets/env}"

if [ -z "${BWS_ACCESS_TOKEN:-}" ]; then
  echo "ERROR: BWS_ACCESS_TOKEN not set" >&2
  exit 1
fi

if ! command -v bws &>/dev/null; then
  echo "ERROR: bws CLI not found" >&2
  exit 1
fi

echo "Fetching secrets from Bitwarden Secrets Manager..."

bws secret list --output json | \
  python3 -c "
import json, sys
secrets = json.load(sys.stdin)
for s in secrets:
    key = s['key'].upper().replace('-', '_')
    val = s['value'].replace('\n', ' ').replace(\"'\", \"'\\\"'\\\"'\")
    print(f\"export {key}='{val}'\")
" > "${SECRETS_OUT}"

echo "Secrets written to ${SECRETS_OUT}"
echo "Source with: source ${SECRETS_OUT}"

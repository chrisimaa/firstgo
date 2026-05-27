#!/usr/bin/env bash
# Load .env and run the reconciliation using the local venv.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "No .env found. Copy .env.example to .env and fill in credentials." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
# Fall back to the JSON file if the env var wasn't provided inline.
if [ -z "${GOOGLE_SERVICE_ACCOUNT_JSON:-}" ] && [ -f google-service-account.json ]; then
  GOOGLE_SERVICE_ACCOUNT_JSON="$(cat google-service-account.json)"
fi
set +a

exec ./.venv/bin/python scripts/recon.py "$@"

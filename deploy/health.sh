#!/usr/bin/env bash
set -euo pipefail
url="${HEALTH_URL:-http://127.0.0.1:8000/health/ready}"
attempts="${HEALTH_ATTEMPTS:-30}"
for ((i=1; i<=attempts; i++)); do
  curl --fail --silent --show-error "$url" >/dev/null && exit 0
  sleep 2
done
echo "health check failed: $url" >&2
exit 1

#!/usr/bin/env bash
set -euo pipefail
: "${ROLLBACK_IMAGE_REF:?ROLLBACK_IMAGE_REF must be an immutable image digest}"
: "${ROLLBACK_WEB_IMAGE_REF:?ROLLBACK_WEB_IMAGE_REF must be an immutable image digest}"
[[ "$ROLLBACK_IMAGE_REF" == *@sha256:* ]] || { echo "digest required" >&2; exit 2; }
[[ "$ROLLBACK_WEB_IMAGE_REF" == *@sha256:* ]] || { echo "web digest required" >&2; exit 2; }
exec 9>/tmp/easy-content-creator-deploy.lock
flock -n 9 || { echo "another deployment is running" >&2; exit 1; }
export ECC_IMAGE="$ROLLBACK_IMAGE_REF"
export ECC_WEB_IMAGE="$ROLLBACK_WEB_IMAGE_REF"
docker compose pull api worker
docker compose up -d --remove-orphans
./deploy/health.sh
printf 'app=%s\nweb=%s\n' "$ROLLBACK_IMAGE_REF" "$ROLLBACK_WEB_IMAGE_REF" > .deployed-image

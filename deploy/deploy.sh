#!/usr/bin/env bash
set -euo pipefail
: "${IMAGE_REF:?IMAGE_REF must be an immutable image digest}"
: "${WEB_IMAGE_REF:?WEB_IMAGE_REF must be an immutable image digest}"
[[ "$IMAGE_REF" == *@sha256:* ]] || { echo "IMAGE_REF must contain @sha256:" >&2; exit 2; }
[[ "$WEB_IMAGE_REF" == *@sha256:* ]] || { echo "WEB_IMAGE_REF must contain @sha256:" >&2; exit 2; }
: "${BACKUP_DIR:?BACKUP_DIR is required}"
exec 9>/tmp/easy-content-creator-deploy.lock
flock -n 9 || { echo "another deployment is running" >&2; exit 1; }
export ECC_IMAGE="$IMAGE_REF"
export ECC_WEB_IMAGE="$WEB_IMAGE_REF"
./deploy/backup.sh
docker compose pull api worker
docker compose run --rm api python -m alembic upgrade head
docker compose up -d --remove-orphans
./deploy/health.sh
printf 'app=%s\nweb=%s\n' "$IMAGE_REF" "$WEB_IMAGE_REF" > .deployed-image

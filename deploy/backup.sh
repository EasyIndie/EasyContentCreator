#!/usr/bin/env bash
set -euo pipefail
: "${BACKUP_DIR:?BACKUP_DIR is required}"
mkdir -p "$BACKUP_DIR"
file="$BACKUP_DIR/ecc-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-ecc}" -d "${POSTGRES_DB:-ecc}" -Fc > "$file"
test -s "$file"
echo "$file"

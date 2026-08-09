#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"

assert_rejected() {
  local output
  if output="$("$@" 2>&1)"; then
    echo "expected command to fail: $*" >&2
    exit 1
  fi
  grep -Eq 'digest|required|immutable' <<<"$output"
}

assert_rejected env IMAGE_REF=mutable WEB_IMAGE_REF=also-mutable BACKUP_DIR=/tmp ./deploy/deploy.sh
assert_rejected env ROLLBACK_IMAGE_REF=mutable ROLLBACK_WEB_IMAGE_REF=also-mutable ./deploy/rollback.sh

fake_bin="$(mktemp -d)"
trap 'rm -rf "$fake_bin"' EXIT
cat >"$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$fake_bin/curl"
PATH="$fake_bin:$PATH" HEALTH_ATTEMPTS=1 ./deploy/health.sh

echo "deploy guards and health probe passed"

#!/usr/bin/env bash
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"
./scripts/check-docs.sh

if [[ -f pyproject.toml ]]; then
  command -v ruff >/dev/null && ruff format --check .
  command -v ruff >/dev/null && ruff check .
  command -v mypy >/dev/null && mypy apps packages
  command -v pytest >/dev/null && pytest
fi

if [[ -f apps/web/package.json ]]; then
  npm --prefix apps/web run lint --if-present
  npm --prefix apps/web run typecheck --if-present
  npm --prefix apps/web test --if-present -- --run
  npm --prefix apps/web run build --if-present
fi

docker compose config --quiet
git diff --check

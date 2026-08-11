#!/usr/bin/env bash
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"
./scripts/check-docs.sh

if [[ -f pyproject.toml ]]; then
  source "$root/scripts/python-toolchain.sh"
  resolve_python_toolchain "$root"
  export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
  verify_repository_imports "$root"
  ./scripts/test-verify-fail-closed.sh
  "$ECC_VERIFY_PYTHON" -m ruff format --check .
  "$ECC_VERIFY_PYTHON" -m ruff check .
  "$ECC_VERIFY_PYTHON" -m mypy apps packages migrations
  "$ECC_VERIFY_PYTHON" -m pytest -m "not compose"
  "$ECC_VERIFY_PYTHON" -m pytest -m compose tests/e2e
fi

if [[ -f apps/web/package.json ]]; then
  npm --prefix apps/web run lint --if-present
  npm --prefix apps/web run typecheck --if-present
  npm --prefix apps/web test --if-present -- --run
  npm --prefix apps/web run build --if-present
fi

docker compose config --quiet
git diff --check

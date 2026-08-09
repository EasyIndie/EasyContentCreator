#!/usr/bin/env bash
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
source "$root/scripts/python-toolchain.sh"

test_root="$(mktemp -d)"
trap 'rm -rf "$test_root"' EXIT
mkdir -p "$test_root/.venv/bin"

if (resolve_python_toolchain "$test_root") >/dev/null 2>&1; then
  echo "incomplete virtual environment unexpectedly passed verification" >&2
  exit 1
fi

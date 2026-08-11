#!/usr/bin/env bash
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
source "$root/scripts/python-toolchain.sh"
resolve_python_toolchain "$root"
known_python="$ECC_VERIFY_PYTHON"

test_root="$(mktemp -d)"
foreign_root=""
trap 'rm -rf "$test_root"; [[ -z "$foreign_root" ]] || rm -rf "$foreign_root"' EXIT
mkdir -p "$test_root/.venv/bin"

if (resolve_python_toolchain "$test_root") >/dev/null 2>&1; then
  echo "incomplete virtual environment unexpectedly passed verification" >&2
  exit 1
fi

# A foreign console script must never select the interpreter or test a different worktree.
printf '#!/usr/bin/env bash\nexec %q "$@"\n' "$known_python" >"$test_root/.venv/bin/python"
chmod +x "$test_root/.venv/bin/python"
printf '#!/foreign/editable/python\nexit 0\n' >"$test_root/.venv/bin/pytest"
chmod +x "$test_root/.venv/bin/pytest"
resolve_python_toolchain "$test_root"
[[ "$ECC_VERIFY_PYTHON" == "$test_root/.venv/bin/python" ]]

foreign_root="$(mktemp -d)"
mkdir -p "$foreign_root/apps" "$test_root/migrations" "$test_root/packages"
touch "$foreign_root/apps/__init__.py" "$test_root/migrations/__init__.py" \
  "$test_root/packages/__init__.py"
if (PYTHONPATH="$foreign_root" verify_repository_imports "$test_root") >/dev/null 2>&1; then
  echo "foreign editable import unexpectedly passed verification" >&2
  exit 1
fi
mkdir -p "$test_root/apps"
touch "$test_root/apps/__init__.py"
PYTHONPATH="$foreign_root" verify_repository_imports "$test_root"

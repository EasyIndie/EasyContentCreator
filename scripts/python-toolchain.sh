#!/usr/bin/env bash

resolve_python_toolchain() {
  local root="$1"
  local python
  if [[ -d "$root/.venv" ]]; then
    python="$root/.venv/bin/python"
    [[ -x "$python" ]] || {
      echo "incomplete Python virtual environment: missing .venv/bin/python" >&2
      return 1
    }
  else
    python="$(command -v python || true)"
    [[ -n "$python" ]] || {
      echo "missing required Python interpreter (create .venv or install python on PATH)" >&2
      return 1
    }
  fi

  "$python" -c "import mypy, pytest, ruff" >/dev/null 2>&1 || {
    echo "incomplete Python toolchain: python cannot import ruff, mypy, and pytest" >&2
    return 1
  }
  ECC_VERIFY_PYTHON="$python"
}

verify_repository_imports() {
  local root="$1"
  (
    cd "$root"
    ECC_VERIFY_ROOT="$root" PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}" \
      "$ECC_VERIFY_PYTHON" -c '
import os
from pathlib import Path
import apps, migrations, packages

root = Path(os.environ["ECC_VERIFY_ROOT"]).resolve()
for module in (apps, migrations, packages):
    origin = Path(module.__file__).resolve()
    if not origin.is_relative_to(root):
        raise SystemExit(f"repository import escaped current worktree: {module.__name__}={origin}")
'
  )
}

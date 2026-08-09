#!/usr/bin/env bash

resolve_python_toolchain() {
  local root="$1"
  local tool
  if [[ -d "$root/.venv" ]]; then
    for tool in python ruff mypy pytest; do
      [[ -x "$root/.venv/bin/$tool" ]] || {
        echo "incomplete Python virtual environment: missing .venv/bin/$tool" >&2
        return 1
      }
    done
    ECC_VERIFY_PYTHON="$root/.venv/bin/python"
    ECC_VERIFY_RUFF="$root/.venv/bin/ruff"
    ECC_VERIFY_MYPY="$root/.venv/bin/mypy"
    ECC_VERIFY_PYTEST="$root/.venv/bin/pytest"
    return 0
  fi

  for tool in python ruff mypy pytest; do
    command -v "$tool" >/dev/null 2>&1 || {
      echo "missing required Python tool: $tool (create .venv or install it on PATH)" >&2
      return 1
    }
  done
  ECC_VERIFY_PYTHON="$(command -v python)"
  ECC_VERIFY_RUFF="$(command -v ruff)"
  ECC_VERIFY_MYPY="$(command -v mypy)"
  ECC_VERIFY_PYTEST="$(command -v pytest)"
}

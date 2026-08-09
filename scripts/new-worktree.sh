#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 2 ]] || { echo "usage: $0 ECC-NNN short-slug" >&2; exit 2; }
task_id="$1"; slug="$2"
[[ "$task_id" =~ ^ECC-[0-9]{3}$ ]] || { echo "invalid task id" >&2; exit 2; }
[[ "$slug" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || { echo "invalid slug" >&2; exit 2; }

root="$(git rev-parse --show-toplevel)"
parent="$(dirname "$root")"
branch="task/${task_id}-${slug}"
task_lower="$(printf '%s' "$task_id" | tr '[:upper:]' '[:lower:]')"
target="${parent}/$(basename "$root")-${task_lower}-${slug}"
git show-ref --verify --quiet "refs/heads/$branch" && { echo "branch exists: $branch" >&2; exit 1; }
[[ ! -e "$target" ]] || { echo "target exists: $target" >&2; exit 1; }
git worktree add -b "$branch" "$target" main
echo "$target"

#!/usr/bin/env bash
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"

if [[ "${1:-}" != "--structure-only" ]]; then
  required=(AGENTS.md docs/vision.md docs/architecture.md docs/roadmap.md docs/glossary.md)
  for file in "${required[@]}"; do
    [[ -s "$file" ]] || { echo "missing required document: $file" >&2; exit 1; }
  done
fi

status=0
duplicate_ids="$(find docs/tasks -type f -name 'ECC-[0-9][0-9][0-9].md' -exec basename {} .md \; | sort | uniq -d)"
if [[ -n "$duplicate_ids" ]]; then
  echo "duplicate task ids:" >&2
  echo "$duplicate_ids" >&2
  status=1
fi

while IFS= read -r file; do
  id="$(basename "$file" .md)"
  grep -qE '^id: ECC-[0-9]{3}$' "$file" || { echo "$file: invalid id" >&2; status=1; }
  grep -qE '^status: (backlog|ready|active|review|blocked|completed)$' "$file" || { echo "$file: invalid status" >&2; status=1; }
  grep -qE '^model: (low|medium|high)$' "$file" || { echo "$file: invalid model" >&2; status=1; }
  [[ "$id" == "TEMPLATE" ]] || grep -q "id: $id" "$file" || { echo "$file: filename/id mismatch" >&2; status=1; }
  case "$file" in
    docs/tasks/completed/*)
      grep -q '^status: completed$' "$file" || { echo "$file: directory/status mismatch" >&2; status=1; }
      [[ -s "docs/handoffs/$id.md" ]] || { echo "$file: missing handoff" >&2; status=1; }
      ;;
    docs/tasks/blocked/*)
      grep -q '^status: blocked$' "$file" || { echo "$file: directory/status mismatch" >&2; status=1; }
      [[ -s "docs/handoffs/$id.md" ]] || { echo "$file: missing handoff" >&2; status=1; }
      ;;
    docs/tasks/active/*)
      grep -qE '^status: (active|review)$' "$file" || { echo "$file: directory/status mismatch" >&2; status=1; }
      ;;
    docs/tasks/backlog/*)
      grep -qE '^status: (backlog|ready)$' "$file" || { echo "$file: directory/status mismatch" >&2; status=1; }
      ;;
  esac
done < <(find docs/tasks -type f -name 'ECC-[0-9][0-9][0-9].md' -print)

while IFS= read -r file; do
  grep -qE '^- 状态：(proposed|accepted|superseded|rejected)$' "$file" || { echo "$file: invalid ADR status" >&2; status=1; }
done < <(find docs/decisions -type f -name 'ADR-[0-9][0-9][0-9]-*.md' -print)

exit "$status"

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
while IFS= read -r file; do
  id="$(basename "$file" .md)"
  grep -qE '^id: ECC-[0-9]{3}$' "$file" || { echo "$file: invalid id" >&2; status=1; }
  grep -qE '^status: (backlog|ready|active|review|blocked|completed)$' "$file" || { echo "$file: invalid status" >&2; status=1; }
  grep -qE '^model: (low|medium|high)$' "$file" || { echo "$file: invalid model" >&2; status=1; }
  [[ "$id" == "TEMPLATE" ]] || grep -q "id: $id" "$file" || { echo "$file: filename/id mismatch" >&2; status=1; }
done < <(find docs/tasks -type f -name 'ECC-[0-9][0-9][0-9].md' -print)

while IFS= read -r file; do
  grep -qE '^- 状态：(proposed|accepted|superseded|rejected)$' "$file" || { echo "$file: invalid ADR status" >&2; status=1; }
done < <(find docs/decisions -type f -name 'ADR-[0-9][0-9][0-9]-*.md' -print)

exit "$status"

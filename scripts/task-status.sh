#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 2 ]] || { echo "usage: $0 ECC-NNN active|blocked|completed" >&2; exit 2; }
id="$1"; target="$2"
[[ "$target" =~ ^(active|blocked|completed)$ ]] || { echo "invalid target status" >&2; exit 2; }
root="$(git rev-parse --show-toplevel)"; cd "$root"
file="$(find docs/tasks -type f -name "$id.md" -print)"
[[ -n "$file" && "$(printf '%s\n' "$file" | wc -l | tr -d ' ')" == 1 ]] || { echo "task must exist exactly once" >&2; exit 1; }
dest="docs/tasks/$target/$id.md"
mkdir -p "$(dirname "$dest")"
sed "s/^status: .*/status: $target/" "$file" > "$dest.tmp"
mv "$dest.tmp" "$dest"
[[ "$file" == "$dest" ]] || git rm "$file" >/dev/null
echo "$dest"

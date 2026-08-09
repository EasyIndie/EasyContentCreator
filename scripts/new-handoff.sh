#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 1 && "$1" =~ ^ECC-[0-9]{3}$ ]] || { echo "usage: $0 ECC-NNN" >&2; exit 2; }
root="$(git rev-parse --show-toplevel)"; cd "$root"
dest="docs/handoffs/$1.md"
[[ ! -e "$dest" ]] || { echo "handoff exists: $dest" >&2; exit 1; }
sed "s/ECC-NNN/$1/g" docs/handoffs/TEMPLATE.md > "$dest"
echo "$dest"

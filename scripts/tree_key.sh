#!/usr/bin/env bash
# A hash of everything a check could read: HEAD plus the path and content of every
# tracked or untracked (non-ignored) file. Two runs with the same key are verifying the
# same tree, so the second one can be told the first already passed.
#
# If hashing fails for any reason this prints a unique key instead, so a broken key can
# only cost a re-run - it can never let a stale "green" stand for a changed tree.
set -uo pipefail
cd "$(dirname "$0")/.."

list=$(mktemp) || { echo "nokey-$$-$(date +%s)"; exit 0; }
trap 'rm -f "$list"' EXIT

git ls-files -z --cached --others --exclude-standard \
  | while IFS= read -r -d '' f; do [ -f "$f" ] && printf '%s\0' "$f"; done > "$list"

if [ ! -s "$list" ] || ! hashes=$(xargs -0 git hash-object -- < "$list"); then
  echo "nokey-$$-$(date +%s)"
  exit 0
fi

{
  git rev-parse HEAD 2>/dev/null || echo "no-head"
  printf '%s\n' "$hashes"
  tr '\0' '\n' < "$list"
} | shasum | cut -c1-16

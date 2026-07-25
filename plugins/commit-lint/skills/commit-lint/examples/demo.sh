#!/bin/sh
# Demonstrate commit-lint on the bundled fixtures and on real git history.
set -e
S="$(dirname "$0")/../scripts/commit_lint.py"

echo "### Linting each bad message"
while IFS= read -r msg; do
  [ -z "$msg" ] && continue
  printf '\n$ commit_lint.py lint -m %s\n' "\"$msg\""
  python3 "$S" lint -m "$msg" --fail-on high || true
done < "$(dirname "$0")/bad_messages.txt"

echo "\n### Linting the good messages (each should pass)"
while IFS= read -r msg; do
  [ -z "$msg" ] && continue
  python3 "$S" lint -m "$msg" >/dev/null && echo "ok   $msg"
done < "$(dirname "$0")/good_messages.txt"

echo "\n### Changelog from real git history"
python3 "$S" changelog --range HEAD --version 1.1.0 --date 2026-07-24

#!/bin/sh
# Git commit-msg hook: reject commits that don't follow Conventional Commits.
# Install:  cp this file to .git/hooks/commit-msg && chmod +x .git/hooks/commit-msg
exec python3 "$(git rev-parse --show-toplevel)/plugins/commit-lint/skills/commit-lint/scripts/commit_lint.py" \
  lint --file "$1" --fail-on high

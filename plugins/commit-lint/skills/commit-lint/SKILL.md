---
name: commit-lint
description: This skill should be used when the user asks to "check my commit messages", "lint commits", "is this a valid conventional commit", "generate a changelog", "write release notes from git history", "set up a commit-msg hook", or works with Conventional Commits / commitlint / semantic-release conventions. It validates commit messages against the Conventional Commits spec (header format, type, scope, imperative lowercase subject, body formatting, BREAKING CHANGE consistency) and generates a grouped Keep-a-Changelog-style release note from a git revision range.
version: 1.0.0
---

# commit-lint

Validate commit messages against Conventional Commits, and turn a revision range
into a grouped release note with breaking changes called out first.

## When to use this skill

- **Before committing** — check the message you're about to use.
- **Reviewing a PR** — lint every commit in `origin/main..HEAD`.
- **Cutting a release** — generate the changelog from `v1.2.0..HEAD`.
- **Setting up a repo** — install a `commit-msg` hook or add a CI gate.

Triggers include "lint my commits", "is this a valid conventional commit?",
"generate a changelog", "write release notes".

## Workflow

The script has two modes; both share one parser, so lint results and changelog
grouping never disagree.

### Lint

```bash
S="${CLAUDE_PLUGIN_ROOT}/skills/commit-lint/scripts/commit_lint.py"

python3 "$S" lint -m "feat(api): add cursor pagination"   # a single message
python3 "$S" lint --file .git/COMMIT_EDITMSG              # commit-msg hook
python3 "$S" lint --range origin/main..HEAD               # every commit in a PR
python3 "$S" lint --range HEAD --json                     # machine-readable
python3 "$S" lint -m "..." --types feat,fix,docs,deps     # custom type set
```

Flags: `--json`, `--fail-on {info,low,medium,high,critical}` (default `high` — so
only malformed headers fail, style nits stay advisory), `--selftest`. Exit code is
`1` when findings meet the threshold.

### Changelog

```bash
python3 "$S" changelog --range v1.2.0..HEAD --version 1.3.0 --date 2026-07-24
```

Output is Markdown ready to paste into `CHANGELOG.md` or a GitHub release:
breaking changes first, then Features / Bug Fixes / Performance / … , scopes
bolded, short shas appended. Non-conventional commits go under **Other** so
nothing silently vanishes; merge/revert/fixup commits are excluded.

## Interpreting findings

Each has a rule ID, severity, the offending header, why the convention exists,
and a concrete rewrite. Full catalog: `references/rules.md`. The format is:

```
type(scope)!: subject      # imperative, lowercase, no trailing period
                           # blank line
body explaining what and why
                           # blank line
BREAKING CHANGE: what broke and how to migrate
```

When fixing a message, rewrite it fully rather than describing the rule — the
user wants the corrected line.

## Setting up enforcement

- **Local hook** — `examples/commit-msg-hook.sh` is a ready-to-copy
  `.git/hooks/commit-msg` that rejects malformed messages before they land.
- **CI gate** — lint just the PR's commits so existing history isn't retroactively
  failed:
  ```yaml
  - run: python3 .../commit_lint.py lint --range origin/${{ github.base_ref }}..HEAD
  ```

## Caveats worth stating

- **Don't rewrite pushed history** to satisfy the linter. Apply it going forward;
  if the user asks about old commits, note that fixing them means a force-push
  and check whether that's acceptable first.
- A repo may have its own convention (different types, no scopes). Use `--types`
  and respect an existing `commitlint.config.js` / `.commitlintrc` if present
  rather than imposing the defaults.
- CL008 (imperative mood) matches a word list of common past-tense/gerund
  openers, so an unusual verb may pass. It won't guess at meaning.

## Output style for the user

For linting: show the corrected message, not a lecture. For changelogs: print the
Markdown and note which commits landed under "Other" (those are the ones whose
messages didn't follow the convention).

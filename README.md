# claude-skills

[![CI](https://github.com/aabhimittal/claude-skills/actions/workflows/ci.yml/badge.svg)](https://github.com/aabhimittal/claude-skills/actions/workflows/ci.yml)

An open-source [Claude Code](https://code.claude.com) plugin marketplace — eleven
focused, **dependency-free** developer skills. Each ships a deterministic Python
analyzer (or driver) with a self-test and a CI-friendly exit code, so the
guidance is enforced by code, not vibes.

📖 **[docs/USAGE.md](docs/USAGE.md)** — see each skill in action with real,
captured terminal output on the bundled examples (what you get after installing).

## Plugins

| Plugin | What it does |
| --- | --- |
| 🛡️ **migration-guard** | Flags database migrations that lock tables, rewrite tables, or lose data — and suggests online-safe rewrites. |
| 🩺 **llm-app-doctor** | Audits AI/LLM integration code for leaked keys, prompt-injection sinks, dead model IDs, and params that now error. |
| 🐳 **dockerfile-doctor** | Lints Dockerfiles for security (root, `:latest`, baked secrets, `curl\|sh`) and image bloat / cache problems. |
| 🔑 **env-doctor** | Reconciles `.env` ↔ code usage ↔ `.env.example`: un-ignored `.env`, missing/dead vars, placeholders, leaked secrets. |
| 🔍 **regression-finder** | Drives a safe automated `git bisect run` to pinpoint the commit that introduced a regression. |
| 🔐 **actions-guard** | Security-lints GitHub Actions workflows: unpinned actions, `pull_request_target` pwn requests, `run:` script injection, over-broad tokens. |
| 🔁 **api-contract-guard** | Diffs two API schema versions (OpenAPI JSON + GraphQL SDL) and flags breaking changes for clients. |
| 🐙 **compose-guard** | Lints Docker Compose for privileged containers, mounted `docker.sock`, host namespaces, baked secrets, exposed datastores. |
| ♿ **a11y-guard** | Accessibility linter for HTML/JSX: missing alt text, unnamed icon buttons, `div onClick`, unlabelled inputs, focus traps. |
| 📝 **commit-lint** | Conventional Commits linter **and** changelog generator from git history. |
| 🎲 **flaky-test-hunter** | Runs tests N times, ranks non-deterministic tests, and classifies the root cause. |

---

### 🛡️ migration-guard

**Catch dangerous database migrations before they take production down.** Adding
a column, an index, or a foreign key can lock a large table for minutes, rewrite
every row, or destroy data on a busy production database. migration-guard
statically analyzes migrations (PostgreSQL SQL plus Rails/Django/Alembic DSLs)
and tells you which operations are unsafe, *why* (the locking/rewrite behavior),
and the zero-downtime rewrite.
[Rules](plugins/migration-guard/skills/migration-guard/references/rules.md) ·
[Postgres locking model](plugins/migration-guard/skills/migration-guard/references/postgres-locks.md)

### 🩺 llm-app-doctor

**Catch the mistakes that break AI apps in production.** A key committed to git,
user input concatenated into the *system* prompt (a prompt-injection sink), a
retired model ID, a `temperature`/`budget_tokens` that now returns a 400, a
`max_tokens` that trips the SDK timeout, a `datetime.now()` in the system prompt
that silently disables prompt caching. Covers Anthropic (authoritatively) plus
OpenAI and compatible SDKs in Python and JS/TS.
[Rules](plugins/llm-app-doctor/skills/llm-app-doctor/references/rules.md) ·
[Safe patterns](plugins/llm-app-doctor/skills/llm-app-doctor/references/safe-patterns.md)

### 🐳 dockerfile-doctor

**Stop shipping insecure, bloated images.** Flags running as root, `:latest`
base tags, secrets baked into `ENV`/`ARG` (recoverable via `docker history`),
`ADD` misuse, `curl ... | sh`, apt/pip caches left in layers, and dependency
installs ordered to bust Docker's layer cache.
[Rules](plugins/dockerfile-doctor/skills/dockerfile-doctor/references/rules.md)

### 🔑 env-doctor

**Stop leaking secrets and breaking onboarding via `.env`.** Reconciles the
variables your code reads, your real `.env`, and your committed `.env.example`,
flagging an un-ignored `.env`, vars the example forgot to document, dead example
entries, placeholder values left in `.env`, and real secrets committed to the
example.
[Rules](plugins/env-doctor/skills/env-doctor/references/rules.md)

### 🔍 regression-finder

**"This used to work" → the exact commit, in one command.** Drives an automated
`git bisect run` from a known-good ref and a repro command, with a clean-tree
guard, a `--verify` boundary check, and guaranteed branch restoration. Prints the
culprit commit and its diff.
[Recipes](plugins/regression-finder/skills/regression-finder/references/recipes.md)

### 🔐 actions-guard

**Stop attackers from owning your CI.** Security-lints GitHub Actions workflows
for the supply-chain and injection mistakes that leak secrets or run attacker
code: third-party actions pinned to a mutable tag instead of a commit SHA,
`pull_request_target` workflows that check out untrusted PR code with secrets
("pwn requests"), `${{ github.event.* }}` interpolated straight into a `run:`
shell (script injection), over-broad `GITHUB_TOKEN` permissions, and hard-coded
credentials. No PyYAML needed.
[Rules](plugins/actions-guard/skills/actions-guard/references/rules.md)

### 🔁 api-contract-guard

**Catch a breaking API change in review, not in production.** Diffs an old and a
new API schema and reports what breaks existing clients — removed
endpoints/types/fields, newly-required parameters/arguments, type changes,
removed enum values — while noting additive changes as non-breaking. Auto-detects
**OpenAPI/Swagger JSON** and **GraphQL SDL**.
[Rules](plugins/api-contract-guard/skills/api-contract-guard/references/rules.md)

### 🐙 compose-guard

**One line in a compose file can hand over your host.** Flags `privileged: true`,
a bind-mounted `/var/run/docker.sock` (full daemon control → host takeover, and
`:ro` doesn't help), host `network_mode`/`pid`/`ipc`, near-root capabilities like
`SYS_ADMIN`, `seccomp:unconfined`, `:latest` images, secrets hard-coded in
`environment:`, sensitive host bind mounts, and datastore ports published on
`0.0.0.0`. Correctly ignores the *safe* `cap_drop: [ALL]` pattern.
[Rules](plugins/compose-guard/skills/compose-guard/references/rules.md)

### ♿ a11y-guard

**Find the barriers that lock people out of your UI.** Catches images with no
`alt`, icon-only buttons that announce as just "button", `<div onClick>` with no
keyboard path, unlabelled form fields, positive `tabindex`, `aria-hidden` on
focusable elements (ghost focus), missing `<html lang>`, zoom-blocking viewports,
and "click here" links. Works on HTML, JSX/TSX, Vue and Svelte; each rule maps to
a WCAG criterion and explains *who* it blocks. Automated checks are a floor, not
a ceiling — the skill says so.
[Rules + WCAG mappings](plugins/a11y-guard/skills/a11y-guard/references/rules.md)

### 📝 commit-lint

**Conventional Commits, enforced — plus the changelog for free.** Validates header
format, type, scope, imperative lowercase subject, body wrapping, and
`BREAKING CHANGE` consistency (merge/revert/fixup commits are exempt by design).
Then `changelog` mode turns any revision range into a Keep-a-Changelog release
note: breaking changes first, grouped by type, scopes bolded, non-conventional
commits under **Other** so nothing silently disappears. Ships a ready-to-copy
`commit-msg` hook.
[Rules](plugins/commit-lint/skills/commit-lint/references/rules.md)

### 🎲 flaky-test-hunter

**A single green run proves nothing.** Runs your test command N times, ranks the
tests that both passed *and* failed by instability (most unstable first, with the
observed failure rate and which runs failed), and classifies the likely root
cause from each test's own failure output — timing, ordering/shared state,
randomness, network, concurrency, resource exhaustion, time-of-day, float
precision — with the fix that removes the non-determinism rather than hiding it.
Consistently-failing tests are reported separately (broken, not flaky), and it
prints the statistical confidence of your run count.
[Causes + fixes](plugins/flaky-test-hunter/skills/flaky-test-hunter/references/causes.md)

## Install

In Claude Code:

```
/plugin marketplace add aabhimittal/claude-skills
/plugin install migration-guard@claude-skills
/plugin install llm-app-doctor@claude-skills
/plugin install dockerfile-doctor@claude-skills
/plugin install env-doctor@claude-skills
/plugin install regression-finder@claude-skills
/plugin install actions-guard@claude-skills
/plugin install api-contract-guard@claude-skills
/plugin install compose-guard@claude-skills
/plugin install a11y-guard@claude-skills
/plugin install commit-lint@claude-skills
/plugin install flaky-test-hunter@claude-skills
```

Then just ask Claude naturally — *"is this migration safe to deploy?"*, *"audit
my AI code"*, *"why is my image so big?"*, *"is my .env in gitignore?"*, *"which
commit broke this test?"*, *"is my CI workflow secure?"*, *"did I break the
API?"*, *"why is mounting docker.sock bad?"*, *"is this component accessible?"*,
*"generate a changelog"*, *"which tests are flaky?"* — and the matching skill
activates automatically.

## Use the analyzers directly (no install required)

Every analyzer is plain Python with no dependencies, so you can run it standalone
or wire it into CI. Each exits non-zero when a finding meets `--fail-on`
(default `high`), and each has a `--selftest`.

```bash
# migration-guard
python3 plugins/migration-guard/skills/migration-guard/scripts/analyze_migration.py db/migrate/*.sql

# llm-app-doctor (scan a file or a whole tree)
python3 plugins/llm-app-doctor/skills/llm-app-doctor/scripts/analyze_llm_code.py ./src

# dockerfile-doctor
python3 plugins/dockerfile-doctor/skills/dockerfile-doctor/scripts/analyze_dockerfile.py Dockerfile

# env-doctor (point it at a project root)
python3 plugins/env-doctor/skills/env-doctor/scripts/analyze_env.py .

# regression-finder
python3 plugins/regression-finder/skills/regression-finder/scripts/regression_finder.py \
  --good v1.2.0 --verify -- pytest -x tests/test_foo.py::test_bar

# compose-guard
python3 plugins/compose-guard/skills/compose-guard/scripts/analyze_compose.py docker-compose.yml

# a11y-guard
python3 plugins/a11y-guard/skills/a11y-guard/scripts/analyze_a11y.py ./src

# commit-lint (lint a PR's commits, or generate a changelog)
python3 plugins/commit-lint/skills/commit-lint/scripts/commit_lint.py lint --range origin/main..HEAD
python3 plugins/commit-lint/skills/commit-lint/scripts/commit_lint.py changelog --range v1.1.0..HEAD

# flaky-test-hunter
python3 plugins/flaky-test-hunter/skills/flaky-test-hunter/scripts/hunt_flaky.py -n 20 --cmd "pytest -q"
```

Each `*-doctor`/analyzer ships paired examples (unsafe vs. safe) under its
`examples/` directory.

## Repository layout

```
.claude-plugin/marketplace.json          # marketplace catalog (lists all plugins)
plugins/
  <plugin>/
    .claude-plugin/plugin.json           # plugin manifest
    skills/<plugin>/
      SKILL.md                           # skill instructions + triggers
      scripts/                           # the analyzer/driver (stdlib only)
      references/                        # rule catalog / recipes
      examples/                          # paired unsafe vs. safe fixtures
```

## Contributing

New rules are welcome — each is a small function in the relevant analyzer with a
stable ID, severity, and remediation message, plus a case in that analyzer's
`--selftest` suite. Run `--selftest` before sending a change.

## License

[MIT](LICENSE) © 2026 Abhishek Mittal

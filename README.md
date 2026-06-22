# claude-skills

[![CI](https://github.com/aabhimittal/claude-skills/actions/workflows/ci.yml/badge.svg)](https://github.com/aabhimittal/claude-skills/actions/workflows/ci.yml)

An open-source [Claude Code](https://code.claude.com) plugin marketplace — five
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

## Install

In Claude Code:

```
/plugin marketplace add aabhimittal/claude-skills
/plugin install migration-guard@claude-skills
/plugin install llm-app-doctor@claude-skills
/plugin install dockerfile-doctor@claude-skills
/plugin install env-doctor@claude-skills
/plugin install regression-finder@claude-skills
```

Then just ask Claude naturally — *"is this migration safe to deploy?"*, *"audit
my AI code"*, *"why is my image so big?"*, *"is my .env in gitignore?"*, *"which
commit broke this test?"* — and the matching skill activates automatically.

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

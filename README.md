# claude-skills

An open-source [Claude Code](https://code.claude.com) plugin marketplace.

## Plugins

### 🛡️ migration-guard

**Catch dangerous database migrations before they take production down.**

A schema migration that looks innocent in review — adding a column, an index, a
foreign key — can lock a large table for minutes, rewrite every row, or destroy
data when it runs against a busy production database. `migration-guard` is a
Claude Code skill that statically analyzes migrations and tells you exactly
which operations are unsafe, *why* (the locking/rewrite behavior behind it), and
how to rewrite them to run online with zero downtime.

It ships with a dependency-free, deterministic Python analyzer so the dangerous
patterns are caught by code, not guessed at — and it doubles as a CI gate.

**Detects, among others:**

- `ADD COLUMN ... NOT NULL` without a default, or with a volatile default
  (`now()`, `gen_random_uuid()`) → full table rewrite
- `CREATE INDEX` / `REINDEX` without `CONCURRENTLY`, and `CONCURRENTLY` wrongly
  placed inside a transaction
- Foreign keys / `CHECK` constraints added without `NOT VALID` + `VALIDATE`
- `ALTER COLUMN ... TYPE` table rewrites
- Destructive ops: `DROP COLUMN`, `DROP TABLE`, `TRUNCATE`, unqualified
  `UPDATE`/`DELETE`
- App-breaking `RENAME`s, and a missing `lock_timeout` guard
- Framework equivalents in **Rails/ActiveRecord, Django, and Alembic**

See [the rule catalog](plugins/migration-guard/skills/migration-guard/references/rules.md)
and [the PostgreSQL locking reference](plugins/migration-guard/skills/migration-guard/references/postgres-locks.md).

### 🩺 llm-app-doctor

**Catch the mistakes that break AI apps in production — before they ship.**

The code that calls an LLM is where AI apps quietly break: a key committed to
git, user input concatenated into the *system* prompt (a prompt-injection
sink), a model ID that was retired months ago, a `temperature` or
`budget_tokens` that now returns a 400 on current models, a `max_tokens` so
large it trips the SDK's HTTP timeout, or a `datetime.now()` in the system
prompt that silently disables prompt caching. `llm-app-doctor` statically audits
your AI integration code and reports each issue with the line, the production
consequence, and the fix.

It ships a dependency-free, deterministic analyzer with current model/API
knowledge, covering **Anthropic** (authoritatively) plus **OpenAI** and
compatible SDKs in **Python and JavaScript/TypeScript**.

**Detects:** hard-coded API keys · prompt-injection sinks in system prompts ·
retired/deprecated/date-suffixed model IDs · `budget_tokens` /
`temperature` / `output_format` that now error or are deprecated · missing
`max_tokens` · large `max_tokens` without streaming · prompt-cache busters.

See [the rule catalog](plugins/llm-app-doctor/skills/llm-app-doctor/references/rules.md)
and [the safe-patterns reference](plugins/llm-app-doctor/skills/llm-app-doctor/references/safe-patterns.md).

```bash
# scan a file or a whole tree; gate CI on findings
python3 plugins/llm-app-doctor/skills/llm-app-doctor/scripts/analyze_llm_code.py ./src --fail-on high
```

## Install

In Claude Code:

```
/plugin marketplace add aabhimittal/claude-skills
/plugin install migration-guard@claude-skills
/plugin install llm-app-doctor@claude-skills
```

Then just ask Claude things like *"is this migration safe to deploy?"* or
*"audit my AI code for injection and dead models"* — the matching skill
activates automatically.

### Use the analyzers directly (no install required)

Both analyzers are plain Python with no dependencies, so you can run them
standalone or wire them into CI:

```bash
# migration-guard
python3 plugins/migration-guard/skills/migration-guard/scripts/analyze_migration.py db/migrate/0042_add_index.sql

# llm-app-doctor (scan a file or a whole tree)
python3 plugins/llm-app-doctor/skills/llm-app-doctor/scripts/analyze_llm_code.py ./src --fail-on medium
```

Each exits non-zero when a finding meets the `--fail-on` threshold (default
`high`). Try them on the bundled examples:

```bash
cd plugins/migration-guard/skills/migration-guard/scripts
python3 analyze_migration.py ../examples/unsafe_migration.sql   # 2 critical, 4 high
python3 analyze_migration.py ../examples/safe_migration.sql     # clean ✓
python3 analyze_migration.py --selftest                         # verify the analyzer

cd ../../../../llm-app-doctor/skills/llm-app-doctor/scripts
python3 analyze_llm_code.py ../examples/unsafe_app.py           # 2 critical, 4 high, 1 medium
python3 analyze_llm_code.py ../examples/safe_app.py             # clean ✓
python3 analyze_llm_code.py --selftest                          # verify the analyzer
```

## Repository layout

```
.claude-plugin/marketplace.json        # marketplace catalog (lists both plugins)
plugins/
  migration-guard/
    .claude-plugin/plugin.json         # plugin manifest
    skills/migration-guard/
      SKILL.md                         # skill instructions
      scripts/analyze_migration.py     # the analyzer (stdlib only)
      references/                      # rule catalog + locking model
      examples/                        # unsafe vs. safe migrations
  llm-app-doctor/
    .claude-plugin/plugin.json
    skills/llm-app-doctor/
      SKILL.md
      scripts/analyze_llm_code.py      # the analyzer (stdlib only)
      references/                      # rule catalog + safe patterns
      examples/                        # unsafe vs. safe apps (Python + TS)
```

## Contributing

New rules are welcome — each rule is a small function in the relevant analyzer
with a stable ID, severity, and a remediation message, plus a case in that
analyzer's `--selftest` suite. Run `--selftest` before sending a change.

## License

[MIT](LICENSE) © 2026 Abhishek Mittal

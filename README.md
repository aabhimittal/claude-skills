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

## Install

In Claude Code:

```
/plugin marketplace add aabhimittal/claude-skills
/plugin install migration-guard@claude-skills
```

Then just ask Claude things like *"is this migration safe to deploy?"* and the
skill activates automatically.

### Use the analyzer directly (no install required)

The analyzer is plain Python with no dependencies, so you can run it standalone
or wire it into CI:

```bash
python3 plugins/migration-guard/skills/migration-guard/scripts/analyze_migration.py db/migrate/0042_add_index.sql
# exits non-zero if any high/critical finding (configurable via --fail-on)
```

```bash
# CI example: fail the build on medium-or-worse findings
python3 .../analyze_migration.py --fail-on medium migrations/*.sql
```

Try it on the bundled examples:

```bash
cd plugins/migration-guard/skills/migration-guard/scripts
python3 analyze_migration.py ../examples/unsafe_migration.sql   # 2 critical, 4 high
python3 analyze_migration.py ../examples/safe_migration.sql     # clean ✓
python3 analyze_migration.py --selftest                         # verify the analyzer
```

## Repository layout

```
.claude-plugin/marketplace.json        # marketplace catalog
plugins/
  migration-guard/
    .claude-plugin/plugin.json         # plugin manifest
    skills/
      migration-guard/
        SKILL.md                       # skill instructions
        scripts/analyze_migration.py   # the analyzer (stdlib only)
        references/                    # rule catalog + locking model
        examples/                      # unsafe vs. safe migrations
```

## Contributing

New rules are welcome — each rule is a small function in `analyze_migration.py`
with a stable ID, severity, and a remediation message, plus a case in the
`--selftest` suite. Run `python3 analyze_migration.py --selftest` before sending
a change.

## License

[MIT](LICENSE) © 2026 Abhishek Mittal

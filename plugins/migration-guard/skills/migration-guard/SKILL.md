---
name: migration-guard
description: This skill should be used when the user asks to "review this migration", "is this migration safe", "check my database migration", "will this migration lock the table", "safe migration", or works with schema-change files (Postgres SQL, Rails/ActiveRecord, Django, Alembic/SQLAlchemy, Flyway, Prisma). It statically analyzes migrations for operations that cause production downtime, table locks, full-table rewrites, or irreversible data loss, and proposes online-safe rewrites.
version: 1.0.0
---

# migration-guard

Audit database schema migrations **before they run** for operations that lock
tables, rewrite whole tables, or destroy data on a busy production database —
and rewrite them into online-safe equivalents.

## When to use this skill

Use it whenever a migration is being written, reviewed, or about to be applied:
SQL DDL files, or framework migrations (Rails/ActiveRecord, Django, Alembic,
Flyway, Prisma). Typical triggers: "is this migration safe?", "will this lock
the table?", "review my migration", or any diff that adds/removes columns,
indexes, or constraints.

## Workflow

1. **Locate the migration file(s).** Look at the diff, the path the user named,
   or the framework's migration directory (e.g. `db/migrate/`, `migrations/`,
   `alembic/versions/`).

2. **Run the analyzer.** It is dependency-free standard-library Python:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/migration-guard/scripts/analyze_migration.py" path/to/migration.sql
   ```

   Pass multiple files, or `-` to read from stdin. Useful flags:
   - `--json` — machine-readable output for further processing or CI.
   - `--fail-on {info,low,medium,high,critical}` — exit non-zero at/above this
     severity (default `high`). Use this to gate CI.
   - `--selftest` — sanity-check the analyzer itself.

   The script exits `1` when findings meet the threshold, `0` when clean, so it
   doubles as a CI check.

3. **Interpret the findings.** Each finding has a rule ID, severity, the exact
   line, why it is risky, and a concrete fix. Read `references/rules.md` for the
   full catalog and `references/postgres-locks.md` for the locking model behind
   each rule.

4. **Explain and rewrite.** Summarize the real-world impact in plain terms
   ("this rewrites the whole `users` table under an exclusive lock — every query
   blocks until it finishes"), then offer an online-safe rewrite. The
   `examples/` folder pairs an unsafe migration with its safe version; use the
   same expand/backfill/contract patterns.

5. **State the caveat.** The analyzer cannot see table sizes. On a small or
   empty table many flagged operations are fine. Frame findings as "this is
   dangerous *at production scale* — here is the safe form," not as absolute
   bans. When the table is known to be tiny, say so.

## What it detects (summary)

- Column adds that rewrite the table: `NOT NULL` without default, volatile
  defaults like `now()` / `gen_random_uuid()`.
- Index builds that block writes: `CREATE INDEX` / `REINDEX` without
  `CONCURRENTLY`; and `CONCURRENTLY` mistakenly placed inside a transaction.
- Constraint adds that scan the whole table: foreign keys and `CHECK` without
  `NOT VALID` + `VALIDATE CONSTRAINT`.
- Type changes (`ALTER COLUMN ... TYPE`) that rewrite the table.
- Destructive/irreversible ops: `DROP COLUMN`, `DROP TABLE`, `TRUNCATE`,
  unqualified `UPDATE`/`DELETE`.
- App-breaking renames that need an expand/contract rollout.
- A missing `lock_timeout` guard before risky DDL.
- Framework equivalents in Rails, Alembic, and Django migration DSLs.

See `references/rules.md` for every rule ID and severity.

## Output style for the user

Lead with the headline (e.g. "2 critical, 4 high — do not apply as-is"), then
walk through findings worst-first, and finish with a corrected migration the
user can copy. Keep the explanation about *impact under load*, not just syntax.

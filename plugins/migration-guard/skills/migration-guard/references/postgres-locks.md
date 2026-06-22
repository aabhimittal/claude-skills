# PostgreSQL locking & rewrite reference

A quick reference for *why* the analyzer flags each operation. The core idea:
on a busy production database, a migration is dangerous when it (a) holds an
`ACCESS EXCLUSIVE` lock for a long time, or (b) rewrites/scans the whole table
while holding any blocking lock. Short metadata-only changes are fine; it is
the duration under lock that causes the outage.

## Lock modes that matter

| Lock mode | Blocks | Typical source |
| --- | --- | --- |
| `ACCESS EXCLUSIVE` | everything, including `SELECT` | most `ALTER TABLE`, `DROP`, `TRUNCATE`, `VACUUM FULL`, non-concurrent `CREATE INDEX` (blocks writes) |
| `SHARE` | writes | `CREATE INDEX` (non-concurrent) |
| `SHARE UPDATE EXCLUSIVE` | other DDL, not normal reads/writes | `CREATE INDEX CONCURRENTLY`, `VALIDATE CONSTRAINT`, `ALTER ... SET NOT NULL` validation step |

The danger is not just holding the lock — it is that a blocked DDL statement
sits in the lock queue *ahead* of every subsequent query, so one stuck
`ALTER TABLE` can freeze all traffic to the table. This is why a short
`lock_timeout` is recommended before any risky DDL.

## Operations and safe alternatives

### Adding a column
- Plain `ADD COLUMN` (nullable, no default): **safe** — metadata only.
- `ADD COLUMN ... DEFAULT <constant>`: **safe on PostgreSQL 11+** (stored in
  catalog, no rewrite).
- `ADD COLUMN ... NOT NULL` (no default): **unsafe** — fails on a populated
  table; needs a rewrite.
- `ADD COLUMN ... DEFAULT <volatile>` (e.g. `now()`, `gen_random_uuid()`):
  **unsafe** — every row is written, full rewrite under `ACCESS EXCLUSIVE`.

Safe pattern for NOT NULL:
1. `ADD COLUMN col ... ` (nullable)
2. backfill in batches
3. `ADD CONSTRAINT chk CHECK (col IS NOT NULL) NOT VALID`
4. `VALIDATE CONSTRAINT chk`
5. `ALTER COLUMN col SET NOT NULL` (PG12+ reuses the validated constraint and
   skips the scan)

### Indexes
- `CREATE INDEX`: blocks writes for the whole build. Use
  `CREATE INDEX CONCURRENTLY`.
- `CREATE INDEX CONCURRENTLY`: does not block writes, but **cannot run inside a
  transaction**, is not atomic, and can leave an `INVALID` index behind on
  failure (drop and retry). Disable the per-migration transaction.
- `REINDEX`: use `REINDEX ... CONCURRENTLY` (PG12+).

### Constraints
- Foreign keys and `CHECK` constraints validate every existing row under a
  lock. Add them `NOT VALID`, then `VALIDATE CONSTRAINT` separately.

### Changing a column type
- `ALTER COLUMN ... TYPE` usually rewrites the whole table. Prefer add-new-
  column / backfill / swap / drop-old across deploys. A few casts are exempt
  (e.g. increasing `varchar(n)` length, `int`→numeric in some versions) —
  confirm for your exact case and version.

### Destructive / app-breaking
- `DROP COLUMN`, `DROP TABLE`, `TRUNCATE`: irreversible data loss.
- `RENAME COLUMN` / `RENAME TABLE`: instant, but breaks deployed code reading
  the old name. Use expand/contract (add new name, dual-write, migrate readers,
  drop old name later).
- Unqualified `UPDATE`/`DELETE`: rewrites every row in one statement — batch it.

## The lock_timeout guard

```sql
SET lock_timeout = '5s';
SET statement_timeout = '15s';  -- optional, bound the whole statement
```

With this in place, a contended DDL statement fails fast instead of stalling
the table behind a long-running query. Wrap-and-retry in application/migration
tooling.

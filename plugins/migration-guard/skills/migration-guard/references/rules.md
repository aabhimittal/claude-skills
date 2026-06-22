# migration-guard rule catalog

Every finding carries a stable rule ID so it can be referenced, suppressed, or
discussed. Severities: `critical` > `high` > `medium` > `low` > `info`. The
default CI gate (`--fail-on`) is `high`.

## SQL rules (PostgreSQL-oriented)

| Rule | Severity | Trigger | Core risk |
| --- | --- | --- | --- |
| PG001 | critical | `ADD COLUMN ... NOT NULL` with no `DEFAULT` | fails / rewrites table under ACCESS EXCLUSIVE |
| PG002 | high | `ADD COLUMN ... DEFAULT <volatile>` | full table rewrite |
| PG003 | high | `CREATE INDEX` without `CONCURRENTLY` | blocks writes for the whole build |
| PG004 | critical | `CONCURRENTLY` inside a transaction | runtime error — not allowed in a txn |
| PG005 | high | `ADD ... FOREIGN KEY` without `NOT VALID` | validates all rows under a lock |
| PG006 | high | `ADD CONSTRAINT ... CHECK` without `NOT VALID` | full-table scan under a lock |
| PG007 | critical | `ALTER COLUMN ... TYPE` | full table rewrite under ACCESS EXCLUSIVE |
| PG008 | high | `ALTER COLUMN ... SET NOT NULL` | full-table scan under a lock |
| PG009 | high | `DROP COLUMN` | irreversible data loss; breaks running code |
| PG010 | critical | `DROP TABLE` / `TRUNCATE` | irreversible data loss |
| PG011 | medium | `RENAME COLUMN` / `RENAME TO` | breaks deployed code referencing old name |
| PG012 | high | `UPDATE`/`DELETE` without `WHERE` | locks every row; WAL bloat; replica lag |
| PG013 | medium | explicit `LOCK TABLE` | blocks others for the transaction |
| PG014 | high | `VACUUM FULL` / `CLUSTER` | rewrites table under ACCESS EXCLUSIVE |
| PG016 | low | risky DDL present, no `lock_timeout` set | a blocked DDL stalls all table traffic |
| PG017 | high | `REINDEX` without `CONCURRENTLY` | locks out writes during rebuild |

## ORM / migration-DSL rules

These are best-effort line-based checks for migrations written in a framework
DSL rather than raw SQL.

| Rule | Severity | Trigger |
| --- | --- | --- |
| RAILS001 | high | `add_index` without `algorithm: :concurrently` |
| RAILS002 | high | `add_column`/`add_reference` `null: false` without `default:` |
| RAILS003 | high | `remove_column` / `drop_table` (destructive) |
| ALEMBIC001 | high | `op.create_index(...)` without `postgresql_concurrently=True` |
| DJANGO001 | info | `migrations.AddField` — review null/default and indexing |

## Notes & limitations

- The analyzer is a **static linter**, not a database connection. It does not
  know table sizes; on a tiny or empty table, several flagged operations are
  perfectly fine. Treat findings as "stop and think about production scale,"
  not as absolute prohibitions.
- Detection is pattern-based. Highly unusual formatting or dynamically
  generated SQL may be missed. The scrubber removes comments and string
  literals to avoid false positives, so keywords inside those do not trigger.
- MySQL/MariaDB and other engines have different (often more forgiving, e.g.
  online DDL) locking behavior. Rules are written for PostgreSQL semantics;
  the destructive-operation rules still apply broadly.

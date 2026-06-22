#!/usr/bin/env python3
"""migration-guard: a static safety analyzer for database schema migrations.

Detects operations that commonly cause production downtime, long-held table
locks, full-table rewrites, or irreversible data loss. The analyzer is
PostgreSQL-focused (where locking behavior is well documented) and also runs
lighter pattern checks for the Rails, Django, and Alembic migration DSLs.

Design goals:
  * Pure Python standard library. No third-party deps, no network access.
  * Deterministic: the same input always produces the same findings.
  * CI-friendly: a non-zero exit code when findings meet a severity threshold.

Usage:
    analyze_migration.py FILE [FILE ...]        # analyze one or more files
    cat migration.sql | analyze_migration.py -  # read SQL from stdin
    analyze_migration.py --json FILE            # machine-readable output
    analyze_migration.py --fail-on medium FILE  # gate CI at a threshold
    analyze_migration.py --selftest             # run built-in checks

Exit codes:
    0  no findings at or above the --fail-on threshold (default: high)
    1  one or more findings at or above the threshold
    2  usage / runtime error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Callable, Iterable

# --------------------------------------------------------------------------- #
# Severity model
# --------------------------------------------------------------------------- #

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SEVERITY_LABEL = {
    "info": "INFO",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "critical": "CRITICAL",
}


@dataclass
class Finding:
    rule: str
    severity: str
    title: str
    detail: str
    fix: str
    file: str = ""
    line: int = 0
    snippet: str = ""

    def sort_key(self) -> tuple:
        # Most severe first, then by file/line for stable ordering.
        return (-SEVERITY_ORDER[self.severity], self.file, self.line, self.rule)


# --------------------------------------------------------------------------- #
# SQL scrubbing — remove comments and string/dollar-quoted literals while
# preserving byte offsets (and therefore line numbers) so that matches made on
# the scrubbed text map cleanly back to the original source.
# --------------------------------------------------------------------------- #


def scrub_sql(text: str) -> str:
    """Return a same-length copy of ``text`` with comments and string literals
    blanked to spaces (newlines preserved). This prevents false positives from
    keywords appearing inside comments or string constants.
    """
    out = list(text)
    n = len(text)
    i = 0
    state = "normal"
    dollar_tag = ""

    def blank(idx: int) -> None:
        if out[idx] != "\n":
            out[idx] = " "

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if state == "normal":
            if c == "-" and nxt == "-":
                state = "line_comment"
                blank(i)
                i += 1
                continue
            if c == "/" and nxt == "*":
                state = "block_comment"
                blank(i)
                i += 1
                continue
            if c == "'":
                state = "single"
                i += 1
                continue
            if c == '"':
                state = "double"
                i += 1
                continue
            if c == "$":
                m = re.match(r"\$[A-Za-z0-9_]*\$", text[i:])
                if m:
                    dollar_tag = m.group(0)
                    state = "dollar"
                    for j in range(i, i + len(dollar_tag)):
                        blank(j)
                    i += len(dollar_tag)
                    continue
            i += 1
            continue

        if state == "line_comment":
            if c == "\n":
                state = "normal"
            else:
                blank(i)
            i += 1
            continue

        if state == "block_comment":
            if c == "*" and nxt == "/":
                blank(i)
                blank(i + 1)
                i += 2
                state = "normal"
                continue
            blank(i)
            i += 1
            continue

        if state == "single":
            if c == "'" and nxt == "'":  # escaped quote
                i += 2
                continue
            if c == "'":
                state = "normal"
                i += 1
                continue
            blank(i)
            i += 1
            continue

        if state == "double":
            if c == '"':
                state = "normal"
            i += 1
            continue

        if state == "dollar":
            if text[i:].startswith(dollar_tag):
                for j in range(i, i + len(dollar_tag)):
                    blank(j)
                i += len(dollar_tag)
                state = "normal"
                continue
            blank(i)
            i += 1
            continue

    return "".join(out)


def line_starts(text: str) -> list[int]:
    starts = [0]
    for idx, ch in enumerate(text):
        if ch == "\n":
            starts.append(idx + 1)
    return starts


def offset_to_line(starts: list[int], offset: int) -> int:
    # Binary search for the greatest start <= offset.
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


@dataclass
class Statement:
    text: str          # scrubbed, original case
    lower: str         # scrubbed, lowercased, whitespace-collapsed
    offset: int        # offset of first non-space char in the original text
    line: int


def split_statements(scrubbed: str, starts: list[int]) -> list[Statement]:
    statements: list[Statement] = []
    seg_start = 0
    for i, ch in enumerate(scrubbed):
        if ch == ";":
            statements.append(_make_statement(scrubbed, seg_start, i, starts))
            seg_start = i + 1
    if seg_start < len(scrubbed):
        statements.append(_make_statement(scrubbed, seg_start, len(scrubbed), starts))
    return [s for s in statements if s.lower.strip()]


def _make_statement(scrubbed: str, a: int, b: int, starts: list[int]) -> Statement:
    raw = scrubbed[a:b]
    # Find first non-space offset for the line number.
    off = a
    while off < b and scrubbed[off] in " \t\r\n":
        off += 1
    collapsed = re.sub(r"\s+", " ", raw).strip().lower()
    return Statement(text=raw, lower=collapsed, offset=off, line=offset_to_line(starts, off))


# --------------------------------------------------------------------------- #
# Rule engine
# --------------------------------------------------------------------------- #

VOLATILE_DEFAULT = re.compile(
    r"default\s+[^,)]*\b(now|current_timestamp|current_date|current_time|"
    r"clock_timestamp|statement_timestamp|transaction_timestamp|timeofday|"
    r"random|gen_random_uuid|uuid_generate_v\d|nextval)\b",
    re.IGNORECASE,
)


@dataclass
class Context:
    explicit_txn: bool
    in_transaction: str   # "yes" | "no" | "auto"
    has_lock_timeout: bool
    has_risky_ddl: bool


# Each SQL rule inspects a single (scrubbed, lowercased) statement and yields
# zero or more Finding objects.
SqlRule = Callable[[Statement, Context], Iterable[Finding]]


def _f(rule, severity, title, detail, fix, stmt: Statement) -> Finding:
    snippet = re.sub(r"\s+", " ", stmt.text).strip()
    if len(snippet) > 140:
        snippet = snippet[:137] + "..."
    return Finding(rule=rule, severity=severity, title=title, detail=detail, fix=fix,
                   line=stmt.line, snippet=snippet)


def in_txn(ctx: Context) -> bool:
    if ctx.in_transaction == "yes":
        return True
    if ctx.in_transaction == "no":
        return False
    return ctx.explicit_txn


def rule_add_column_not_null(stmt: Statement, ctx: Context):
    s = stmt.lower
    if "alter table" in s and "add column" in s and re.search(r"\bnot null\b", s):
        if "default" not in s:
            yield _f(
                "PG001", "critical",
                "ADD COLUMN NOT NULL without a DEFAULT",
                "Adding a NOT NULL column with no default fails on a populated "
                "table and requires a full table rewrite under an ACCESS "
                "EXCLUSIVE lock, blocking all reads and writes.",
                "Add the column nullable, backfill in batches, then "
                "`ALTER TABLE ... ADD CONSTRAINT ... CHECK (col IS NOT NULL) NOT VALID`, "
                "`VALIDATE CONSTRAINT`, and finally `SET NOT NULL`.",
                stmt,
            )


def rule_volatile_default(stmt: Statement, ctx: Context):
    s = stmt.lower
    if "alter table" in s and "add column" in s and VOLATILE_DEFAULT.search(stmt.text):
        yield _f(
            "PG002", "high",
            "ADD COLUMN with a volatile DEFAULT",
            "A volatile default (e.g. now(), gen_random_uuid(), random()) forces "
            "PostgreSQL to rewrite every row under an ACCESS EXCLUSIVE lock, even "
            "on PostgreSQL 11+ where constant defaults are cheap.",
            "Add the column with no default, backfill existing rows in batches, "
            "then set the default for new rows in a separate statement.",
            stmt,
        )


def rule_create_index(stmt: Statement, ctx: Context):
    s = stmt.lower
    if re.search(r"\bcreate\b.*\bindex\b", s) and "concurrently" not in s:
        yield _f(
            "PG003", "high",
            "CREATE INDEX without CONCURRENTLY",
            "Building an index without CONCURRENTLY holds a lock that blocks all "
            "writes (INSERT/UPDATE/DELETE) to the table until the build finishes.",
            "Use `CREATE INDEX CONCURRENTLY` (note: it must run outside an "
            "explicit transaction and is not atomic — verify and retry on failure).",
            stmt,
        )


def rule_concurrently_in_txn(stmt: Statement, ctx: Context):
    s = stmt.lower
    if "concurrently" in s and in_txn(ctx):
        yield _f(
            "PG004", "critical",
            "CONCURRENTLY inside a transaction",
            "CREATE/DROP INDEX CONCURRENTLY and REINDEX CONCURRENTLY cannot run "
            "inside a transaction block and will raise an error. Many migration "
            "frameworks wrap each migration in a transaction by default.",
            "Run the statement outside any transaction. In Rails use "
            "`disable_ddl_transaction!`; in Django set `atomic = False`; in "
            "Alembic avoid the transactional wrapper for this migration.",
            stmt,
        )


def rule_add_fk(stmt: Statement, ctx: Context):
    s = stmt.lower
    if "alter table" in s and "add constraint" in s and "foreign key" in s and "not valid" not in s:
        yield _f(
            "PG005", "high",
            "ADD FOREIGN KEY without NOT VALID",
            "Adding a foreign key validates every existing row while holding a "
            "lock on both the referencing and referenced tables.",
            "Split into two steps: `ADD CONSTRAINT ... FOREIGN KEY ... NOT VALID`, "
            "then in a later statement/migration `VALIDATE CONSTRAINT` (which takes "
            "only a SHARE UPDATE EXCLUSIVE lock).",
            stmt,
        )


def rule_add_check(stmt: Statement, ctx: Context):
    s = stmt.lower
    if "alter table" in s and "add constraint" in s and re.search(r"\bcheck\b", s) and "not valid" not in s:
        yield _f(
            "PG006", "high",
            "ADD CHECK constraint without NOT VALID",
            "Adding a CHECK constraint scans the whole table to validate existing "
            "rows while holding an ACCESS EXCLUSIVE lock.",
            "Add it `NOT VALID` first, then `VALIDATE CONSTRAINT` in a separate "
            "statement to avoid the blocking full-table scan.",
            stmt,
        )


def rule_alter_type(stmt: Statement, ctx: Context):
    s = stmt.lower
    if "alter table" in s and re.search(r"alter (column )?\w+ (set data )?type", s):
        yield _f(
            "PG007", "critical",
            "ALTER COLUMN ... TYPE rewrites the table",
            "Changing a column's type generally rewrites the entire table under "
            "an ACCESS EXCLUSIVE lock, blocking reads and writes for the duration.",
            "Add a new column of the target type, backfill in batches, swap reads "
            "to it, then drop the old column across multiple deploys. Some widening "
            "casts (e.g. varchar length increases) are exempt — verify for your case.",
            stmt,
        )


def rule_set_not_null(stmt: Statement, ctx: Context):
    s = stmt.lower
    if "alter table" in s and re.search(r"alter (column )?\w+ set not null", s):
        yield _f(
            "PG008", "high",
            "SET NOT NULL scans the whole table",
            "ALTER COLUMN ... SET NOT NULL scans every row to verify the "
            "constraint while holding an ACCESS EXCLUSIVE lock.",
            "On PostgreSQL 12+, first add `CHECK (col IS NOT NULL) NOT VALID`, run "
            "`VALIDATE CONSTRAINT`, then `SET NOT NULL` — the planner reuses the "
            "validated constraint and skips the blocking scan.",
            stmt,
        )


def rule_drop_column(stmt: Statement, ctx: Context):
    s = stmt.lower
    if "alter table" in s and "drop column" in s:
        yield _f(
            "PG009", "high",
            "DROP COLUMN is destructive and irreversible",
            "Dropping a column is fast but permanently discards its data, and it "
            "breaks any running application code that still references the column.",
            "Confirm no deployed code reads or writes the column first. Ship the "
            "code change that stops using it, deploy, then drop the column in a "
            "later migration. Ensure you have a backup.",
            stmt,
        )


def rule_drop_or_truncate(stmt: Statement, ctx: Context):
    s = stmt.lower
    if re.match(r"drop table\b", s) or re.match(r"truncate\b", s):
        yield _f(
            "PG010", "critical",
            "DROP TABLE / TRUNCATE destroys data",
            "DROP TABLE and TRUNCATE permanently delete data and TRUNCATE takes an "
            "ACCESS EXCLUSIVE lock. Neither is reversible without a restore.",
            "Verify the table is truly unused, take a backup, and prefer a soft "
            "two-step retirement (rename out of the way first, drop later).",
            stmt,
        )


def rule_rename(stmt: Statement, ctx: Context):
    s = stmt.lower
    if "alter table" in s and ("rename column" in s or re.search(r"\brename to\b", s)):
        yield _f(
            "PG011", "medium",
            "RENAME breaks running application code",
            "Renaming a table or column is metadata-only and fast, but any "
            "currently deployed code referencing the old name breaks immediately, "
            "causing errors during the deploy window.",
            "Use an expand/contract migration: add the new name, backfill and "
            "dual-write, migrate readers, then drop the old name in a later deploy.",
            stmt,
        )


def rule_unqualified_dml(stmt: Statement, ctx: Context):
    s = stmt.lower
    if (re.match(r"update\s+\S", s) or re.match(r"delete\s+from\b", s)) and " where " not in f" {s} ":
        yield _f(
            "PG012", "high",
            "UPDATE/DELETE without a WHERE clause",
            "An unqualified UPDATE or DELETE touches every row in one statement, "
            "holding row locks for the whole table, bloating WAL, and creating "
            "replication lag.",
            "Add a WHERE clause and process large backfills in bounded batches "
            "(e.g. by primary-key ranges) with a commit between batches.",
            stmt,
        )


def rule_lock_table(stmt: Statement, ctx: Context):
    s = stmt.lower
    if re.match(r"lock table\b", s) or re.match(r"lock\s+\w", s):
        yield _f(
            "PG013", "medium",
            "Explicit LOCK TABLE",
            "An explicit LOCK can block other sessions for the rest of the "
            "transaction; the default mode is ACCESS EXCLUSIVE.",
            "Take the narrowest lock mode you need and keep the holding "
            "transaction as short as possible.",
            stmt,
        )


def rule_vacuum_cluster(stmt: Statement, ctx: Context):
    s = stmt.lower
    if re.match(r"vacuum\s+full\b", s) or re.match(r"cluster\b", s):
        yield _f(
            "PG014", "high",
            "VACUUM FULL / CLUSTER rewrites under a heavy lock",
            "VACUUM FULL and CLUSTER rewrite the entire table while holding an "
            "ACCESS EXCLUSIVE lock, making the table unavailable for the duration.",
            "Avoid in migrations. Use pg_repack for online bloat reclamation, or "
            "schedule during a maintenance window.",
            stmt,
        )


def rule_reindex(stmt: Statement, ctx: Context):
    s = stmt.lower
    if re.match(r"reindex\b", s) and "concurrently" not in s:
        yield _f(
            "PG017", "high",
            "REINDEX without CONCURRENTLY",
            "A plain REINDEX locks out writes (and reads, for some object types) "
            "until it completes.",
            "Use `REINDEX ... CONCURRENTLY` (PostgreSQL 12+), outside a transaction.",
            stmt,
        )


SQL_RULES: list[SqlRule] = [
    rule_add_column_not_null,
    rule_volatile_default,
    rule_create_index,
    rule_concurrently_in_txn,
    rule_add_fk,
    rule_add_check,
    rule_alter_type,
    rule_set_not_null,
    rule_drop_column,
    rule_drop_or_truncate,
    rule_rename,
    rule_unqualified_dml,
    rule_lock_table,
    rule_vacuum_cluster,
    rule_reindex,
]

RISKY_DDL = re.compile(
    r"\b(alter table|create index|drop table|truncate|reindex|cluster|vacuum full)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# ORM / migration-DSL pattern rules (line oriented, best effort)
# --------------------------------------------------------------------------- #

ORM_LINE_RULES = [
    (
        re.compile(r"\badd_index\b", re.I),
        re.compile(r"algorithm:\s*:concurrently", re.I),
        "RAILS001", "high",
        "Rails add_index without algorithm: :concurrently",
        "add_index builds the index with a lock that blocks writes to the table.",
        "Pass `algorithm: :concurrently` and add `disable_ddl_transaction!` to the "
        "migration (concurrent index builds cannot run in a transaction).",
    ),
    (
        re.compile(r"\b(add_column|add_reference)\b.*null:\s*false", re.I),
        re.compile(r"default:", re.I),
        "RAILS002", "high",
        "Rails add_column null: false without a default",
        "Adding a NOT NULL column with no default rewrites the table under a lock.",
        "Add the column nullable, backfill, then change it to null: false in a "
        "later step (or supply a constant default on PostgreSQL 11+).",
    ),
    (
        re.compile(r"\b(remove_column|drop_table)\b", re.I),
        None,
        "RAILS003", "high",
        "Rails remove_column / drop_table is destructive",
        "Removing a column or table permanently deletes data and can break "
        "running code that still references it.",
        "Ship the code that stops using it first, deploy, then remove in a later "
        "migration. Keep a backup.",
    ),
    (
        re.compile(r"\bop\.create_index\b", re.I),
        re.compile(r"postgresql_concurrently\s*=\s*True", re.I),
        "ALEMBIC001", "high",
        "Alembic create_index without postgresql_concurrently=True",
        "A non-concurrent index build locks out writes to the table.",
        "Pass `postgresql_concurrently=True` and disable the per-migration "
        "transaction so the concurrent build can run.",
    ),
    (
        re.compile(r"migrations\.AddField\b", re.I),
        None,
        "DJANGO001", "info",
        "Django AddField — check null/default handling",
        "A non-null AddField without a database default can rewrite the table; "
        "Django wraps migrations in a transaction by default.",
        "For large tables, add the field nullable, backfill with a data migration, "
        "then enforce NOT NULL; consider AddIndexConcurrently / "
        "atomic = False for index work.",
    ),
]


def analyze_orm(lines: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for idx, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for trigger, safe, rule, sev, title, detail, fix in ORM_LINE_RULES:
            if trigger.search(raw) and (safe is None or not safe.search(raw)):
                snippet = line[:140]
                findings.append(Finding(rule=rule, severity=sev, title=title,
                                        detail=detail, fix=fix, line=idx, snippet=snippet))
    return findings


# --------------------------------------------------------------------------- #
# File analysis
# --------------------------------------------------------------------------- #


def looks_like_orm(path: str, text: str) -> bool:
    if path.endswith((".rb", ".py")):
        return True
    return bool(re.search(r"\b(def change\b|def up\b|op\.\w+\(|migrations\.)", text))


def analyze_text(text: str, in_transaction: str = "auto") -> list[Finding]:
    scrubbed = scrub_sql(text)
    starts = line_starts(text)
    statements = split_statements(scrubbed, starts)

    low = scrubbed.lower()
    explicit_txn = bool(re.search(r"\bbegin\b|\bstart transaction\b", low))
    has_lock_timeout = "lock_timeout" in low
    has_risky_ddl = bool(RISKY_DDL.search(low))

    ctx = Context(
        explicit_txn=explicit_txn,
        in_transaction=in_transaction,
        has_lock_timeout=has_lock_timeout,
        has_risky_ddl=has_risky_ddl,
    )

    findings: list[Finding] = []
    for stmt in statements:
        for rule in SQL_RULES:
            findings.extend(rule(stmt, ctx))

    # File-level advisory: risky DDL but no lock_timeout guard.
    if has_risky_ddl and not has_lock_timeout:
        findings.append(Finding(
            rule="PG016", severity="low",
            title="No lock_timeout set before risky DDL",
            detail="The migration performs locking DDL but never sets a "
                   "lock_timeout. A blocked DDL statement can queue behind a long "
                   "query and then block every query behind it.",
            fix="Set a short guard at the top of the migration, e.g. "
                "`SET lock_timeout = '5s';` (and `statement_timeout`), so a "
                "contended lock fails fast instead of stalling the table.",
            line=1, snippet=""))

    return findings


def analyze_file(path: str) -> list[Finding]:
    if path == "-":
        text = sys.stdin.read()
        display = "<stdin>"
        is_orm = looks_like_orm("", text)
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        display = path
        is_orm = looks_like_orm(path, text)

    findings = analyze_text(text)
    if is_orm:
        findings.extend(analyze_orm(text.splitlines()))

    for f in findings:
        f.file = display
    return findings


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def render_text(findings: list[Finding], threshold: str) -> str:
    if not findings:
        return "migration-guard: no risky operations detected. ✓"

    lines = []
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    summary = ", ".join(
        f"{counts[s]} {SEVERITY_LABEL[s].lower()}"
        for s in ["critical", "high", "medium", "low", "info"]
        if counts.get(s)
    )
    lines.append(f"migration-guard found {len(findings)} item(s): {summary}\n")

    for f in findings:
        loc = f"{f.file}:{f.line}" if f.line else f.file
        lines.append(f"[{SEVERITY_LABEL[f.severity]}] {f.rule}  {f.title}")
        lines.append(f"  at {loc}")
        if f.snippet:
            lines.append(f"  > {f.snippet}")
        lines.append(f"  why: {f.detail}")
        lines.append(f"  fix: {f.fix}")
        lines.append("")

    gate = SEVERITY_ORDER[threshold]
    worst = max((SEVERITY_ORDER[f.severity] for f in findings), default=-1)
    if worst >= gate:
        lines.append(f"FAIL: findings at or above '{threshold}'. "
                     f"Review the locking behavior before applying this migration.")
    else:
        lines.append(f"OK: no findings at or above '{threshold}' (advisory items only).")
    return "\n".join(lines)


def render_json(findings: list[Finding], threshold: str) -> str:
    gate = SEVERITY_ORDER[threshold]
    worst = max((SEVERITY_ORDER[f.severity] for f in findings), default=-1)
    payload = {
        "tool": "migration-guard",
        "threshold": threshold,
        "passed": worst < gate,
        "count": len(findings),
        "findings": [asdict(f) for f in findings],
    }
    return json.dumps(payload, indent=2)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

SELFTEST_CASES = [
    ("ALTER TABLE users ADD COLUMN active boolean NOT NULL;", "PG001"),
    ("ALTER TABLE users ADD COLUMN created timestamptz DEFAULT now();", "PG002"),
    ("CREATE INDEX idx_users_email ON users (email);", "PG003"),
    ("BEGIN; CREATE INDEX CONCURRENTLY idx ON users (email); COMMIT;", "PG004"),
    ("ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY (uid) REFERENCES users (id);", "PG005"),
    ("ALTER TABLE orders ADD CONSTRAINT chk CHECK (total >= 0);", "PG006"),
    ("ALTER TABLE users ALTER COLUMN age TYPE bigint;", "PG007"),
    ("ALTER TABLE users ALTER COLUMN email SET NOT NULL;", "PG008"),
    ("ALTER TABLE users DROP COLUMN legacy;", "PG009"),
    ("DROP TABLE old_events;", "PG010"),
    ("ALTER TABLE users RENAME COLUMN name TO full_name;", "PG011"),
    ("UPDATE users SET active = true;", "PG012"),
    ("VACUUM FULL users;", "PG014"),
    ("REINDEX TABLE users;", "PG017"),
    ("add_index :users, :email", "RAILS001"),
]

SELFTEST_NEGATIVES = [
    "ALTER TABLE users ADD COLUMN active boolean NOT NULL DEFAULT false;",
    "CREATE INDEX CONCURRENTLY idx_users_email ON users (email);",
    "ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY (uid) REFERENCES users (id) NOT VALID;",
    "UPDATE users SET active = true WHERE id = 5;",
    "-- CREATE INDEX in a comment should not fire\nSELECT 1;",
    "SELECT 'DROP TABLE x' AS note;",
]


def run_selftest() -> int:
    failures = 0
    for sql, expected in SELFTEST_CASES:
        text = sql
        findings = analyze_text(text)
        if sql.startswith("add_index"):
            findings += analyze_orm(text.splitlines())
        rules = {f.rule for f in findings}
        if expected not in rules:
            failures += 1
            print(f"  MISS: expected {expected} for: {sql!r} -> got {sorted(rules)}")
    for sql in SELFTEST_NEGATIVES:
        findings = analyze_text(sql)
        bad = [f.rule for f in findings if f.severity in ("high", "critical")]
        if bad:
            failures += 1
            print(f"  FALSE POSITIVE: {bad} for: {sql!r}")
    if failures:
        print(f"selftest: {failures} failure(s)")
        return 1
    print(f"selftest: all {len(SELFTEST_CASES)} positive and "
          f"{len(SELFTEST_NEGATIVES)} negative cases passed ✓")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="analyze_migration.py",
        description="Static safety analyzer for database schema migrations.",
    )
    parser.add_argument("files", nargs="*", help="migration files, or - for stdin")
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    parser.add_argument("--fail-on", default="high",
                        choices=list(SEVERITY_ORDER.keys()),
                        help="minimum severity that causes a non-zero exit (default: high)")
    parser.add_argument("--selftest", action="store_true",
                        help="run built-in detection checks and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    if not args.files:
        parser.print_usage()
        print("error: provide at least one file (or - for stdin)", file=sys.stderr)
        return 2

    all_findings: list[Finding] = []
    for path in args.files:
        try:
            all_findings.extend(analyze_file(path))
        except FileNotFoundError:
            print(f"error: file not found: {path}", file=sys.stderr)
            return 2

    all_findings.sort(key=lambda f: f.sort_key())

    if args.json:
        print(render_json(all_findings, args.fail_on))
    else:
        print(render_text(all_findings, args.fail_on))

    gate = SEVERITY_ORDER[args.fail_on]
    worst = max((SEVERITY_ORDER[f.severity] for f in all_findings), default=-1)
    return 1 if worst >= gate else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

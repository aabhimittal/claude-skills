#!/usr/bin/env python3
"""commit-lint: a Conventional Commits linter and changelog generator.

Two modes over the same parser:

  lint       Validate commit messages against the Conventional Commits spec —
             malformed headers, unknown types, non-imperative or capitalized
             subjects, trailing periods, over-long headers/body lines, missing
             blank line before the body, and `!`/`BREAKING CHANGE` consistency.

  changelog  Generate a grouped, Keep-a-Changelog-style release note from git
             history, with breaking changes called out first.

Design goals:
  * Pure Python standard library. Git is shelled out to read-only (log/rev-parse).
  * Deterministic and CI-friendly (non-zero exit at/above --fail-on).

Usage:
    commit_lint.py lint --message "feat(api): add pagination"
    commit_lint.py lint --file .git/COMMIT_EDITMSG      # commit-msg hook
    commit_lint.py lint --range origin/main..HEAD       # lint a PR's commits
    commit_lint.py changelog --range v1.2.0..HEAD [--version 1.3.0]
    commit_lint.py --selftest

Exit codes: 0 clean, 1 findings at/above threshold, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict, field

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SEVERITY_LABEL = {k: k.upper() for k in SEVERITY_ORDER}

DEFAULT_TYPES = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance",
    "refactor": "Refactoring",
    "docs": "Documentation",
    "test": "Tests",
    "build": "Build System",
    "ci": "CI",
    "chore": "Chores",
    "style": "Styling",
    "revert": "Reverts",
}
CHANGELOG_ORDER = ["feat", "fix", "perf", "refactor", "docs", "test", "build",
                   "ci", "style", "revert", "chore"]
# Common non-imperative subject openers (past tense / gerund / 3rd person).
NON_IMPERATIVE = re.compile(
    r"^(added|adds|adding|fixed|fixes|fixing|updated|updates|updating|"
    r"removed|removes|removing|changed|changes|changing|created|creates|creating|"
    r"deleted|deletes|deleting|implemented|implements|implementing|"
    r"refactored|refactors|refactoring|renamed|renames|renaming|"
    r"improved|improves|improving|bumped|bumps|bumping|moved|moves|moving)\b",
    re.IGNORECASE,
)
HEADER = re.compile(
    r"^(?P<type>[a-zA-Z][a-zA-Z0-9]*)"
    r"(?:\((?P<scope>[^()]*)\))?"
    r"(?P<bang>!)?"
    r": (?P<subject>.+)$"
)
MERGE_OR_REVERT = re.compile(r"^(Merge |Revert \"|fixup! |squash! |amend! )")

HEADER_MAX = 72
BODY_MAX = 100


@dataclass
class Finding:
    rule: str
    severity: str
    title: str
    detail: str
    fix: str
    commit: str = ""
    header: str = ""

    def sort_key(self) -> tuple:
        return (-SEVERITY_ORDER[self.severity], self.commit, self.rule)


@dataclass
class Commit:
    sha: str = ""
    raw: str = ""
    header: str = ""
    type: str = ""
    scope: str = ""
    subject: str = ""
    breaking: bool = False
    breaking_desc: str = ""
    body: str = ""
    valid: bool = False
    skipped: bool = False
    findings: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Parsing / linting
# --------------------------------------------------------------------------- #


def parse_commit(raw: str, sha: str = "", allowed_types: set | None = None) -> Commit:
    types = allowed_types if allowed_types is not None else set(DEFAULT_TYPES)
    lines = raw.rstrip().split("\n")
    header = lines[0].strip() if lines else ""
    c = Commit(sha=sha, raw=raw, header=header)

    if not header:
        c.findings.append(Finding(
            "CL001", "high", "Empty commit message",
            "The commit has no subject line at all.",
            "Write a header: `type(scope): imperative summary`.", sha, header))
        return c

    # Merge/revert/fixup commits are exempt from the format rules.
    if MERGE_OR_REVERT.match(header):
        c.skipped = True
        return c

    m = HEADER.match(header)
    if not m:
        # Give a targeted hint for the most common malformations.
        def _short(s: str, n: int = 40) -> str:
            """Truncate on a word boundary so the suggestion stays readable."""
            s = s.strip()
            if len(s) <= n:
                return s
            cut = s[:n].rsplit(" ", 1)[0]
            return (cut or s[:n]) + "…"

        hint = ("Use `type(scope): subject` — e.g. `feat(auth): add SSO login`. "
                "Note the colon **and** the single space after it.")
        if re.match(r"^[a-zA-Z][a-zA-Z0-9]*(\([^)]*\))?!?:[^ ]", header):
            hint = "Add a space after the colon: `type(scope): subject`."
        elif not re.match(r"^[a-zA-Z][a-zA-Z0-9]*(\([^)]*\))?!?:", header):
            # No `type:` prefix at all — the most common case.
            hint = ("Add a type prefix and colon: "
                    f"`feat: {_short(header.rstrip('.'))}` (or fix/docs/chore/…). "
                    "Lowercase the subject and use the imperative mood.")
        c.findings.append(Finding(
            "CL002", "high", "Header does not follow Conventional Commits",
            f"`{header}` doesn't match `type(scope)!: subject`.", hint, sha, header))
        return c

    c.type = m.group("type")
    c.scope = m.group("scope") or ""
    c.subject = m.group("subject").strip()
    c.breaking = bool(m.group("bang"))
    c.valid = True

    if c.type.lower() != c.type:
        c.findings.append(Finding(
            "CL003", "medium", "Type is not lowercase",
            f"Type `{c.type}` should be lowercase.",
            f"Use `{c.type.lower()}`.", sha, header))
    if c.type.lower() not in types:
        c.findings.append(Finding(
            "CL004", "medium", "Unknown commit type",
            f"`{c.type}` is not one of the allowed types "
            f"({', '.join(sorted(types))}).",
            "Use an allowed type, or extend the set with `--types`.", sha, header))
    if m.group("scope") is not None and not c.scope.strip():
        c.findings.append(Finding(
            "CL005", "low", "Empty scope parentheses",
            "`()` with no scope inside adds noise.",
            "Name the scope (`feat(api): …`) or drop the parentheses.", sha, header))

    if c.subject[:1].isupper() and not re.match(r"^[A-Z]{2,}\b", c.subject):
        c.findings.append(Finding(
            "CL006", "low", "Subject starts with a capital letter",
            "Conventional Commits subjects are conventionally lowercase "
            "(acronyms excepted).",
            f"Use `{c.subject[0].lower() + c.subject[1:]}`.", sha, header))
    if c.subject.endswith("."):
        c.findings.append(Finding(
            "CL007", "low", "Subject ends with a period",
            "The subject line is a title, not a sentence.",
            f"Drop the trailing period: `{c.subject.rstrip('.')}`.", sha, header))
    if NON_IMPERATIVE.match(c.subject):
        first = c.subject.split()[0]
        c.findings.append(Finding(
            "CL008", "low", "Subject is not in the imperative mood",
            f"`{first}` is past tense or a gerund. The convention reads as a "
            "command completing \"this commit will …\".",
            "Use the imperative: \"add\" not \"added/adds\", \"fix\" not "
            "\"fixed/fixes\".", sha, header))
    if len(header) > HEADER_MAX:
        c.findings.append(Finding(
            "CL009", "low", f"Header longer than {HEADER_MAX} characters",
            f"The header is {len(header)} characters, so it will be truncated in "
            "`git log --oneline`, GitHub lists, and many tools.",
            f"Tighten it to <= {HEADER_MAX} characters and move detail to the body.",
            sha, header))

    # Body checks
    if len(lines) > 1:
        if lines[1].strip():
            c.findings.append(Finding(
                "CL010", "medium", "No blank line between header and body",
                "Git treats the first paragraph as the subject; without a blank "
                "line the body is folded into it.",
                "Insert an empty line after the header.", sha, header))
        body_lines = lines[2:] if len(lines) > 2 else []
        c.body = "\n".join(lines[1:]).strip()
        for bl in body_lines:
            if len(bl) > BODY_MAX and not re.match(r"^\s*(https?://|\S+@|\|)", bl.strip()):
                c.findings.append(Finding(
                    "CL011", "low", f"Body line longer than {BODY_MAX} characters",
                    "Long lines are hard to read in terminals that don't soft-wrap.",
                    f"Wrap body text at {BODY_MAX} characters.", sha, header))
                break

    # BREAKING CHANGE consistency
    bc = re.search(r"^BREAKING[ -]CHANGE(S)?:\s*(.+)$", c.raw, re.MULTILINE)
    if bc:
        c.breaking = True
        c.breaking_desc = bc.group(2).strip()
    elif c.breaking:
        c.breaking_desc = c.subject
    if bc and not m.group("bang"):
        c.findings.append(Finding(
            "CL012", "low", "BREAKING CHANGE footer without `!` in the header",
            "The footer marks a breaking change but the header doesn't, so tools "
            "scanning only headers (and humans skimming `git log`) will miss it.",
            f"Add `!` before the colon: `{c.type}"
            f"{'(' + c.scope + ')' if c.scope else ''}!: {c.subject}`.", sha, header))

    return c


def lint_commits(commits: list[Commit]) -> list[Finding]:
    out: list[Finding] = []
    for c in commits:
        out.extend(c.findings)
    out.sort(key=lambda x: x.sort_key())
    return out


# --------------------------------------------------------------------------- #
# Git access
# --------------------------------------------------------------------------- #

SEP = "\x1e"  # record separator


def git_commits(rev_range: str) -> list[Commit]:
    proc = subprocess.run(
        ["git", "log", f"--format=%H%x1f%B{SEP}", rev_range],
        text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git log failed")
    commits: list[Commit] = []
    for record in proc.stdout.split(SEP):
        record = record.strip("\n")
        if not record.strip():
            continue
        sha, _, body = record.partition("\x1f")
        commits.append(parse_commit(body.strip("\n"), sha.strip()[:12]))
    return commits


# --------------------------------------------------------------------------- #
# Changelog
# --------------------------------------------------------------------------- #


def build_changelog(commits: list[Commit], version: str = "", date: str = "") -> str:
    breaking: list[Commit] = []
    groups: dict[str, list[Commit]] = {}
    other: list[Commit] = []

    for c in commits:
        if c.skipped:
            continue
        if c.breaking:
            breaking.append(c)
        if c.valid:
            groups.setdefault(c.type.lower(), []).append(c)
        else:
            other.append(c)

    heading = f"## {version}" if version else "## Unreleased"
    if date:
        heading += f" — {date}"
    lines = [heading, ""]

    if breaking:
        lines.append("### ⚠ BREAKING CHANGES")
        lines.append("")
        for c in breaking:
            desc = c.breaking_desc or c.subject
            scope = f"**{c.scope}:** " if c.scope else ""
            lines.append(f"- {scope}{desc}" + (f" ({c.sha})" if c.sha else ""))
        lines.append("")

    for t in CHANGELOG_ORDER:
        if t not in groups:
            continue
        lines.append(f"### {DEFAULT_TYPES.get(t, t.title())}")
        lines.append("")
        for c in groups[t]:
            scope = f"**{c.scope}:** " if c.scope else ""
            bang = " ⚠" if c.breaking else ""
            lines.append(f"- {scope}{c.subject}{bang}" + (f" ({c.sha})" if c.sha else ""))
        lines.append("")

    # Any non-conventional types that aren't in the known order.
    for t in sorted(set(groups) - set(CHANGELOG_ORDER)):
        lines.append(f"### {t.title()}")
        lines.append("")
        for c in groups[t]:
            scope = f"**{c.scope}:** " if c.scope else ""
            lines.append(f"- {scope}{c.subject}" + (f" ({c.sha})" if c.sha else ""))
        lines.append("")

    if other:
        lines.append("### Other")
        lines.append("")
        for c in other:
            lines.append(f"- {c.header}" + (f" ({c.sha})" if c.sha else ""))
        lines.append("")

    if len(lines) <= 2:
        lines.append("_No changes._")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def render_text(findings: list[Finding], threshold: str, n_commits: int) -> str:
    if not findings:
        return f"commit-lint: {n_commits} commit(s) checked, all conform. ✓"
    counts: dict[str, int] = {}
    for x in findings:
        counts[x.severity] = counts.get(x.severity, 0) + 1
    summary = ", ".join(f"{counts[s]} {s}" for s in
                        ["critical", "high", "medium", "low", "info"] if counts.get(s))
    out = [f"commit-lint found {len(findings)} issue(s) in {n_commits} commit(s): "
           f"{summary}\n"]
    for x in findings:
        out.append(f"[{SEVERITY_LABEL[x.severity]}] {x.rule}  {x.title}")
        if x.commit or x.header:
            loc = f"{x.commit} " if x.commit else ""
            out.append(f"  at {loc}{x.header!r}")
        out.append(f"  why: {x.detail}")
        out.append(f"  fix: {x.fix}")
        out.append("")
    gate = SEVERITY_ORDER[threshold]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    out.append(f"FAIL: findings at or above '{threshold}'." if worst >= gate
               else f"OK: no findings at or above '{threshold}' (advisory items only).")
    return "\n".join(out)


def render_json(findings: list[Finding], threshold: str, n_commits: int) -> str:
    gate = SEVERITY_ORDER[threshold]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    return json.dumps({
        "tool": "commit-lint",
        "threshold": threshold,
        "passed": worst < gate,
        "commits": n_commits,
        "count": len(findings),
        "findings": [asdict(x) for x in findings],
    }, indent=2)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

SELFTEST_POSITIVE = [
    ("update the readme", "CL002"),          # no type prefix
    ("feat(api):add thing", "CL002"),        # missing space after colon
    ("Feat(api): add thing", "CL003"),       # non-lowercase type
    ("wibble: do a thing", "CL004"),         # unknown type
    ("feat(): add thing", "CL005"),          # empty scope
    ("feat: Add thing", "CL006"),            # capitalized subject
    ("feat: add thing.", "CL007"),           # trailing period
    ("feat: added thing", "CL008"),          # non-imperative
    ("feat: " + "x" * 90, "CL009"),          # over-long header
    ("feat: add thing\nbody with no blank line", "CL010"),
    ("feat: add thing\n\n" + "y" * 120, "CL011"),
    ("feat: add thing\n\nBREAKING CHANGE: drops v1 API", "CL012"),
    ("", "CL001"),
]

SELFTEST_NEGATIVE = [
    "feat(api): add cursor pagination",
    "fix: handle empty payload",
    "docs: clarify install steps",
    "feat(api)!: drop v1 endpoints\n\nBREAKING CHANGE: v1 endpoints are removed",
    "chore(deps): bump urllib3 to 2.2.2",
    "fix: return 404 for unknown IDs\n\nPreviously a missing ID raised a 500.",
    "Merge pull request #42 from foo/bar",   # exempt
    'Revert "feat: add thing"',              # exempt
    "refactor: extract HTTP client",
    "feat: add API rate limiting",           # 'API' acronym must not trip CL006
]


def run_selftest() -> int:
    failures = 0
    for msg, expected in SELFTEST_POSITIVE:
        rules = {f.rule for f in parse_commit(msg).findings}
        if expected not in rules:
            failures += 1
            print(f"  MISS: expected {expected} for {msg!r} -> {sorted(rules)}")
    for msg in SELFTEST_NEGATIVE:
        got = [f.rule for f in parse_commit(msg).findings]
        if got:
            failures += 1
            print(f"  FALSE POSITIVE: {got} for {msg!r}")

    # Parser field extraction
    c = parse_commit("feat(auth)!: add SSO\n\nBREAKING CHANGE: removes basic auth")
    if not (c.type == "feat" and c.scope == "auth" and c.subject == "add SSO"
            and c.breaking and c.breaking_desc == "removes basic auth"):
        failures += 1
        print(f"  parse fields wrong: {c.type=} {c.scope=} {c.subject=} "
              f"{c.breaking=} {c.breaking_desc=}")

    # Changelog grouping
    commits = [
        parse_commit("feat(api): add pagination", "aaa1111"),
        parse_commit("fix(ui): correct button alignment", "bbb2222"),
        parse_commit("feat!: drop node 16\n\nBREAKING CHANGE: node 18+ required",
                     "ccc3333"),
        parse_commit("chore: tidy imports", "ddd4444"),
        parse_commit("Merge pull request #9", "eee5555"),
    ]
    cl = build_changelog(commits, version="1.4.0", date="2026-07-24")
    checks = [
        ("## 1.4.0 — 2026-07-24" in cl, "version heading"),
        ("### ⚠ BREAKING CHANGES" in cl, "breaking section"),
        ("node 18+ required" in cl, "breaking description"),
        ("### Features" in cl and "**api:** add pagination" in cl, "features group"),
        ("### Bug Fixes" in cl and "**ui:** correct button alignment" in cl, "fixes group"),
        ("Merge pull request" not in cl, "merge commit excluded"),
        (cl.index("### ⚠ BREAKING CHANGES") < cl.index("### Features"),
         "breaking listed first"),
    ]
    for ok, label in checks:
        if not ok:
            failures += 1
            print(f"  changelog: {label} failed")

    if failures:
        print(f"selftest: {failures} failure(s)")
        return 1
    print(f"selftest: all {len(SELFTEST_POSITIVE)} positive, "
          f"{len(SELFTEST_NEGATIVE)} negative, parser and changelog cases passed ✓")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="commit_lint.py",
        description="Conventional Commits linter and changelog generator.")
    p.add_argument("mode", nargs="?", choices=["lint", "changelog"],
                   help="lint commit messages, or generate a changelog")
    p.add_argument("--message", "-m", help="lint a single message string")
    p.add_argument("--file", "-F", help="lint a message file (e.g. COMMIT_EDITMSG)")
    p.add_argument("--range", "-r", dest="rev_range",
                   help="git revision range (e.g. origin/main..HEAD, v1.0.0..HEAD)")
    p.add_argument("--types", help="comma-separated allowed types (overrides default)")
    p.add_argument("--version", dest="version", default="",
                   help="changelog: version heading")
    p.add_argument("--date", default="", help="changelog: release date")
    p.add_argument("--json", action="store_true", help="lint: emit JSON output")
    p.add_argument("--fail-on", default="high", choices=list(SEVERITY_ORDER),
                   help="minimum severity that causes a non-zero exit (default: high)")
    p.add_argument("--selftest", action="store_true", help="run built-in checks")
    args = p.parse_args(argv)

    if args.selftest:
        return run_selftest()
    if not args.mode:
        p.print_usage()
        print("error: choose a mode: `lint` or `changelog`", file=sys.stderr)
        return 2

    allowed = None
    if args.types:
        allowed = {t.strip().lower() for t in args.types.split(",") if t.strip()}

    commits: list[Commit] = []
    if args.message is not None:
        commits = [parse_commit(args.message, allowed_types=allowed)]
    elif args.file:
        try:
            with open(args.file, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except FileNotFoundError:
            print(f"error: file not found: {args.file}", file=sys.stderr)
            return 2
        # Strip comment lines git adds to the editor template.
        raw = "\n".join(l for l in raw.split("\n") if not l.startswith("#"))
        commits = [parse_commit(raw.strip(), allowed_types=allowed)]
    elif args.rev_range:
        try:
            commits = git_commits(args.rev_range)
        except (RuntimeError, FileNotFoundError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if allowed is not None:
            commits = [parse_commit(c.raw, c.sha, allowed_types=allowed)
                       for c in commits]
    else:
        p.print_usage()
        print("error: provide --message, --file, or --range", file=sys.stderr)
        return 2

    if args.mode == "changelog":
        print(build_changelog(commits, args.version, args.date), end="")
        return 0

    findings = lint_commits(commits)
    n = len(commits)
    print(render_json(findings, args.fail_on, n) if args.json
          else render_text(findings, args.fail_on, n))
    gate = SEVERITY_ORDER[args.fail_on]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    return 1 if worst >= gate else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

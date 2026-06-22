#!/usr/bin/env python3
"""env-doctor: reconcile .env, code usage, and .env.example for a project.

Cross-checks three sources of truth about environment configuration:
  * the variables your code actually reads (os.environ / process.env / getenv …),
  * the variables defined in your real `.env` file(s), and
  * the variables documented in `.env.example` / `.env.sample`,
and flags the gaps that break onboarding or leak secrets: a `.env` that isn't
git-ignored, required vars missing from the example, undocumented or dead vars,
placeholder values left in a real `.env`, and real secrets committed to the
example.

Design goals:
  * Pure Python standard library. No third-party deps, no network access.
  * Deterministic: the same input always produces the same findings.
  * CI-friendly: a non-zero exit code when findings meet a severity threshold.

Usage:
    analyze_env.py [DIR]                 # analyze a project root (default: .)
    analyze_env.py --json DIR            # machine-readable output
    analyze_env.py --fail-on medium DIR  # gate CI at a threshold
    analyze_env.py --selftest            # run built-in checks

Exit codes:
    0  no findings at or above the --fail-on threshold (default: high)
    1  one or more findings at or above the threshold
    2  usage / runtime error
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass, asdict, field
from typing import Iterable

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SEVERITY_LABEL = {k: k.upper() for k in SEVERITY_ORDER}

SOURCE_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go",
               ".rb", ".php", ".java", ".rs")

# Variables that conventionally come from the OS/runtime, not your .env.
WELL_KNOWN = {
    "NODE_ENV", "PORT", "HOME", "PATH", "PWD", "USER", "LANG", "LC_ALL",
    "TZ", "CI", "DEBUG", "PYTHONPATH", "HOSTNAME", "SHELL", "TERM", "TMPDIR",
    "RAILS_ENV", "FLASK_ENV", "GO_ENV", "ENV", "ENVIRONMENT",
}

USAGE_PATTERNS = [
    re.compile(r"""os\.environ\s*\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""),
    re.compile(r"""os\.environ\.get\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
    re.compile(r"""os\.getenv\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
    re.compile(r"""os\.Getenv\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
    re.compile(r"""process\.env\.([A-Za-z_][A-Za-z0-9_]*)"""),
    re.compile(r"""process\.env\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""),
    re.compile(r"""import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)"""),
    re.compile(r"""Deno\.env\.get\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
    re.compile(r"""\bgetenv\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
    re.compile(r"""ENV\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""),  # Ruby
]

SECRET_NAME = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"credential|client[_-]?secret|auth|dsn|connection[_-]?string)",
    re.IGNORECASE,
)
PLACEHOLDER_VALUE = re.compile(
    r"^\s*(?:|x{2,}|changeme|change[-_ ]?me|your[-_ ]?[\w-]*|<[^>]*>|placeholder|"
    r"example|sample|todo|tbd|none|null|n/?a|\*{2,}|\.{3,}|\$\{[^}]*\}|"
    r"[\w-]*here|[\w-]*goes[-_]here)\s*$",
    re.IGNORECASE,
)
SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{12,}|ghp_[A-Za-z0-9]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|[A-Za-z0-9+/]{32,}={0,2}|[0-9a-f]{32,})"
)


@dataclass
class Finding:
    rule: str
    severity: str
    title: str
    detail: str
    fix: str
    file: str = ""
    line: int = 0
    var: str = ""

    def sort_key(self) -> tuple:
        return (-SEVERITY_ORDER[self.severity], self.file, self.line, self.rule, self.var)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_env(text: str) -> dict[str, tuple[str, int]]:
    """Return {KEY: (value, line)} for a dotenv-style file."""
    out: dict[str, tuple[str, int]] = {}
    for i, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        key = m.group(1)
        val = m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = (val, i)
    return out


def find_usages(text: str) -> set[str]:
    found: set[str] = set()
    for pat in USAGE_PATTERNS:
        for m in pat.finditer(text):
            found.add(m.group(1))
    return found


def gitignore_ignores(patterns: list[str], filename: str) -> bool:
    base = os.path.basename(filename)
    for p in patterns:
        p = p.strip()
        if not p or p.startswith("#"):
            continue
        p = p.rstrip("/")
        cand = p[1:] if p.startswith("/") else p
        if cand == base or fnmatch.fnmatch(base, cand):
            return True
        # patterns like `.env*` or `*.env`
        if fnmatch.fnmatch(base, p):
            return True
    return False


def is_example_name(base: str) -> bool:
    b = base.lower()
    return any(t in b for t in ("example", "sample", "template", "dist", "default"))


def is_env_name(base: str) -> bool:
    b = base.lower()
    return b == ".env" or b.startswith(".env.") or b.endswith(".env")


# --------------------------------------------------------------------------- #
# Reconciliation (pure — unit-tested by --selftest)
# --------------------------------------------------------------------------- #


@dataclass
class Project:
    root: str = "."
    code_usage: dict[str, tuple[str, int]] = field(default_factory=dict)  # var -> (file,line)
    env_files: dict[str, dict[str, tuple[str, int]]] = field(default_factory=dict)
    example_files: dict[str, dict[str, tuple[str, int]]] = field(default_factory=dict)
    gitignore: list[str] = field(default_factory=list)


def reconcile(proj: Project) -> list[Finding]:
    findings: list[Finding] = []

    # Merge example keys across all example files.
    example_keys: dict[str, tuple[str, str, int]] = {}  # key -> (file, value, line)
    for ef, kv in proj.example_files.items():
        for k, (v, ln) in kv.items():
            example_keys.setdefault(k, (ef, v, ln))

    # ENV001: real .env not git-ignored.
    for ef in proj.env_files:
        if not gitignore_ignores(proj.gitignore, ef):
            findings.append(Finding(
                "ENV001", "high", ".env file is not git-ignored",
                f"`{os.path.basename(ef)}` holds real configuration/secrets but is not "
                "matched by .gitignore — one `git add .` away from committing secrets.",
                "Add `.env` (and `.env.*`, keeping `!.env.example`) to .gitignore, and "
                "scrub it from history if it was ever committed.",
                file=ef, line=0))

    # ENV005 / placeholder + ENV004 undocumented, over real .env entries.
    for ef, kv in proj.env_files.items():
        for key, (val, ln) in kv.items():
            if PLACEHOLDER_VALUE.match(val):
                findings.append(Finding(
                    "ENV005", "high", "Placeholder/empty value in a real .env",
                    f"`{key}` in `{os.path.basename(ef)}` still has a placeholder/empty "
                    "value — the app will start with a broken or missing setting.",
                    "Set a real value, or remove the line if the variable is unused.",
                    file=ef, line=ln, var=key))
            if key not in example_keys and proj.example_files:
                findings.append(Finding(
                    "ENV004", "medium", "Variable in .env is undocumented",
                    f"`{key}` is set in `{os.path.basename(ef)}` but absent from the "
                    "example file — new contributors won't know it exists.",
                    f"Add `{key}=` (with a placeholder) to your .env.example.",
                    file=ef, line=ln, var=key))

    # ENV006: real secret committed to an example file.
    for ef, kv in proj.example_files.items():
        for key, (val, ln) in kv.items():
            if val and not PLACEHOLDER_VALUE.match(val) and \
                    (SECRET_VALUE.search(val) or
                     (SECRET_NAME.search(key) and len(val) >= 8)):
                findings.append(Finding(
                    "ENV006", "high", "Real secret committed in the example file",
                    f"`{key}` in `{os.path.basename(ef)}` looks like a real value, not a "
                    "placeholder. Example files are committed — this leaks the secret.",
                    "Replace the value with a placeholder (e.g. `your-key-here`) and "
                    "rotate the exposed secret.",
                    file=ef, line=ln, var=key))

    # ENV002: used in code but missing from the example (and not well-known).
    for var, (cf, cl) in sorted(proj.code_usage.items()):
        if var in WELL_KNOWN:
            continue
        if proj.example_files and var not in example_keys:
            findings.append(Finding(
                "ENV002", "medium", "Required variable missing from .env.example",
                f"`{var}` is read by the code but not listed in any example file — a "
                "fresh checkout starts misconfigured and fails at runtime.",
                f"Add `{var}=` to your .env.example so onboarding is self-documenting.",
                file=cf, line=cl, var=var))

    # ENV003: documented in the example but never used in code.
    used = set(proj.code_usage)
    for var, (ef, _v, ln) in sorted(example_keys.items()):
        if var not in used and var not in WELL_KNOWN:
            findings.append(Finding(
                "ENV003", "low", "Example variable is never used in code",
                f"`{var}` is in `{os.path.basename(ef)}` but no source file reads it — "
                "likely stale documentation.",
                f"Remove `{var}` from the example, or wire it up if it's actually needed.",
                file=ef, line=ln, var=var))

    findings.sort(key=lambda x: x.sort_key())
    return findings


# --------------------------------------------------------------------------- #
# Disk walking
# --------------------------------------------------------------------------- #


def load_project(root: str) -> Project:
    proj = Project(root=root)
    skip = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist",
            "build", ".next", "vendor", "target"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if name == ".gitignore" and os.path.dirname(full) == root:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    proj.gitignore = fh.read().split("\n")
                continue
            if is_env_name(name):
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    parsed = parse_env(fh.read())
                if is_example_name(name):
                    proj.example_files[full] = parsed
                else:
                    proj.env_files[full] = parsed
                continue
            if name.endswith(SOURCE_EXTS):
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                for var in find_usages(text):
                    if var not in proj.code_usage:
                        # record first usage location with a line number
                        for i, line in enumerate(text.split("\n"), start=1):
                            if var in line:
                                proj.code_usage[var] = (full, i)
                                break
                        else:
                            proj.code_usage[var] = (full, 0)
    return proj


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def render_text(findings: list[Finding], threshold: str) -> str:
    if not findings:
        return "env-doctor: configuration is consistent. ✓"
    counts: dict[str, int] = {}
    for x in findings:
        counts[x.severity] = counts.get(x.severity, 0) + 1
    summary = ", ".join(f"{counts[s]} {s}" for s in
                        ["critical", "high", "medium", "low", "info"] if counts.get(s))
    lines = [f"env-doctor found {len(findings)} issue(s): {summary}\n"]
    for x in findings:
        loc = f"{x.file}:{x.line}" if x.line else x.file
        lines.append(f"[{SEVERITY_LABEL[x.severity]}] {x.rule}  {x.title}")
        lines.append(f"  at {loc}" + (f"  ({x.var})" if x.var else ""))
        lines.append(f"  why: {x.detail}")
        lines.append(f"  fix: {x.fix}")
        lines.append("")
    gate = SEVERITY_ORDER[threshold]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    lines.append(f"FAIL: findings at or above '{threshold}'." if worst >= gate
                 else f"OK: no findings at or above '{threshold}' (advisory items only).")
    return "\n".join(lines)


def render_json(findings: list[Finding], threshold: str) -> str:
    gate = SEVERITY_ORDER[threshold]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    return json.dumps({
        "tool": "env-doctor",
        "threshold": threshold,
        "passed": worst < gate,
        "count": len(findings),
        "findings": [asdict(x) for x in findings],
    }, indent=2)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #


def run_selftest() -> int:
    failures = 0

    def expect(proj: Project, rule: str, present: bool, label: str):
        nonlocal failures
        rules = {f.rule for f in reconcile(proj)}
        if (rule in rules) != present:
            failures += 1
            print(f"  {label}: expected {rule} present={present}, got {sorted(rules)}")

    # ENV001: .env not ignored
    expect(Project(env_files={".env": {"A": ("1", 1)}}, gitignore=[]),
           "ENV001", True, "env not ignored")
    expect(Project(env_files={".env": {"A": ("1", 1)}}, gitignore=[".env"]),
           "ENV001", False, "env ignored")
    expect(Project(env_files={".env": {"A": ("1", 1)}}, gitignore=[".env*"]),
           "ENV001", False, "env ignored by glob")

    # ENV002: used in code, missing from example
    expect(Project(code_usage={"API_URL": ("a.py", 1)},
                   example_files={".env.example": {"OTHER": ("x", 1)}},
                   gitignore=[".env"]),
           "ENV002", True, "required missing from example")

    # ENV003: example var unused
    expect(Project(example_files={".env.example": {"UNUSED": ("x", 1)}}),
           "ENV003", True, "example unused")

    # ENV004: in .env, not in example
    expect(Project(env_files={".env": {"EXTRA": ("v", 1)}},
                   example_files={".env.example": {"A": ("x", 1)}},
                   gitignore=[".env"]),
           "ENV004", True, "undocumented env var")

    # ENV005: placeholder in real .env
    expect(Project(env_files={".env": {"DB_PASSWORD": ("changeme", 1)}},
                   gitignore=[".env"]),
           "ENV005", True, "placeholder in env")

    # ENV006: real secret in example
    expect(Project(example_files={".env.example":
                                  {"API_KEY": ("sk-ant-realtoken1234567890", 1)}}),
           "ENV006", True, "secret in example")
    expect(Project(example_files={".env.example": {"API_KEY": ("your-key-here", 1)}}),
           "ENV006", False, "placeholder in example ok")

    # parse_env sanity
    pe = parse_env('export FOO="bar"\n# c\nBAZ=qux\n')
    if pe.get("FOO", (None,))[0] != "bar" or "BAZ" not in pe:
        failures += 1
        print(f"  parse_env: unexpected {pe}")

    # find_usages sanity
    u = find_usages('os.getenv("A"); process.env.B; import.meta.env.C')
    if u != {"A", "B", "C"}:
        failures += 1
        print(f"  find_usages: unexpected {u}")

    if failures:
        print(f"selftest: {failures} failure(s)")
        return 1
    print("selftest: all reconciliation, parser, and usage cases passed ✓")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="analyze_env.py",
        description="Reconcile .env, code usage, and .env.example for a project.")
    p.add_argument("paths", nargs="*", default=["."],
                   help="project root directories (default: current directory)")
    p.add_argument("--json", action="store_true", help="emit JSON output")
    p.add_argument("--fail-on", default="high", choices=list(SEVERITY_ORDER),
                   help="minimum severity that causes a non-zero exit (default: high)")
    p.add_argument("--selftest", action="store_true", help="run built-in checks")
    args = p.parse_args(argv)

    if args.selftest:
        return run_selftest()

    roots = args.paths or ["."]
    findings: list[Finding] = []
    for root in roots:
        if not os.path.exists(root):
            print(f"error: path not found: {root}", file=sys.stderr)
            return 2
        if os.path.isfile(root):
            root = os.path.dirname(root) or "."
        findings.extend(reconcile(load_project(root)))
    findings.sort(key=lambda x: x.sort_key())

    print(render_json(findings, args.fail_on) if args.json
          else render_text(findings, args.fail_on))
    gate = SEVERITY_ORDER[args.fail_on]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    return 1 if worst >= gate else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

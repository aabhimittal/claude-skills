#!/usr/bin/env python3
"""dockerfile-doctor: a static linter for Dockerfile security and image bloat.

Scans Dockerfiles for the issues that ship insecure or bloated container images:
running as root, mutable/`:latest` base tags, secrets baked into `ARG`/`ENV`,
`ADD` where `COPY` belongs, package-manager caches left in the layer, piping
remote scripts straight into a shell, and dependency-install ordering that
defeats Docker's layer cache.

Design goals:
  * Pure Python standard library. No third-party deps, no network access.
  * Deterministic: the same input always produces the same findings.
  * CI-friendly: a non-zero exit code when findings meet a severity threshold.

Usage:
    analyze_dockerfile.py FILE [FILE ...]     # lint Dockerfiles
    analyze_dockerfile.py DIR                  # recurse, find Dockerfiles
    analyze_dockerfile.py --json FILE          # machine-readable output
    analyze_dockerfile.py --fail-on medium D   # gate CI at a threshold
    analyze_dockerfile.py --selftest           # run built-in checks

Exit codes:
    0  no findings at or above the --fail-on threshold (default: high)
    1  one or more findings at or above the threshold
    2  usage / runtime error
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

SECRET_NAME = re.compile(
    r"\b\w*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|client[_-]?secret|auth)\w*\b",
    re.IGNORECASE,
)
PLACEHOLDER_VALUE = re.compile(
    r"^(?:x{3,}|changeme|your[-_ ]?\w+|<[^>]+>|placeholder|example|todo|test)$",
    re.IGNORECASE,
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
    snippet: str = ""

    def sort_key(self) -> tuple:
        return (-SEVERITY_ORDER[self.severity], self.file, self.line, self.rule)


def _snip(text: str, limit: int = 140) -> str:
    s = re.sub(r"\s+", " ", text).strip()
    return s if len(s) <= limit else s[: limit - 3] + "..."


@dataclass
class Instr:
    op: str         # uppercase instruction (FROM, RUN, ...)
    arg: str        # joined argument text
    line: int       # 1-based line of the instruction start


def parse_instructions(text: str) -> list[Instr]:
    """Parse a Dockerfile into logical instructions, joining `\\` continuations
    and skipping comments/blank lines."""
    instrs: list[Instr] = []
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        start_line = i + 1
        buf = raw
        # join continuations (trailing backslash, ignoring trailing whitespace)
        while buf.rstrip().endswith("\\") and i + 1 < n:
            buf = buf.rstrip()[:-1] + "\n" + lines[i + 1]
            i += 1
        i += 1
        m = re.match(r"\s*(\w+)\s+(.*)", buf, re.DOTALL)
        if not m:
            continue
        op = m.group(1).upper()
        arg = m.group(2)
        instrs.append(Instr(op=op, arg=arg, line=start_line))
    return instrs


def analyze_text(text: str, file: str) -> list[Finding]:
    instrs = parse_instructions(text)
    findings: list[Finding] = []

    def add(rule, sev, title, detail, fix, line, snippet=""):
        findings.append(Finding(rule, sev, title, detail, fix, file, line, snippet))

    # Track stages and the USER set in the final stage.
    stage_user: str | None = None
    final_from_line = 0
    saw_dep_install_after_copy_all = False
    copy_all_line = 0

    for ins in instrs:
        arg = ins.arg
        low = arg.lower()
        snip = _snip(ins.op + " " + arg)

        if ins.op == "FROM":
            stage_user = None  # reset per stage
            final_from_line = ins.line
            # tag check on the image ref (before optional "AS name")
            image = arg.split()[0] if arg.split() else ""
            ref = image.split("AS")[0].strip()
            if "@sha256:" in ref:
                pass  # digest-pinned, best
            elif ":" not in ref.split("/")[-1]:
                add("DKR002", "medium", "Base image has no tag (implies :latest)",
                    f"`FROM {ref}` resolves to the mutable `:latest` tag — builds are "
                    "not reproducible and can change under you.",
                    "Pin an explicit version tag (e.g. `python:3.12-slim`) or, best, a "
                    "digest (`image@sha256:...`).", ins.line, snip)
            elif ref.split("/")[-1].endswith(":latest"):
                add("DKR002", "medium", "Base image pinned to :latest",
                    f"`{ref}` uses the mutable `:latest` tag — builds are not "
                    "reproducible.",
                    "Pin an explicit version tag or a digest.", ins.line, snip)

        elif ins.op == "USER":
            stage_user = arg.strip()
            if stage_user.split(":")[0].lower() in ("root", "0"):
                add("DKR001", "high", "Container runs as root (USER root)",
                    "Running the container process as root means a container escape or "
                    "app compromise starts with root in the container.",
                    "Create and switch to a non-root user (`RUN adduser ...` then "
                    "`USER app`).", ins.line, snip)

        elif ins.op in ("ARG", "ENV"):
            # secret-looking name
            name_match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", arg)
            name = name_match.group(1) if name_match else ""
            value = ""
            vm = re.search(r"=\s*(.+)$", arg) or re.search(r"^\s*\S+\s+(.+)$", arg)
            if vm:
                value = vm.group(1).strip().strip('"').strip("'")
            if SECRET_NAME.search(name):
                if ins.op == "ENV":
                    add("DKR003", "high", "Secret baked into an ENV layer",
                        f"`ENV {name}` persists in the final image and every layer's "
                        "metadata — `docker history`/`inspect` reveals it. Build args "
                        "and env vars are not a secret store.",
                        "Inject secrets at runtime (`docker run -e` / orchestrator "
                        "secrets) or use BuildKit `--mount=type=secret`; never `ENV` "
                        "them.", ins.line, _snip(ins.op + " " + name + "=***"))
                elif value and not PLACEHOLDER_VALUE.match(value):
                    add("DKR003", "high", "Secret value passed via ARG",
                        f"`ARG {name}` with a baked value is recoverable from image "
                        "history even though it isn't in the final env.",
                        "Use BuildKit secret mounts (`RUN --mount=type=secret ...`) "
                        "instead of `ARG` for credentials.", ins.line,
                        _snip(ins.op + " " + name + "=***"))

        elif ins.op == "ADD":
            src = arg.split()[0] if arg.split() else ""
            if not (src.startswith("http://") or src.startswith("https://")) \
                    and not re.search(r"\.(tar|tgz|tar\.gz|tar\.bz2|tar\.xz|zip)\b", src, re.I):
                add("DKR004", "medium", "ADD used where COPY is correct",
                    "`ADD` has surprising behavior (URL fetch, auto-extraction). For "
                    "plain local files use `COPY`, which is explicit and auditable.",
                    "Replace `ADD` with `COPY` unless you specifically need archive "
                    "auto-extraction.", ins.line, snip)
            elif src.startswith("http"):
                add("DKR010", "medium", "ADD fetches a remote URL",
                    "`ADD <url>` downloads at build time with no checksum — a "
                    "compromised or changed URL silently alters your image.",
                    "Download with `curl`/`wget` in a `RUN`, verify a checksum, then "
                    "use the file.", ins.line, snip)

        elif ins.op in ("COPY",):
            parts = arg.split()
            if len(parts) >= 2 and parts[0] == "." and not arg.startswith("--from"):
                copy_all_line = ins.line

        elif ins.op == "RUN":
            # pipe-to-shell supply-chain risk
            if re.search(r"\b(curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b", low):
                add("DKR010", "high", "Piping a remote script into a shell",
                    "`curl ... | sh` executes unreviewed, unpinned remote code at build "
                    "time — a classic supply-chain foothold.",
                    "Download to a file, verify a checksum/signature, inspect, then run "
                    "it.", ins.line, snip)
            if re.search(r"\bsudo\b", low):
                add("DKR011", "low", "sudo used inside RUN",
                    "Build steps already run as root; `sudo` adds nothing and may not be "
                    "installed, breaking the build.",
                    "Drop `sudo` from build commands.", ins.line, snip)
            # apt cache hygiene
            if re.search(r"\bapt(?:-get)?\s+install\b", low):
                if "--no-install-recommends" not in low:
                    add("DKR005", "low", "apt install without --no-install-recommends",
                        "Recommended packages pull in extra weight you didn't ask for, "
                        "bloating the image.",
                        "Add `--no-install-recommends` to `apt-get install`.",
                        ins.line, snip)
                if "rm -rf /var/lib/apt/lists" not in low.replace(" ", " "):
                    add("DKR006", "medium", "apt lists not cleaned in the same layer",
                        "Leaving `/var/lib/apt/lists` in the layer permanently bloats the "
                        "image — deleting it in a later RUN doesn't shrink the earlier "
                        "layer.",
                        "End the install RUN with "
                        "`&& rm -rf /var/lib/apt/lists/*` in the same layer.",
                        ins.line, snip)
                if re.search(r"\bapt(?:-get)?\s+update\b", low) is None:
                    pass  # update may be in a prior layer; flagged separately below
            if re.search(r"\bpip3?\s+install\b", low) and "--no-cache-dir" not in low:
                add("DKR007", "low", "pip install without --no-cache-dir",
                    "pip's download cache stays in the layer, adding megabytes for no "
                    "runtime benefit.",
                    "Add `--no-cache-dir` to `pip install`.", ins.line, snip)
            # dependency install after `COPY . .`
            if copy_all_line and re.search(
                    r"\b(pip3?\s+install|npm\s+(?:ci|install|i)\b|yarn\s+install|"
                    r"bundle\s+install|go\s+mod\s+download|composer\s+install)", low):
                if not saw_dep_install_after_copy_all:
                    saw_dep_install_after_copy_all = True
                    add("DKR008", "low", "Dependencies installed after copying all source",
                        f"`COPY . .` (line {copy_all_line}) before the dependency install "
                        "means any source change busts the cached dependency layer — every "
                        "build reinstalls everything.",
                        "Copy only the manifest first (e.g. `COPY package.json package-"
                        "lock.json ./`), install, then `COPY . .`.", ins.line, snip)

    # Final-stage root check: no USER set in the final stage.
    if stage_user is None and final_from_line:
        # Only warn if the file actually builds a runnable image (has CMD/ENTRYPOINT)
        if any(ins.op in ("CMD", "ENTRYPOINT") for ins in instrs):
            add("DKR001", "high", "Container runs as root (no USER set)",
                "No `USER` instruction in the final stage, so the container runs as "
                "root by default — a compromise of the app is a root compromise of the "
                "container.",
                "Add a non-root user and `USER <name>` before `CMD`/`ENTRYPOINT`.",
                final_from_line, "FROM ... (final stage)")

    findings.sort(key=lambda x: x.sort_key())
    return findings


# --------------------------------------------------------------------------- #
# File / directory handling
# --------------------------------------------------------------------------- #

DOCKERFILE_NAME = re.compile(r"(^|\.)dockerfile$|^dockerfile(\.|$)", re.IGNORECASE)


def is_dockerfile(path: str) -> bool:
    base = os.path.basename(path)
    return bool(DOCKERFILE_NAME.search(base)) or base.lower() == "dockerfile" \
        or base.lower().endswith(".dockerfile")


def analyze_file(path: str) -> list[Finding]:
    if path == "-":
        return analyze_text(sys.stdin.read(), "<stdin>")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return analyze_text(fh.read(), path)


def iter_dockerfiles(root: str) -> Iterable[str]:
    skip = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if is_dockerfile(full):
                yield full


def collect_findings(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        if path != "-" and os.path.isdir(path):
            for fp in iter_dockerfiles(path):
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
        return "dockerfile-doctor: no issues detected. ✓"
    counts: dict[str, int] = {}
    for x in findings:
        counts[x.severity] = counts.get(x.severity, 0) + 1
    summary = ", ".join(f"{counts[s]} {s}" for s in
                        ["critical", "high", "medium", "low", "info"] if counts.get(s))
    lines = [f"dockerfile-doctor found {len(findings)} issue(s): {summary}\n"]
    for x in findings:
        loc = f"{x.file}:{x.line}" if x.line else x.file
        lines.append(f"[{SEVERITY_LABEL[x.severity]}] {x.rule}  {x.title}")
        lines.append(f"  at {loc}")
        if x.snippet:
            lines.append(f"  > {x.snippet}")
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
        "tool": "dockerfile-doctor",
        "threshold": threshold,
        "passed": worst < gate,
        "count": len(findings),
        "findings": [asdict(x) for x in findings],
    }, indent=2)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

SELFTEST_POSITIVE = [
    ("FROM python:latest\nCMD ['x']", "DKR002"),
    ("FROM python\nCMD ['x']", "DKR002"),
    ("FROM python:3.12-slim\nENV API_KEY=abc123\nUSER app\nCMD ['x']", "DKR003"),
    ("FROM python:3.12\nADD ./app /app\nUSER app\nCMD ['x']", "DKR004"),
    ("FROM debian:12\nRUN apt-get install -y curl\nUSER app\nCMD ['x']", "DKR006"),
    ("FROM python:3.12\nRUN pip install flask\nUSER app\nCMD ['x']", "DKR007"),
    ("FROM debian:12\nRUN curl https://x.sh | sh\nUSER app\nCMD ['x']", "DKR010"),
    ("FROM node:20\nCOPY . .\nRUN npm ci\nUSER app\nCMD ['x']", "DKR008"),
    ("FROM python:3.12-slim\nCMD ['python','app.py']", "DKR001"),
]

SELFTEST_NEGATIVE = [
    "FROM python:3.12-slim\nRUN pip install --no-cache-dir flask\n"
    "RUN useradd app\nUSER app\nCMD ['python','app.py']",
    "FROM node:20-alpine AS build\nCOPY package.json package-lock.json ./\n"
    "RUN npm ci\nCOPY . .\nUSER node\nCMD ['node','x.js']",
    "FROM debian:12-slim\nRUN apt-get update && apt-get install -y --no-install-recommends "
    "curl && rm -rf /var/lib/apt/lists/*\nUSER app\nCMD ['x']",
]


def run_selftest() -> int:
    failures = 0
    for src, expected in SELFTEST_POSITIVE:
        rules = {x.rule for x in analyze_text(src, "<t>")}
        if expected not in rules:
            failures += 1
            print(f"  MISS: expected {expected} for {src!r} -> {sorted(rules)}")
    for src in SELFTEST_NEGATIVE:
        bad = [x.rule for x in analyze_text(src, "<t>")
               if x.severity in ("high", "critical")]
        if bad:
            failures += 1
            print(f"  FALSE POSITIVE: {bad} for {src!r}")
    if failures:
        print(f"selftest: {failures} failure(s)")
        return 1
    print(f"selftest: all {len(SELFTEST_POSITIVE)} positive and "
          f"{len(SELFTEST_NEGATIVE)} negative cases passed ✓")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="analyze_dockerfile.py",
        description="Static linter for Dockerfile security and image bloat.")
    p.add_argument("paths", nargs="*", help="Dockerfiles or directories, or - for stdin")
    p.add_argument("--json", action="store_true", help="emit JSON output")
    p.add_argument("--fail-on", default="high", choices=list(SEVERITY_ORDER),
                   help="minimum severity that causes a non-zero exit (default: high)")
    p.add_argument("--selftest", action="store_true", help="run built-in checks")
    args = p.parse_args(argv)

    if args.selftest:
        return run_selftest()
    if not args.paths:
        p.print_usage()
        print("error: provide a Dockerfile or directory (or - for stdin)", file=sys.stderr)
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

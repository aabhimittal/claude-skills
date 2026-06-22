#!/usr/bin/env python3
"""regression-finder: a safe driver for `git bisect run`.

Given a known-good ref, a known-bad ref (default HEAD), and a test/repro command,
this drives an automated `git bisect run` to pinpoint the exact commit that
introduced a regression, then prints the culprit and its diff stat.

Safety:
  * Refuses to run on a dirty working tree (bisect checks out commits).
  * Always restores the original branch/commit on exit, even on error/interrupt
    (`git bisect reset`).
  * Optional `--verify` first confirms the command actually passes at <good> and
    fails at <bad> — otherwise a bisect is meaningless.

The test command follows `git bisect run` conventions:
    exit 0          -> commit is GOOD
    exit 1..124,126,127 -> commit is BAD
    exit 125        -> SKIP (cannot be tested, e.g. won't build)

Usage:
    regression_finder.py --good <ref> [--bad <ref>] --cmd "pytest -x tests/foo.py"
    regression_finder.py --good v1.2.0 -- pytest -x tests/foo.py::test_thing
    regression_finder.py --good abc123 --bad HEAD --verify --cmd "./repro.sh"
    regression_finder.py --selftest

Exit codes:
    0  a first-bad commit was identified
    1  bisect ran but could not identify a culprit (skips, or command issue)
    2  usage / precondition error (dirty tree, bad refs, not a git repo)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

FIRST_BAD = re.compile(r"([0-9a-f]{7,40})\s+is the first bad commit", re.IGNORECASE)


def git(*args: str, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], text=True,
                          capture_output=capture, check=check)


def is_git_repo() -> bool:
    r = git("rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def tree_is_clean() -> bool:
    r = git("status", "--porcelain")
    return r.returncode == 0 and r.stdout.strip() == ""


def resolve(ref: str) -> str | None:
    r = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return r.stdout.strip() or None


def current_ref() -> str:
    r = git("symbolic-ref", "--short", "-q", "HEAD")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return git("rev-parse", "HEAD").stdout.strip()


def run_cmd(cmd: list[str]) -> int:
    """Run the user's test command, streaming its output, return exit code."""
    return subprocess.run(cmd).returncode


def extract_first_bad(text: str) -> str | None:
    m = FIRST_BAD.search(text)
    return m.group(1) if m else None


def parse_args(argv: list[str]):
    p = argparse.ArgumentParser(
        prog="regression_finder.py",
        description="Drive `git bisect run` to find the commit that broke a test.")
    p.add_argument("--good", help="known-good ref (commit/tag/branch)")
    p.add_argument("--bad", default="HEAD", help="known-bad ref (default: HEAD)")
    p.add_argument("--cmd", help="test command (run via the shell)")
    p.add_argument("--verify", action="store_true",
                   help="first confirm the command passes at --good and fails at --bad")
    p.add_argument("--selftest", action="store_true", help="run built-in checks")
    p.add_argument("rest", nargs=argparse.REMAINDER,
                   help="command after -- (alternative to --cmd)")
    return p, p.parse_args(argv)


def build_command(args) -> list[str] | None:
    if args.cmd:
        return ["sh", "-c", args.cmd]
    rest = args.rest
    if rest and rest and rest[0] == "--":
        rest = rest[1:]
    if rest:
        return list(rest)
    return None


def run_selftest() -> int:
    failures = 0
    sample = ("Bisecting: 3 revisions left to test after this (roughly 2 steps)\n"
              "abc123def4567890abc123def4567890abc12345 is the first bad commit\n"
              "commit abc123def4567890abc123def4567890abc12345\n")
    sha = extract_first_bad(sample)
    if sha != "abc123def4567890abc123def4567890abc12345":
        failures += 1
        print(f"  extract_first_bad: got {sha!r}")
    if extract_first_bad("no culprit here") is not None:
        failures += 1
        print("  extract_first_bad: false positive")
    # short sha form
    if extract_first_bad("deadbee is the first bad commit") != "deadbee":
        failures += 1
        print("  extract_first_bad: short sha not matched")
    # command building
    _, a = parse_args(["--good", "x", "--", "pytest", "-x"])
    if build_command(a) != ["pytest", "-x"]:
        failures += 1
        print(f"  build_command(--): {build_command(a)}")
    _, a = parse_args(["--good", "x", "--cmd", "npm test"])
    if build_command(a) != ["sh", "-c", "npm test"]:
        failures += 1
        print(f"  build_command(--cmd): {build_command(a)}")
    if failures:
        print(f"selftest: {failures} failure(s)")
        return 1
    print("selftest: all parsing and extraction cases passed ✓")
    return 0


def main(argv: list[str]) -> int:
    parser, args = parse_args(argv)
    if args.selftest:
        return run_selftest()

    if not args.good:
        parser.print_usage()
        print("error: --good <ref> is required", file=sys.stderr)
        return 2
    cmd = build_command(args)
    if not cmd:
        print("error: provide a test command via --cmd or after --", file=sys.stderr)
        return 2

    if not is_git_repo():
        print("error: not inside a git repository", file=sys.stderr)
        return 2
    if not tree_is_clean():
        print("error: working tree is dirty — commit or stash changes first "
              "(bisect checks out commits)", file=sys.stderr)
        return 2

    good = resolve(args.good)
    bad = resolve(args.bad)
    if not good:
        print(f"error: cannot resolve --good ref: {args.good}", file=sys.stderr)
        return 2
    if not bad:
        print(f"error: cannot resolve --bad ref: {args.bad}", file=sys.stderr)
        return 2
    if good == bad:
        print("error: --good and --bad resolve to the same commit", file=sys.stderr)
        return 2

    original = current_ref()
    bisecting = False
    try:
        if args.verify:
            print(f"verify: running command at --good ({args.good}) ...")
            git("checkout", "--quiet", good, check=True)
            good_rc = run_cmd(cmd)
            print(f"verify: running command at --bad ({args.bad}) ...")
            git("checkout", "--quiet", bad, check=True)
            bad_rc = run_cmd(cmd)
            git("checkout", "--quiet", original, check=False)
            if good_rc != 0:
                print(f"error: command FAILED at --good (exit {good_rc}); it must "
                      "PASS there for bisect to be meaningful.", file=sys.stderr)
                return 2
            if bad_rc == 0:
                print(f"error: command PASSED at --bad (exit {bad_rc}); it must FAIL "
                      "there. Is the regression actually present at --bad?",
                      file=sys.stderr)
                return 2
            print("verify: good passes, bad fails — proceeding to bisect.\n")

        git("bisect", "reset", capture=True)  # clear any stale bisect state
        start = git("bisect", "start", bad, good)
        if start.returncode != 0:
            sys.stderr.write(start.stderr)
            return 2
        bisecting = True

        print(f"bisecting between good={good[:12]} and bad={bad[:12]} ...\n")
        run = git("bisect", "run", *cmd, capture=True)
        out = (run.stdout or "") + (run.stderr or "")
        print(out, end="" if out.endswith("\n") else "\n")

        sha = extract_first_bad(out)
        if not sha:
            print("\nregression-finder: could not identify a first-bad commit "
                  "(only skipped commits left, or the command never distinguished "
                  "good from bad). Review the output above.")
            return 1

        full = resolve(sha) or sha
        print("\n" + "=" * 60)
        print("CULPRIT — first commit where the command fails:")
        show = git("show", "--no-patch",
                   "--pretty=format:  %h  %an  %ad%n  %s", "--date=short", full)
        print(show.stdout)
        stat = git("show", "--stat", "--oneline", "--format=", full)
        if stat.stdout.strip():
            print("\nFiles changed:")
            print(stat.stdout.rstrip())
        print("=" * 60)
        print(f"\nInspect the full diff with:  git show {full[:12]}")
        return 0
    finally:
        if bisecting:
            git("bisect", "reset", capture=True)
        else:
            git("checkout", "--quiet", original, capture=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

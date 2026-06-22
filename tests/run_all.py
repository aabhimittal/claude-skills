#!/usr/bin/env python3
"""End-to-end test harness for the claude-skills marketplace.

Proves the whole package works as a prototype, with no third-party deps:

  1. Marketplace structure — marketplace.json + every plugin.json is valid,
     each plugin source dir exists, and each SKILL.md's frontmatter `name`
     matches its folder and has a description.
  2. Every analyzer's built-in `--selftest` passes.
  3. Each analyzer flags its bundled UNSAFE fixture (exit 1, expected rule IDs)
     and passes its SAFE fixture (exit 0).
  4. regression-finder actually bisects a planted bug in a throwaway git repo,
     reports the culprit, exits 0, and restores the original branch.

Run from anywhere:  python3 tests/run_all.py
Exit code 0 = all green, 1 = at least one failure.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

FAILURES: list[str] = []
PASSES = 0


def check(cond: bool, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ok   {msg}")
    else:
        FAILURES.append(msg)
        print(f"  FAIL {msg}")


def run(script: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(script), *args],
                          text=True, capture_output=True)
    return proc.returncode, proc.stdout + proc.stderr


def run_json(script: Path, *args: str) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, str(script), "--json", *args],
                          text=True, capture_output=True)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        data = {}
    return proc.returncode, data


def skill(plugin: str) -> Path:
    return REPO / "plugins" / plugin / "skills" / plugin


# --------------------------------------------------------------------------- #
# 1. Structure
# --------------------------------------------------------------------------- #

def test_structure() -> None:
    print("\n[1] marketplace structure")
    mk_path = REPO / ".claude-plugin" / "marketplace.json"
    check(mk_path.exists(), "marketplace.json exists")
    mk = json.loads(mk_path.read_text())
    check(isinstance(mk.get("plugins"), list) and len(mk["plugins"]) >= 5,
          f"marketplace lists >=5 plugins ({len(mk.get('plugins', []))})")
    check(bool(mk.get("name")) and bool(mk.get("owner")),
          "marketplace has name + owner")

    for entry in mk.get("plugins", []):
        name = entry.get("name", "?")
        src = (REPO / entry["source"]).resolve()
        check(src.is_dir(), f"{name}: source dir exists ({entry['source']})")
        pj = src / ".claude-plugin" / "plugin.json"
        check(pj.exists(), f"{name}: plugin.json exists")
        if pj.exists():
            d = json.loads(pj.read_text())
            check(d.get("name") == name, f"{name}: plugin.json name matches catalog")

        sk = skill(name) / "SKILL.md"
        check(sk.exists(), f"{name}: SKILL.md exists")
        if sk.exists():
            parts = sk.read_text().split("---")
            fm = parts[1] if len(parts) > 1 else ""
            fname = next((l.split(":", 1)[1].strip()
                          for l in fm.splitlines() if l.startswith("name:")), None)
            check(fname == name, f"{name}: SKILL name == folder")
            check("description:" in fm and len(fm) > 80,
                  f"{name}: SKILL has a description")


# --------------------------------------------------------------------------- #
# 2. Self-tests
# --------------------------------------------------------------------------- #

ANALYZERS = {
    "migration-guard": skill("migration-guard") / "scripts" / "analyze_migration.py",
    "llm-app-doctor": skill("llm-app-doctor") / "scripts" / "analyze_llm_code.py",
    "dockerfile-doctor": skill("dockerfile-doctor") / "scripts" / "analyze_dockerfile.py",
    "env-doctor": skill("env-doctor") / "scripts" / "analyze_env.py",
    "regression-finder": skill("regression-finder") / "scripts" / "regression_finder.py",
}


def test_selftests() -> None:
    print("\n[2] analyzer self-tests")
    for name, script in ANALYZERS.items():
        check(script.exists(), f"{name}: analyzer script present")
        if script.exists():
            rc, out = run(script, "--selftest")
            check(rc == 0, f"{name}: --selftest exit 0")
            check("passed" in out.lower(), f"{name}: --selftest reports passed")


# --------------------------------------------------------------------------- #
# 3. Fixtures (unsafe flagged, safe clean)
# --------------------------------------------------------------------------- #

def _rules(data: dict) -> set[str]:
    return {f["rule"] for f in data.get("findings", [])}


def test_fixtures() -> None:
    print("\n[3] example fixtures")

    # migration-guard
    mg = ANALYZERS["migration-guard"]
    ex = skill("migration-guard") / "examples"
    rc, data = run_json(mg, str(ex / "unsafe_migration.sql"))
    check(rc == 1, "migration-guard: unsafe migration exits 1")
    check({"PG004", "PG007"} <= _rules(data),
          f"migration-guard: unsafe rules include PG004,PG007 ({sorted(_rules(data))})")
    rc, _ = run(mg, str(ex / "safe_migration.sql"))
    check(rc == 0, "migration-guard: safe migration exits 0")

    # llm-app-doctor
    llm = ANALYZERS["llm-app-doctor"]
    ex = skill("llm-app-doctor") / "examples"
    rc, data = run_json(llm, str(ex / "unsafe_app.py"))
    check(rc == 1, "llm-app-doctor: unsafe app exits 1")
    check({"SEC001", "SEC002", "API001", "REL002", "MOD001"} <= _rules(data),
          f"llm-app-doctor: unsafe rules complete ({sorted(_rules(data))})")
    rc, data = run_json(llm, str(ex / "unsafe_app.ts"))
    check("SEC002" in _rules(data), "llm-app-doctor: TS injection sink flagged")
    rc, _ = run(llm, str(ex / "safe_app.py"))
    check(rc == 0, "llm-app-doctor: safe app exits 0")

    # dockerfile-doctor
    doc = ANALYZERS["dockerfile-doctor"]
    ex = skill("dockerfile-doctor") / "examples"
    rc, data = run_json(doc, str(ex / "Dockerfile.unsafe"))
    check(rc == 1, "dockerfile-doctor: unsafe Dockerfile exits 1")
    check({"DKR001", "DKR003", "DKR010"} <= _rules(data),
          f"dockerfile-doctor: unsafe rules include DKR001,003,010 ({sorted(_rules(data))})")
    rc, _ = run(doc, str(ex / "Dockerfile.safe"))
    check(rc == 0, "dockerfile-doctor: safe Dockerfile exits 0")

    # env-doctor
    envd = ANALYZERS["env-doctor"]
    sample = skill("env-doctor") / "examples" / "sample_project"
    rc, data = run_json(envd, str(sample))
    check(rc == 1, "env-doctor: sample project exits 1")
    check({"ENV001", "ENV002", "ENV004", "ENV005", "ENV006"} <= _rules(data),
          f"env-doctor: sample rules complete ({sorted(_rules(data))})")


# --------------------------------------------------------------------------- #
# 4. regression-finder live bisect on a planted bug
# --------------------------------------------------------------------------- #

def _git(repo: Path, *args: str, check_rc: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True)
    if check_rc and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr}")
    return p


def test_regression_finder_live() -> None:
    print("\n[4] regression-finder live bisect")
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        check(False, "git is available")
        return

    tmp = Path(tempfile.mkdtemp(prefix="regfind-"))
    try:
        _git(tmp, "init", "-q")
        _git(tmp, "config", "user.email", "t@t.t")
        _git(tmp, "config", "user.name", "t")
        (tmp / ".gitignore").write_text("__pycache__/\n")
        (tmp / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        # -B avoids writing bytecode so the tree stays clean during bisect
        (tmp / "test_calc.py").write_text(
            "from calc import add\nassert add(2, 3) == 5\nprint('ok')\n")
        _git(tmp, "add", "-A")
        _git(tmp, "commit", "-qm", "c1 good")
        for i, msg in enumerate(("c2 docs", "c3 docs"), 1):
            (tmp / f"n{i}.md").write_text("x")
            _git(tmp, "add", "-A")
            _git(tmp, "commit", "-qm", msg)
        # the bug
        (tmp / "calc.py").write_text("def add(a, b):\n    return a - b  # bug\n")
        _git(tmp, "add", "-A")
        _git(tmp, "commit", "-qm", "c4 introduces bug")
        culprit = _git(tmp, "rev-parse", "HEAD").stdout.strip()
        for i, msg in enumerate(("c5", "c6"), 1):
            (tmp / f"u{i}.txt").write_text("x")
            _git(tmp, "add", "-A")
            _git(tmp, "commit", "-qm", msg)

        branch_before = _git(tmp, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

        proc = subprocess.run(
            [sys.executable, str(ANALYZERS["regression-finder"]),
             "--good", "HEAD~5", "--bad", "HEAD", "--verify",
             "--cmd", "python3 -B test_calc.py"],
            cwd=str(tmp), text=True, capture_output=True)
        out = proc.stdout + proc.stderr

        check(proc.returncode == 0, "regression-finder: exits 0 on found culprit")
        m = re.search(r"([0-9a-f]{7,40}) is the first bad commit", out)
        check(bool(m), "regression-finder: reports a first bad commit")
        if m:
            check(culprit.startswith(m.group(1)) or m.group(1).startswith(culprit[:len(m.group(1))]),
                  "regression-finder: identifies the correct culprit (c4)")
        branch_after = _git(tmp, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        check(branch_after == branch_before,
              f"regression-finder: restores original branch ({branch_after})")
    finally:
        subprocess.run(["rm", "-rf", str(tmp)])


# --------------------------------------------------------------------------- #

def main() -> int:
    print(f"claude-skills test harness  (python {sys.version.split()[0]})")
    test_structure()
    test_selftests()
    test_fixtures()
    test_regression_finder_live()

    print("\n" + "=" * 60)
    total = PASSES + len(FAILURES)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)}/{total} checks FAILED")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"RESULT: all {total} checks passed ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())

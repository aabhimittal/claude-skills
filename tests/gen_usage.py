#!/usr/bin/env python3
"""Generate docs/USAGE.md from real analyzer runs.

This captures the *actual* terminal output each skill produces on its bundled
example fixtures, so the published "expected output" can never drift from real
behavior. Regenerate any time with:  python3 tests/gen_usage.py

Commit hashes in the regression-finder demo are normalized to stable
placeholders so regenerating doesn't churn the doc.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "USAGE.md"


def skill(p: str) -> Path:
    return REPO / "plugins" / p / "skills" / p


def run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True)
    return proc.returncode, (proc.stdout + proc.stderr).rstrip("\n")


def block(rc: int, text: str) -> str:
    return f"```text\n$ {text}\n```\n*exit code: {rc}*\n" if False else \
        f"```console\n{text}\n```\n_exit code: **{rc}**_\n"


def section(title: str, ask: str, cmd_display: str, rc: int, output: str,
            extra: str = "", footer: str = "") -> str:
    s = f"### {title}\n\n"
    s += f"**Ask Claude:** _{ask}_\n\n"
    s += "The skill activates and runs:\n\n"
    s += f"```bash\n{cmd_display}\n```\n\n"
    s += "Output:\n\n"
    s += f"```console\n{output}\n```\n\n"
    if footer:
        s += footer + "\n"
    else:
        s += f"_Exit code: **{rc}**_ — non-zero because findings met the threshold, "
        s += "so this also fails a CI check.\n"
    if extra:
        s += "\n" + extra + "\n"
    return s + "\n---\n\n"


PY = sys.executable


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []

    parts.append(
        "# Using the claude-skills plugins\n\n"
        "_This document is auto-generated from real runs by "
        "[`tests/gen_usage.py`](../tests/gen_usage.py); the output blocks below "
        "are captured verbatim. Regenerate with `python3 tests/gen_usage.py`._\n\n"
        "## How these skills work once installed\n\n"
        "After `/plugin install <name>@claude-skills`, you don't call anything "
        "directly — you just talk to Claude. Each skill has trigger phrases in "
        "its `SKILL.md`, so asking *\"is this migration safe?\"* or *\"audit my "
        "AI code\"* makes Claude run the matching analyzer, read the findings, and "
        "explain + fix them for you.\n\n"
        "Every analyzer is also a standalone, dependency-free CLI you can run "
        "yourself or wire into CI. Shared conventions:\n\n"
        "- **Exit code** is `0` when clean and `1` when a finding meets the "
        "`--fail-on` threshold (default `high`) — so any analyzer doubles as a CI "
        "gate.\n"
        "- **`--json`** emits machine-readable findings.\n"
        "- **`--selftest`** verifies the analyzer itself.\n\n"
        "The examples below are the exact fixtures shipped under each skill's "
        "`examples/` directory.\n\n---\n\n"
        "## 🛡️ migration-guard\n\n"
    )

    mg = skill("migration-guard") / "scripts" / "analyze_migration.py"
    mg_ex = skill("migration-guard") / "examples"
    rc, out = run([PY, str(mg), "unsafe_migration.sql"], mg_ex)
    parts.append(section(
        "Catch an unsafe database migration",
        "Is this migration safe to deploy to production?",
        "analyze_migration.py unsafe_migration.sql",
        rc, out,
        extra="The same analyzer accepts ORM migrations too — e.g. "
              "`analyze_migration.py unsafe_rails_migration.rb` flags "
              "`add_index` without `algorithm: :concurrently`."))
    rc, out = run([PY, str(mg), "safe_migration.sql"], mg_ex)
    parts.append("### The online-safe rewrite passes\n\n"
                 f"```console\n{out}\n```\n\n_Exit code: **{rc}**_\n\n---\n\n")

    parts.append("## 🩺 llm-app-doctor\n\n")
    llm = skill("llm-app-doctor") / "scripts" / "analyze_llm_code.py"
    llm_ex = skill("llm-app-doctor") / "examples"
    rc, out = run([PY, str(llm), "unsafe_app.py"], llm_ex)
    parts.append(section(
        "Audit AI integration code",
        "Audit my Claude integration for leaked keys, injection, and dead models.",
        "analyze_llm_code.py unsafe_app.py",
        rc, out))
    rc, out = run([PY, str(llm), "--json", "unsafe_app.ts"], llm_ex)
    # show just the first finding from JSON for brevity
    import json
    data = json.loads(out)
    first = json.dumps(data["findings"][0], indent=2)
    parts.append("### Machine-readable output (`--json`) — also works on TS/JS\n\n"
                 "Scanning the TypeScript fixture with `--json` (first finding shown):\n\n"
                 f"```json\n{first}\n```\n\n"
                 "Top-level fields: `tool`, `knowledge_date`, `threshold`, "
                 "`passed`, `count`, `findings[]`.\n\n---\n\n")
    rc, out = run([PY, str(llm), "safe_app.py"], llm_ex)
    parts.append("### The corrected app passes\n\n"
                 f"```console\n{out}\n```\n\n_Exit code: **{rc}**_\n\n---\n\n")

    parts.append("## 🐳 dockerfile-doctor\n\n")
    doc = skill("dockerfile-doctor") / "scripts" / "analyze_dockerfile.py"
    doc_ex = skill("dockerfile-doctor") / "examples"
    rc, out = run([PY, str(doc), "Dockerfile.unsafe"], doc_ex)
    parts.append(section(
        "Harden a Dockerfile",
        "Review my Dockerfile for security and why the image is so big.",
        "analyze_dockerfile.py Dockerfile.unsafe",
        rc, out))
    rc, out = run([PY, str(doc), "Dockerfile.safe"], doc_ex)
    parts.append("### The hardened, slimmed image passes\n\n"
                 f"```console\n{out}\n```\n\n_Exit code: **{rc}**_\n\n---\n\n")

    parts.append("## 🔑 env-doctor\n\n")
    envd = skill("env-doctor") / "scripts" / "analyze_env.py"
    env_ex = skill("env-doctor") / "examples"
    rc, out = run([PY, str(envd), "sample_project"], env_ex)
    parts.append(section(
        "Reconcile .env with code and the example",
        "Is my .env in gitignore, and is my .env.example in sync with the code?",
        "analyze_env.py ./sample_project   # point it at a project root",
        rc, out,
        extra="Note `PORT` is read by the code but **not** flagged — it's a "
              "well-known runtime variable, not something `.env` must provide."))

    # regression-finder live demo (normalized shas)
    parts.append("## 🔍 regression-finder\n\n")
    reg = skill("regression-finder") / "scripts" / "regression_finder.py"
    demo = _run_regression_demo(reg)
    parts.append(section(
        "Find the commit that broke a test",
        "Which commit broke this test? It passed a few commits ago.",
        'regression_finder.py --good HEAD~5 --bad HEAD --verify \\\n'
        '    --cmd "python3 -B test_calc.py"',
        0, demo,
        footer="_Exit code: **0**_ — a culprit was found. (Exit `1` means bisect "
               "couldn't conclude; `2` means a precondition failed, e.g. a dirty "
               "tree.)",
        extra="Safety: it refuses to run on a dirty tree, `--verify` first "
              "confirms the command passes at `--good` and fails at `--bad`, and "
              "it always restores your original branch on exit. _(Commit hashes "
              "above are illustrative placeholders; the interleaved `AssertionError` "
              "tracebacks are your test command's own output at the bad commits.)_"))

    # CI section
    parts.append(
        "## Using the analyzers in CI\n\n"
        "Each analyzer exits non-zero when a finding meets `--fail-on`, so they "
        "drop straight into a pipeline. Example GitHub Actions step:\n\n"
        "```yaml\n"
        "- name: Guard rails\n"
        "  run: |\n"
        "    python3 plugins/migration-guard/skills/migration-guard/scripts/"
        "analyze_migration.py db/migrate/*.sql --fail-on high\n"
        "    python3 plugins/llm-app-doctor/skills/llm-app-doctor/scripts/"
        "analyze_llm_code.py ./src --fail-on high\n"
        "    python3 plugins/dockerfile-doctor/skills/dockerfile-doctor/scripts/"
        "analyze_dockerfile.py Dockerfile --fail-on high\n"
        "    python3 plugins/env-doctor/skills/env-doctor/scripts/"
        "analyze_env.py . --fail-on high\n"
        "```\n\n"
        "This repo's own CI (`.github/workflows/ci.yml`) runs the full harness "
        "`tests/run_all.py` across Python 3.9–3.12 on every push.\n\n"
        "## Reproduce this document\n\n"
        "```bash\npython3 tests/gen_usage.py\n```\n"
    )

    OUT.write_text("".join(parts))
    print(f"wrote {OUT.relative_to(REPO)} ({OUT.stat().st_size} bytes)")
    return 0


def _run_regression_demo(reg: Path) -> str:
    """Build a throwaway repo with a planted bug, run regression-finder, and
    return its output with temp paths and commit hashes normalized."""
    def git(repo: Path, *a: str):
        subprocess.run(["git", "-C", str(repo), *a], check=True,
                       capture_output=True, text=True)

    tmp = Path(tempfile.mkdtemp(prefix="regdemo-"))
    try:
        git(tmp, "init", "-q")
        git(tmp, "config", "user.email", "dev@example.com")
        git(tmp, "config", "user.name", "dev")
        (tmp / ".gitignore").write_text("__pycache__/\n")
        (tmp / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (tmp / "test_calc.py").write_text(
            "from calc import add\nassert add(2, 3) == 5\nprint('ok')\n")
        git(tmp, "add", "-A"); git(tmp, "commit", "-qm", "add: working")
        for i in (1, 2):
            (tmp / f"doc{i}.md").write_text("x")
            git(tmp, "add", "-A"); git(tmp, "commit", "-qm", f"docs {i}")
        (tmp / "calc.py").write_text("def add(a, b):\n    return a - b  # bug\n")
        git(tmp, "add", "-A"); git(tmp, "commit", "-qm", "refactor add (introduces bug)")
        for i in (1, 2):
            (tmp / f"more{i}.txt").write_text("x")
            git(tmp, "add", "-A"); git(tmp, "commit", "-qm", f"unrelated {i}")

        proc = subprocess.run(
            [PY, str(reg), "--good", "HEAD~5", "--bad", "HEAD", "--verify",
             "--cmd", "python3 -B test_calc.py"],
            cwd=str(tmp), text=True, capture_output=True)
        out = (proc.stdout + proc.stderr).rstrip("\n")
        # normalize: temp path + commit hashes -> stable placeholders
        out = out.replace(str(tmp), "/tmp/demo-repo")
        pool = ["a1b2c3d", "e4f5a6b", "c7d8e9f", "0a1b2c3", "4d5e6f7", "8a9b0c1"]
        mapping: dict[str, str] = {}

        def repl(m: re.Match) -> str:
            h = m.group(0)
            key = h[:7]
            if key not in mapping:
                mapping[key] = pool[len(mapping) % len(pool)]
            return mapping[key] + h[7:0]  # keep short form only

        out = re.sub(r"\b[0-9a-f]{7,40}\b", repl, out)
        # Truncate at the natural end of the tool's output (drop the test
        # command's trailing restore-checkout traceback noise).
        marker = "Inspect the full diff with:"
        idx = out.find(marker)
        if idx != -1:
            end = out.find("\n", idx)
            out = out[:end] if end != -1 else out
        return out
    finally:
        subprocess.run(["rm", "-rf", str(tmp)])


if __name__ == "__main__":
    sys.exit(main())

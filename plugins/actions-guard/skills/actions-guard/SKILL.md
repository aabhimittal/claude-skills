---
name: actions-guard
description: This skill should be used when the user asks to "review my GitHub Actions workflow", "is my CI workflow secure", "audit my workflows", "pin my actions", "check for pwn requests / pull_request_target", "script injection in Actions", or works with a `.github/workflows/*.yml` file. It statically lints GitHub Actions workflows for supply-chain and injection risks: unpinned third-party actions, pull_request_target checking out untrusted PR code, `${{ github.event.* }}` script injection in run:, missing least-privilege permissions, secret interpolation, and hard-coded credentials.
version: 1.0.0
---

# actions-guard

Lint GitHub Actions workflows for the mistakes that let attackers run code or
steal secrets in your CI: unpinned actions, `pull_request_target` pwn requests,
script injection, over-privileged tokens, and leaked secrets.

## When to use this skill

Use it whenever a workflow is written or reviewed — a new `.github/workflows/*.yml`,
or a diff touching one. Triggers include "is my CI secure?", "review my GitHub
Actions", "pin my actions", "check for script injection / pull_request_target".

## Workflow

1. **Locate the workflow(s).** Look at the diff, the named file, or pass the repo
   root — the analyzer finds `.github/workflows/*.yml` and `*.yaml`.

2. **Run the analyzer.** Dependency-free standard-library Python (no PyYAML):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/actions-guard/scripts/analyze_workflow.py" .github/workflows/ci.yml
   # or scan the whole repo:
   python3 ".../analyze_workflow.py" .
   ```

   Flags: `--json` (machine output), `--fail-on {info,low,medium,high,critical}`
   (CI gate, default `high`), `--selftest`. Exit code is `1` when findings meet
   the threshold, `0` when clean.

3. **Interpret the findings.** Each has a rule ID, severity, the exact line, why
   it's exploitable, and the fix. Full catalog: `references/rules.md`.

4. **Explain and rewrite.** Lead with the critical items (pwn request, leaked
   token), then the rest. Offer the hardened workflow — the `examples/` folder
   pairs an unsafe workflow with its secured version (SHA-pinned actions,
   `pull_request` trigger, least-privilege `permissions:`, env-bound inputs).

5. **Note the caveats.** This is a static, indentation-aware scan, not a YAML
   parser — highly unusual formatting may be missed. SHA-pinning first-party
   `actions/*` is defense-in-depth (flagged `medium`); the sharp risks are
   third-party pins, pwn requests, and injection.

## What it detects (summary)

- **GHA001** action pinned to a mutable tag/branch instead of a commit SHA.
- **GHA002** `pull_request_target` / `workflow_run` that checks out untrusted PR code.
- **GHA003** `${{ github.event.* }}` / `github.head_ref` injected into a `run:` shell.
- **GHA004** no `permissions:` block (token defaults to broad scope).
- **GHA005** `${{ secrets.* }}` interpolated into `run:` instead of via `env:`.
- **GHA007** remote script piped into a shell (`curl … | sh`).
- **GHA008** hard-coded credential in the workflow.

See `references/rules.md` for details.

## Output style for the user

Lead with the count and the worst item ("this is a pwn request — a fork PR can
run with your secrets"), go worst-first with file:line, and finish with a
hardened workflow they can copy.

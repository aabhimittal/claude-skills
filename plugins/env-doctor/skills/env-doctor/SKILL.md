---
name: env-doctor
description: This skill should be used when the user asks to "check my .env", "audit environment variables", "is my .env in gitignore", "sync my .env.example", "what env vars does this project need", "did I leak a secret in .env", or works with environment configuration (.env, .env.example, process.env / os.environ usage). It reconciles a project's real .env files, the variables the code actually reads, and the .env.example, then flags un-ignored .env files, missing/undocumented/dead variables, placeholder values, and secrets committed to the example.
version: 1.0.0
---

# env-doctor

Reconcile a project's three sources of environment-config truth — the real
`.env`, what the code actually reads, and the committed `.env.example` — and
flag the gaps that leak secrets or break onboarding.

## When to use this skill

Use it when setting up, reviewing, or debugging environment configuration:
onboarding a new contributor ("what env vars does this need?"), before a commit
("is my `.env` ignored?", "did I leak a key?"), or keeping `.env.example` in
sync with the code. Triggers include "check my .env", "audit env vars", "sync my
.env.example".

## Workflow

1. **Point it at the project root.** The analyzer walks the tree for `.env*`
   files, `.env.example`/`.sample`/`.template`, the root `.gitignore`, and source
   files in Python/JS/TS/Go/Ruby/PHP/Java/Rust.

2. **Run the analyzer.** Dependency-free standard-library Python:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/env-doctor/scripts/analyze_env.py" .
   ```

   Useful flags:
   - `--json` — machine-readable output.
   - `--fail-on {info,low,medium,high,critical}` — non-zero exit at/above this
     severity (default `high`); use it to gate CI.
   - `--selftest` — sanity-check the analyzer.

   Exit code is `1` when findings meet the threshold, `0` when clean.

3. **Interpret the findings.** Each has a rule ID, severity, the file/line (and
   the variable name), why it matters, and the fix. Full catalog:
   `references/rules.md`.

4. **Act on them.** Lead with the security items (un-ignored `.env`, leaked
   secret), then the onboarding gaps. You can offer to generate or update
   `.env.example` from the reconciled set of variables. The `examples/`
   directory has a sample project that triggers every rule.

5. **Mind the caveats.** This is static reconciliation — it reads files, it does
   not call git or check what's actually committed (the `.gitignore` check is
   pattern-based). It detects env usage via common idioms; dynamic variable names
   built at runtime are invisible. Treat secret detection as a strong hint, not
   proof.

## What it detects (summary)

- **ENV001 (high):** a real `.env` not matched by `.gitignore`.
- **ENV005 (high):** placeholder/empty value left in a real `.env`.
- **ENV006 (high):** a real-looking secret committed to `.env.example`.
- **ENV002 (medium):** a variable the code reads but the example doesn't document.
- **ENV004 (medium):** a variable in `.env` that the example doesn't document.
- **ENV003 (low):** a variable in the example that no code reads (stale).

See `references/rules.md` for details.

## Output style for the user

Lead with the security headline (e.g. "your `.env` isn't git-ignored, and the
example has a real DSN — rotate it"), then the onboarding/documentation gaps.
Offer to produce a corrected `.env.example`.

---
name: dockerfile-doctor
description: This skill should be used when the user asks to "review my Dockerfile", "is my Dockerfile secure", "why is my image so big", "harden my container", "shrink my Docker image", "Dockerfile best practices", or works with a Dockerfile / containerfile. It statically lints Dockerfiles for security issues (running as root, :latest tags, secrets in ENV/ARG, ADD vs COPY, piping remote scripts to a shell) and image-bloat / cache issues (apt and pip caches left in layers, dependency-install ordering), and suggests fixes.
version: 1.0.0
---

# dockerfile-doctor

Lint Dockerfiles for the problems that ship insecure or bloated images: running
as root, mutable base tags, secrets baked into layers, `ADD` misuse, leftover
package caches, remote scripts piped into a shell, and cache-busting layer order.

## When to use this skill

Use it whenever a Dockerfile is written, reviewed, or about to be built — a new
`Dockerfile`, a `*.dockerfile`, or a diff touching one. Triggers include "review
my Dockerfile", "is this container secure?", "why is my image so big?", or
"harden this image".

## Workflow

1. **Locate the Dockerfile(s).** Look at the diff, the named file, or scan the
   repo — the analyzer accepts files *or* directories and finds `Dockerfile`,
   `Dockerfile.*`, and `*.dockerfile`.

2. **Run the analyzer.** Dependency-free standard-library Python:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/dockerfile-doctor/scripts/analyze_dockerfile.py" Dockerfile
   # or scan a tree:
   python3 ".../analyze_dockerfile.py" .
   ```

   Useful flags:
   - `--json` — machine-readable output.
   - `--fail-on {info,low,medium,high,critical}` — non-zero exit at/above this
     severity (default `high`); use it to gate CI.
   - `--selftest` — sanity-check the analyzer.

   Exit code is `1` when findings meet the threshold, `0` when clean.

3. **Interpret the findings.** Each has a rule ID, severity, the exact line, why
   it matters, and a concrete fix. Full catalog: `references/rules.md`.

4. **Explain and rewrite.** Lead with the headline (e.g. "runs as root + a
   leaked token — fix before building"), go worst-first, then offer a corrected
   Dockerfile. The `examples/` folder pairs an unsafe Dockerfile with its
   hardened, slimmed version.

5. **Note the caveats.** This is a static linter — it reads the Dockerfile, it
   does not build the image or inspect base images. Some checks (e.g. multi-stage
   opportunities) are judgement calls; on a tiny internal image not all findings
   warrant action. Frame findings by impact.

## What it detects (summary)

- **Security:** runs as root (no/`root` `USER`); `:latest`/untagged base; secrets
  in `ENV`/`ARG`; `ADD` where `COPY` belongs; remote URL `ADD`; `curl ... | sh`;
  `sudo` in `RUN`.
- **Bloat / cache:** apt lists not cleaned in-layer; missing
  `--no-install-recommends`; `pip install` without `--no-cache-dir`; dependency
  install after `COPY . .` (cache-busting order).

See `references/rules.md` for every rule ID and severity.

## Output style for the user

Lead with the count and severity headline, then walk findings worst-first with
file:line and the real consequence ("this token is recoverable with `docker
history`"), and finish with a corrected Dockerfile they can copy.

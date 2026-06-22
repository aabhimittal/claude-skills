---
name: llm-app-doctor
description: This skill should be used when the user asks to "audit my AI code", "review my LLM integration", "check my prompt for injection", "is my Claude/OpenAI code safe", "am I using a current model", "why is my prompt cache not hitting", or works with code that calls an LLM chat/completions API (Anthropic, OpenAI, or compatible SDKs in Python or JavaScript/TypeScript). It statically audits AI integration code for leaked API keys, prompt-injection sinks, retired/deprecated model IDs, parameters that now error on current models, missing generation limits, and prompt-cache busters.
version: 1.0.0
---

# llm-app-doctor

Audit AI integration code for the mistakes that break LLM apps in production:
leaked keys, prompt-injection sinks, dead model IDs, parameters that now return
a 400, unbounded or timeout-prone generation, and prompt-cache busters.

## When to use this skill

Use it whenever code that calls an LLM is being written, reviewed, or shipped —
chat endpoints, agents, RAG pipelines, batch jobs, eval harnesses. Triggers
include "audit my AI code", "is this prompt injectable?", "am I on a current
model?", or any diff touching a `messages.create` / `chat.completions.create` /
`responses.create` call. It covers Anthropic (authoritatively) plus OpenAI and
compatible SDKs in Python and JavaScript/TypeScript.

## Workflow

1. **Locate the AI code.** Look at the diff, the file the user named, or scan
   the whole repo — the analyzer accepts files *or* directories and skips
   `node_modules`, `.venv`, build dirs, etc.

2. **Run the analyzer.** Dependency-free standard-library Python:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/llm-app-doctor/scripts/analyze_llm_code.py" path/to/app.py
   # or scan a tree:
   python3 ".../analyze_llm_code.py" ./src
   ```

   Useful flags:
   - `--json` — machine-readable output (includes `knowledge_date`).
   - `--fail-on {info,low,medium,high,critical}` — non-zero exit at/above this
     severity (default `high`); use it to gate CI.
   - `--selftest` — sanity-check the analyzer.

   Exit code is `1` when findings meet the threshold, `0` when clean.

3. **Interpret the findings.** Each has a rule ID, severity, the exact line,
   why it bites in production, and a concrete fix. The full catalog is in
   `references/rules.md`; the safe-rewrite patterns are in
   `references/safe-patterns.md`.

4. **Explain and rewrite.** Lead with the headline (e.g. "1 leaked key, 1
   injection sink — fix before deploy"), walk findings worst-first, and offer
   the corrected code. The `examples/` folder pairs an unsafe app with its safe
   version (Python and TypeScript).

5. **Mind the caveats.** The analyzer is a static linter with an embedded model
   knowledge date — it does not call any API. For Anthropic model-ID and
   parameter questions, prefer the bundled `claude-api` skill or current docs if
   something looks stale. When `model=` is a variable (not a literal),
   model-specific rules are skipped for that call — surface that if relevant.

## What it detects (summary)

- **Security:** hard-coded API keys; untrusted input interpolated into the
  *system* prompt (prompt-injection sink).
- **Model hygiene:** retired model IDs (404), deprecated models, date-suffixed
  aliases.
- **API drift (current Anthropic):** `budget_tokens`, `temperature`/`top_p`/
  `top_k`, and `output_format` that now error or are deprecated.
- **Reliability:** missing `max_tokens`; large `max_tokens` without streaming.
- **Cost/caching:** volatile values (timestamps, UUIDs) in the system prompt
  that silently disable prompt caching.

See `references/rules.md` for every rule ID and severity.

## Output style for the user

Lead with the count and severity headline, then go worst-first with file:line,
the production impact in plain terms ("this key is in git history — rotate it"),
and a copy-pasteable fix. Keep it about *consequences*, not just syntax.

# llm-app-doctor rule catalog

Stable rule IDs, severities, and the reasoning behind each. Severities:
`critical` > `high` > `medium` > `low` > `info`. Default CI gate (`--fail-on`)
is `high`. Model/API facts are current as of the analyzer's embedded knowledge
date (see `--json` output `knowledge_date`).

## Security

| Rule | Severity | Trigger | Risk |
| --- | --- | --- | --- |
| SEC001 | critical | API key literal (`sk-ant-…`, `sk-…`, `sk-proj-…`) in source | Leaked credential; rotation needs a code change |
| SEC002 | high | Untrusted-looking variable interpolated into the **system** prompt | Prompt injection (user text gains operator authority) + cache busting |

**SEC002 detail.** The analyzer flags interpolation (f-string, JS template
literal, or `+` concatenation) into a `system=` / `system:` value when the
interpolated identifier looks like user input (`user_question`, `userMessage`,
`query`, `requestBody`, …). System prompts should be byte-stable. Untrusted
text belongs in a user message; on Opus 4.8, trusted runtime context can be a
`{"role": "system"}` entry in `messages[]` instead of string interpolation.

## Model hygiene

| Rule | Severity | Trigger |
| --- | --- | --- |
| MOD001 | critical | Retired model ID (404) — `claude-2.x`, `claude-3-*`, `claude-instant`, … |
| MOD002 | medium | Deprecated model nearing retirement (`claude-opus-4-1`, `claude-3-haiku`, legacy OpenAI families) |
| MOD003 | medium | Date snapshot appended to a current alias (e.g. `claude-opus-4-8-20251101`) |

## API drift (current Anthropic models)

| Rule | Severity | Trigger | Fix |
| --- | --- | --- | --- |
| API001 | high\* | `thinking.budget_tokens` | `thinking={'type':'adaptive'}` + `output_config.effort` |
| API002 | high | `temperature`/`top_p`/`top_k` on Fable 5 / Opus 4.8 / 4.7 | Remove the param; steer via prompting |
| API003 | medium | Deprecated top-level `output_format=` | `output_config={'format': {...}}` |

\* API001 is `high` on Fable 5 / Opus 4.8 / 4.7 (these **400**), `medium` on
Opus 4.6 / Sonnet 4.6 (deprecated), and `medium` advisory when the model can't
be resolved statically.

## Reliability

| Rule | Severity | Trigger | Risk |
| --- | --- | --- | --- |
| REL001 | medium | Anthropic `messages.create` with no `max_tokens` | API requires it; unset cap risks runaway output/cost |
| REL002 | high | `max_tokens` > 16000 on a non-streaming call | Can exceed the SDK HTTP timeout; the Python SDK raises before sending |

## Cost / caching

| Rule | Severity | Trigger | Risk |
| --- | --- | --- | --- |
| COST001 | low | `datetime.now()` / `uuid` / `random` rendered into the system prompt | Cache prefix changes every request, so prompt caching never hits |

## Notes & limitations

- This is a **static linter**, not a runtime checker — it reads source, it does
  not call any API. Findings about model IDs and parameter validity reflect the
  embedded knowledge date; verify against the latest model docs if in doubt.
- Detection is provider-aware but Anthropic-authoritative. OpenAI/compatible
  calls get the structural checks (keys, missing limits, injection sinks, legacy
  model families) but not Anthropic-specific parameter rules.
- When a `model=` value is a variable rather than a string literal, model-
  dependent rules (API001/API002, MOD*) are skipped for that call — the analyzer
  won't guess.
- Scanning is pattern/balanced-parens based. Highly dynamic call construction
  (building the kwargs dict elsewhere) may be missed.

---
name: api-contract-guard
description: This skill should be used when the user asks to "check for breaking API changes", "did I break the API", "diff these schemas", "is this a breaking change", "compare OpenAPI/Swagger versions", "GraphQL breaking change", or works with two versions of an API schema (OpenAPI/Swagger JSON or GraphQL SDL). It diffs the old and new schema and reports changes that break existing clients — removed endpoints/types/fields, newly-required parameters/arguments, type changes, and removed enum values — while noting additive changes as non-breaking.
version: 1.0.0
---

# api-contract-guard

Diff two versions of an API schema and catch **breaking changes** before they
ship — removed endpoints/fields, newly-required inputs, type changes — for
OpenAPI/Swagger (JSON) and GraphQL (SDL).

## When to use this skill

Use it whenever an API schema changes: a PR that edits `openapi.json`/
`schema.graphql`, a "did I break the contract?" question, or a pre-release
compatibility check. Triggers include "breaking API change?", "diff these
schemas", "compare OpenAPI versions", "GraphQL breaking change".

## Workflow

1. **Get the two versions.** You need the **old** (baseline) and **new**
   schema. In a PR, old = the base branch's schema, new = the head's — e.g.
   `git show origin/main:openapi.json > /tmp/old.json` then compare against the
   working copy.

2. **Run the analyzer.** Dependency-free standard-library Python:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/api-contract-guard/scripts/analyze_contract.py" OLD NEW
   ```

   It auto-detects the format (OpenAPI JSON vs GraphQL SDL — both files must be
   the same). Flags: `--json`, `--fail-on {info,low,medium,high,critical}`
   (default `high`), `--selftest`. Exit code is `1` when a breaking change meets
   the threshold, `0` when compatible.

3. **Interpret the findings.** Each has a rule ID, severity, the exact location
   (endpoint/type/field), why it breaks clients, and how to avoid it. Additive
   changes are reported as `info` (won't fail CI). Full catalog:
   `references/rules.md`.

4. **Advise.** Lead with a clear verdict ("this is a breaking change: 2 removed
   fields + 1 newly-required argument"), then the safe path: deprecate before
   removing, keep new inputs optional / give defaults, or version the endpoint.
   The `examples/` folder has paired old/new schemas for both formats.

5. **Caveats.** OpenAPI support covers paths, operations, parameters, and
   request-body required fields (JSON specs; not YAML — convert with any tool
   first). It does not deep-diff response schemas or resolve `$ref` chains.
   GraphQL covers types, fields, field types, arguments, and enum/union members.

## What it detects (summary)

**OpenAPI:** removed path (OAS001), removed operation (OAS002), new required
parameter (OAS003), parameter became required (OAS004), removed required
parameter (OAS005), new required body field (OAS006); additive new path
(OAS100, info).

**GraphQL:** removed type (GQL001), type kind changed (GQL002), removed
enum/union member (GQL003), removed field (GQL004), field type changed (GQL005),
new required argument (GQL006), removed argument (GQL007), argument became
required (GQL008); additive new type (GQL100, info).

See `references/rules.md` for details.

## Output style for the user

Open with the verdict (breaking vs compatible) and the count, list the breaking
changes with their exact locations, then give the non-breaking migration path
(deprecate → dual-support → remove; keep inputs optional; version the endpoint).

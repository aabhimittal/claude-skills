# env-doctor rule catalog

Stable rule IDs and severities. `critical` > `high` > `medium` > `low` > `info`.
Default CI gate (`--fail-on`) is `high`.

| Rule | Severity | Trigger | Risk |
| --- | --- | --- | --- |
| ENV001 | high | A real `.env` (`.env`, `.env.local`, …) not matched by root `.gitignore` | One `git add .` from committing secrets |
| ENV005 | high | Placeholder/empty value in a real `.env` (`changeme`, `xxx`, empty, `<...>`) | App starts with a broken/missing setting |
| ENV006 | high | Real-looking secret value in an example file | Example files are committed → secret leak |
| ENV002 | medium | Variable read by code but missing from any example file | Fresh checkout is misconfigured and fails |
| ENV004 | medium | Variable set in `.env` but missing from the example | Undocumented config; contributors won't know |
| ENV003 | low | Variable in the example that no source file reads | Stale documentation |

## How sources are gathered

- **Code usage:** `os.environ[...]`, `os.environ.get(...)`, `os.getenv(...)`,
  `os.Getenv(...)` (Go), `process.env.X` / `process.env["X"]`,
  `import.meta.env.X` (Vite), `Deno.env.get(...)`, `getenv(...)`, `ENV["X"]`
  (Ruby) — across `.py/.js/.jsx/.ts/.tsx/.mjs/.cjs/.go/.rb/.php/.java/.rs`.
- **Real `.env`:** files named `.env` or `.env.<something>` that are **not**
  examples.
- **Example:** files whose name contains `example`, `sample`, `template`,
  `dist`, or `default`.
- **`.gitignore`:** the root-level file; matched against `.env` basenames with
  glob semantics (e.g. `.env`, `.env*`, `*.env`).

## Well-known variables (never flagged as undocumented)

`NODE_ENV`, `PORT`, `HOME`, `PATH`, `PWD`, `USER`, `LANG`, `TZ`, `CI`, `DEBUG`,
`PYTHONPATH`, `HOSTNAME`, `RAILS_ENV`, `FLASK_ENV`, `ENV`, `ENVIRONMENT`, and a
handful of others — these conventionally come from the OS/runtime, not `.env`.

## Notes & limitations

- Static reconciliation only — it does **not** invoke git, so the `.gitignore`
  check is a pattern match, not a check of what is actually tracked. If a `.env`
  was committed *before* being ignored, this won't see that; scrub history
  separately.
- Env-var usage is detected via common idioms. Variables whose names are built
  dynamically (`os.getenv(prefix + name)`) are invisible.
- Secret detection (ENV005/ENV006) is heuristic — high-entropy strings and
  secret-looking key names. Treat it as a strong hint and verify.

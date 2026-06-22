# env-doctor sample project (fixture)

A deliberately broken project that triggers every env-doctor rule. Run:

```bash
python3 ../../scripts/analyze_env.py .
```

Expected findings:

| Rule | Where | Why |
| --- | --- | --- |
| ENV001 | `.env` | no `.gitignore`, so the real `.env` isn't ignored |
| ENV005 | `.env` → `STRIPE_API_KEY=changeme` | placeholder left in a real `.env` |
| ENV006 | `.env.example` → `SENTRY_DSN` | a real-looking (fake) secret committed to the example |
| ENV004 | `.env` → `STRIPE_API_KEY`, `LEGACY_FLAG` | set in `.env` but undocumented in the example |
| ENV002 | `app.py` → `STRIPE_API_KEY` | read by code but missing from `.env.example` |
| ENV003 | `.env.example` → `SENTRY_DSN` | documented but never used in code |

`PORT` is read in `app.py` but **not** flagged — it's a well-known runtime var.

All values here are fake fixtures, not real credentials.

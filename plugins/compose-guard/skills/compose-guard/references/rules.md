# compose-guard rule catalog

Stable rule IDs and severities. `critical` > `high` > `medium` > `low` > `info`.
Default CI gate (`--fail-on`) is `high`.

| Rule | Severity | Trigger | Risk |
| --- | --- | --- | --- |
| CMP001 | critical | `privileged: true` | Disables nearly all isolation; trivial escape to host root |
| CMP002 | critical | `/var/run/docker.sock` (or `/run/docker.sock`) bind-mounted | Full control of the Docker daemon → host takeover. `:ro` does **not** mitigate it |
| CMP003 | high / medium | `network_mode: host`, `pid: host`, `ipc: host` (high); `userns_mode`/`uts: host` (medium) | Removes the namespace boundary with the host |
| CMP004 | high | `cap_add` with `SYS_ADMIN`, `SYS_PTRACE`, `SYS_MODULE`, `DAC_READ_SEARCH`, `NET_ADMIN`, `ALL`, … | Host-level power; `SYS_ADMIN` is close to root |
| CMP008 | high | `security_opt: seccomp:unconfined` / `apparmor:unconfined` | Removes the syscall/MAC filter that blocks most escapes |
| CMP006 | high | Secret-looking key in `environment:` with a literal value | Credential committed to version control |
| CMP007 | high / medium | Sensitive host path (`/`, `/etc`, `/proc`, `/sys`, `/var/lib/docker`, `/root`, …) bind-mounted — `high` if read-write, `medium` if `:ro` | Container can read or modify host state |
| CMP005 | medium | Image untagged or `:latest` | Non-reproducible deploys; image changes under you |
| CMP009 | medium | Datastore port (5432, 3306, 27017, 6379, 9200, …) published with no host IP | Binds `0.0.0.0` — often public on a cloud VM |

## Fix patterns

- **Drop, don't add.** `cap_drop: [ALL]` plus `read_only: true` is the strong
  default; add back only the single capability a service provably needs.
- **Never mount the socket.** If a container must orchestrate containers, put a
  socket proxy in front that allow-lists only the required API endpoints.
- **Secrets by reference.** `DB_PASSWORD: ${DB_PASSWORD}` (supplied at deploy
  time) or Docker/orchestrator secrets — never a literal.
- **Don't publish internal services.** Services that only other compose services
  reach need no `ports:` at all — put them on a shared internal network. If you
  must publish, bind loopback: `127.0.0.1:5432:5432`.
- **Pin images.** An explicit version tag, or a digest (`image@sha256:…`) for
  full reproducibility.
- **Narrow the mount.** Mount the specific subdirectory, and add `:ro` unless
  writes are genuinely required.

## Notes & limitations

- Static, indentation-aware scan — not a YAML parser. Inline `# comments` are
  stripped (respecting quotes), and bare `- ITEM` list entries are attributed to
  their enclosing key so `cap_add: [SYS_ADMIN]` is flagged while the safe
  `cap_drop: [ALL]` is not. Anchors/aliases and heavily flow-mapped services may
  be missed.
- Several rules are **contextual**: a dev stack bind-mounting `./src`, or a CI
  runner that genuinely needs the socket, may be acceptable. Treat severity as
  guidance and judge by environment (dev vs production).
- Secret detection is name-based (`*PASSWORD*`, `*TOKEN*`, `*SECRET*`, …) with a
  placeholder filter, so `${VAR}`, `changeme`, and `<your-key>` are not flagged.

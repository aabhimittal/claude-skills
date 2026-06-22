# dockerfile-doctor rule catalog

Stable rule IDs and severities. `critical` > `high` > `medium` > `low` > `info`.
Default CI gate (`--fail-on`) is `high`.

## Security

| Rule | Severity | Trigger | Risk |
| --- | --- | --- | --- |
| DKR001 | high | Final stage has no `USER`, or `USER root`/`USER 0` | App compromise = root in the container |
| DKR002 | medium | Base image untagged or `:latest` | Non-reproducible builds; base changes under you |
| DKR003 | high | Secret-looking `ENV` / `ARG` (`*TOKEN*`, `*SECRET*`, `*KEY*`, …) | Recoverable via `docker history` / image metadata |
| DKR004 | medium | `ADD` of a local file/dir | `ADD`'s implicit behavior is surprising; use `COPY` |
| DKR010 | high / medium | `curl\|wget ... \| sh`; or `ADD <url>` | Unreviewed, unpinned remote code at build time |
| DKR011 | low | `sudo` inside `RUN` | Build is already root; `sudo` may be absent and break it |

## Image bloat & cache

| Rule | Severity | Trigger | Fix |
| --- | --- | --- | --- |
| DKR005 | low | `apt install` without `--no-install-recommends` | Add the flag |
| DKR006 | medium | `apt` lists not removed in the same `RUN` | `&& rm -rf /var/lib/apt/lists/*` in-layer |
| DKR007 | low | `pip install` without `--no-cache-dir` | Add the flag |
| DKR008 | low | dependency install after `COPY . .` | Copy the manifest first, install, then `COPY . .` |

## Notes & limitations

- Static linter only — it parses the Dockerfile (joining `\` continuations and
  skipping comments). It does not build the image, pull base images, or inspect
  layers, so it can't measure actual image size or know what a base image ships.
- Secret detection is name-based (`ENV`/`ARG` whose key looks like a credential)
  plus a value heuristic for `ARG`; it won't catch a secret hidden in a
  generically-named variable.
- `DKR008` fires once per file when a dependency install follows a `COPY . .`;
  the fix (manifest-first copy) eliminates it.
- Rules reflect general best practice for Linux container images. On a tiny
  internal/throwaway image, the low-severity bloat items may not be worth acting
  on — treat severity as guidance.

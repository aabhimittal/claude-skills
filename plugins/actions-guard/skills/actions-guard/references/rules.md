# actions-guard rule catalog

Stable rule IDs and severities. `critical` > `high` > `medium` > `low` > `info`.
Default CI gate (`--fail-on`) is `high`.

| Rule | Severity | Trigger | Risk |
| --- | --- | --- | --- |
| GHA002 | critical | `pull_request_target`/`workflow_run` + checkout of PR head | Fork PR runs its own code with your secrets + write token ("pwn request") |
| GHA008 | critical | Token/key/private-key literal in the workflow | Committed credential; anyone with repo/log access can use it |
| GHA001 | high / medium | `uses:` pinned to a tag/branch, not a 40-char commit SHA | A moved tag (or compromised owner) runs malicious code in CI. `high` for third-party, `medium` for `actions/*`/`github/*` |
| GHA003 | high | `${{ github.event.* }}` / `github.head_ref` inside a `run:` shell | Attacker-controlled PR title/branch/body breaks out of the string → arbitrary commands |
| GHA007 | high | `curl … \| sh` in a `run:` step | Unreviewed remote code executes with the workflow's privileges |
| GHA005 | medium | `${{ secrets.* }}` interpolated into `run:` | Secret lands on the command line (process list / error logs); injection risk |
| GHA004 | medium | No `permissions:` key anywhere in the file | `GITHUB_TOKEN` gets the (often write-all) default scope |

## Fix patterns

- **Pin to a SHA:** `uses: actions/checkout@11bd719...  # v4.2.2`. Renovate/Dependabot
  can keep the pin current while preserving the comment.
- **Avoid pwn requests:** build/test untrusted PRs under `pull_request` (no
  secrets, read-only token). Keep any privileged step (labeling, commenting) in a
  separate job that never checks out or runs PR-controlled code.
- **Kill injection:** bind the value to `env:` and quote it —
  `env: {TITLE: ${{ github.event.pull_request.title }}}` then `run: echo "$TITLE"`.
  GitHub expands `${{ }}` *before* the shell sees it, so an unquoted expression is
  raw injection; an `env` var is passed as data.
- **Least privilege:** `permissions: {contents: read}` at the top; widen per-job.
- **Secrets via env:** `env: {TOKEN: ${{ secrets.TOKEN }}}` then use `"$TOKEN"`.

## Notes & limitations

- Static, indentation-aware scan — not a full YAML parser. It joins `run:` block
  scalars (`|`, `>`) by indentation, which covers normal formatting; exotic YAML
  (anchors, flow-mapped steps) may be missed.
- GHA002 is a heuristic: it fires when the file uses a privileged trigger **and**
  a checkout `ref:` references the PR head. Review the specific job.
- SHA-pinning `actions/*` is defense-in-depth (`medium`). The high-severity risks
  are third-party pins, pwn requests, and injection.

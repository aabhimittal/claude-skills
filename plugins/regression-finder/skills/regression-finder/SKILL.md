---
name: regression-finder
description: This skill should be used when the user asks to "find which commit broke this", "git bisect", "find the regression", "what commit introduced this bug", "this used to work", "bisect to find the failing commit", or wants to automatically locate the commit that introduced a test failure or behavior change. It drives an automated, safe `git bisect run` from a known-good ref and a repro command, then reports the culprit commit and its diff.
version: 1.0.0
---

# regression-finder

Find the exact commit that introduced a regression by driving an automated
`git bisect run` — safely (clean-tree check, always restores your branch) and
with a built-in sanity check that the repro actually distinguishes good from bad.

## When to use this skill

Use it whenever something "used to work" and you have (or can write) a command
that fails now but passed before: a failing test, a crash, a wrong output, a
performance cliff. Triggers include "which commit broke this?", "bisect this",
"find the regression".

## Workflow

1. **Establish the three inputs.**
   - **Bad ref** — where the bug exists (default `HEAD`).
   - **Good ref** — a commit/tag/branch where it did *not* exist. If the user
     doesn't know one, suggest an older tag or a date-based ref
     (`git rev-list -1 --before=2026-01-01 HEAD`).
   - **Repro command** — exits **0 when good**, **non-zero when bad**. A focused
     test is ideal (`pytest -x path::test`, `npm test -- -t name`, `cargo test
     name`, `go test ./pkg -run Name`), or a small `repro.sh`. See
     `references/recipes.md`.

2. **Run the finder.** It requires a clean working tree (bisect checks out
   commits) and restores your branch on exit no matter what:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/regression-finder/scripts/regression_finder.py" \
     --good v1.2.0 --bad HEAD --verify --cmd "pytest -x tests/test_foo.py::test_bar"
   # or put the command after --:
   python3 ".../regression_finder.py" --good abc123 -- pytest -x tests/test_foo.py
   ```

   - `--verify` (recommended) first confirms the command passes at `--good` and
     fails at `--bad` before bisecting — otherwise the result is meaningless.
   - The command follows bisect conventions: exit `0` = good, `1..127` (not 125)
     = bad, `125` = skip (e.g. won't build at that commit).

3. **Read the result.** On success it prints the culprit commit (sha, author,
   date, subject) and the files it changed, plus `git show <sha>` to inspect the
   full diff. Exit code is `0` when a culprit is found, `1` if bisect couldn't
   conclude (only skips, or the command never distinguished good from bad), `2`
   on a precondition error (dirty tree, unresolvable ref).

4. **Explain the culprit.** Open the diff, identify the specific change that
   caused the failure, and propose the fix. The bisect tells you *where*; your
   job is to explain *why* and fix it.

## Safety guarantees

- **Refuses to run on a dirty tree** — commit or stash first.
- **Always restores state** — `git bisect reset` runs on success, failure, and
  interrupt, returning you to your original branch/commit.
- **Verifies the boundary** (with `--verify`) so you don't bisect a flaky or
  mis-specified command.

## Tips

- Make the repro **fast and deterministic** — bisect runs it ~log2(N) times.
  Narrow to a single test and skip slow setup where possible.
- For "won't build at this commit" ranges, have the command `exit 125` to skip.
- Flaky test? Wrap it to retry, or it will mislead the bisect.

See `references/recipes.md` for ready-to-use commands per language/framework.

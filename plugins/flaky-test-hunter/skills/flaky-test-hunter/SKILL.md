---
name: flaky-test-hunter
description: This skill should be used when the user asks to "find flaky tests", "this test fails randomly", "why does CI fail intermittently", "test passes locally but fails in CI", "run the tests N times", "is this test flaky", or wants to diagnose non-deterministic test failures. It runs a test command repeatedly, ranks tests that both passed and failed by instability, and classifies likely root causes (timing, ordering/shared state, randomness, network, concurrency, resource exhaustion, time-of-day, float precision) with targeted fixes.
version: 1.0.0
---

# flaky-test-hunter

Find non-deterministic tests by running the suite repeatedly, rank them by how
unstable they are, and classify *why* each one flakes so the fix removes the
non-determinism instead of hiding it.

## When to use this skill

- "This test fails randomly" / "CI is flaky" / "passes locally, fails in CI".
- Before trusting a suspicious green run.
- After a retry was added to make CI pass (the flake is still there).

The core idea: **a single run proves nothing about a flaky test.** The unit of
evidence here is "ran it N times", and the output is a per-test failure *rate*.

## Workflow

1. **Get a runnable test command.** Narrow it if possible — the hunter runs it N
   times, so a focused command is much faster:
   `pytest -q tests/test_api.py`, `npm test -- -t "checkout"`,
   `go test ./pkg -run TestThing -count=1`.

2. **Run the hunt.**

   ```bash
   S="${CLAUDE_PLUGIN_ROOT}/skills/flaky-test-hunter/scripts/hunt_flaky.py"

   python3 "$S" -n 10 --cmd "pytest -q tests/"      # 10 runs
   python3 "$S" -n 20 -- pytest -q tests/test_api.py  # command after --
   python3 "$S" -n 30 --cmd "npm test" --json         # machine-readable
   python3 "$S" -n 10 --cmd "..." --timeout 120        # per-run timeout
   python3 "$S" -n 50 --cmd "..." --stop-after-flaky 1 # stop at first flake
   ```

   Per-test results are parsed from pytest, unittest, Jest/Vitest, Go test, and
   TAP output. If nothing parses, it falls back to run-level pass/fail (adding
   `-v` to the test command usually enables per-test detail). Exit code is `1`
   when any flaky test is observed.

3. **Read the ranking.** Tests that both passed *and* failed are flaky by
   definition, ranked with the most unstable (closest to a 50% failure rate)
   first, showing `failed 4/12 runs (33%)` and which run numbers failed.
   Consistently-failing tests are reported **separately** — those are broken, not
   flaky, and need a different fix.

4. **Act on the root cause.** Each flaky test gets a category, the signals that
   matched, and the fix. Full table plus isolation recipes:
   `references/causes.md`. The most common ones:
   - **timing** → wait on the condition, not the clock; never `sleep`.
   - **ordering** → shared state leaks; verify by running the test alone, then
     shuffled (`pytest -p randomly`, `go test -shuffle=on`).
   - **randomness** → seed the RNG or assert on invariants.
   - **concurrency** → often a *real* product race; run a race detector.

5. **Be honest about the evidence.** If nothing flaked in a short run, say so
   precisely: a 5% flake survives 10 runs ~60% of the time. The tool prints this
   caveat; don't upgrade "didn't reproduce in 10 runs" to "not flaky". Suggest
   more runs (`-n 30`+) for rare flakes.

## Important guidance

**Don't recommend a retry as the fix.** `--reruns` / `jest.retryTimes` converts a
visible flake into a hidden one and leaves the underlying race or leak in the
product code. It's a temporary shield at best — pair it with a tracked fix.

Likewise: lengthening a sleep makes the flake rarer, not absent, and skipping the
test silently removes coverage.

## Try it

`examples/flaky_suite.py` is a deliberately flaky suite (a simulated race, a
timeout, an unseeded-random assertion, plus one deterministically broken test):

```bash
python3 scripts/hunt_flaky.py -n 12 --cmd "python3 examples/flaky_suite.py"
```

It should surface three flaky tests with distinct root causes and separate out
the consistently broken one.

## Output style for the user

Lead with the count and the worst offender's failure rate ("`test_checkout`
failed 5/12 runs — 42%"), give each one its root cause and fix, keep
consistently-failing tests in a separate bucket, and state the confidence limit
of the run count you used.

# Flakiness root causes and fixes

The hunter classifies a failure by scanning that test's own failure output for
signal keywords. Categories below, with the fix that actually removes the
non-determinism rather than hiding it.

| Category | Typical signals | Why it's flaky | Fix |
| --- | --- | --- | --- |
| **timing** | `timeout`, `timed out`, `exceeded 5000ms`, `sleep`, `waitFor`, `retry` | The test assumes an operation finishes within a fixed wall-clock window; under CI load it doesn't | Wait on the *condition*, not the clock: poll with a deadline, or await a real signal/event. Inject a fake clock instead of sleeping |
| **ordering** | `already exists`, `duplicate key`, `unique constraint`, `not found`, `fixture`, `previous test` | State leaks between tests, so results depend on execution order | Each test creates and destroys its own data; reset global/module state in setup/teardown. Verify by running the test alone, then in shuffled order |
| **randomness** | `random`, `shuffle`, `uuid`, `faker`, `seed`, `hypothesis` | An unseeded random value flows into an assertion | Seed the RNG in setup, or assert on invariants (shape, membership, count) instead of exact values |
| **network** | `ECONNREFUSED`, `ETIMEDOUT`, `dns`, `502/503/504`, `429`, `ssl` | The test depends on a real external service and inherits its reliability | Stub the boundary: local fake server, or record-replay fixtures |
| **concurrency** | `race`, `deadlock`, `mutex`, `goroutine`, `data race`, `thread` | Two operations interleave differently between runs | Add real synchronization, or serialize the interleaving under test. Run a race detector (`go test -race`, TSan) — the test is often surfacing a genuine product bug |
| **resource** | `EADDRINUSE`, `too many open files`, `OOM`, `ENOSPC`, `port in use` | Leaked handles or a hard-coded port collide across runs | Close handles in teardown; bind to port `0` (ephemeral) instead of a fixed port |
| **time-of-day** | `datetime`, `timezone`, `today`, `midnight`, `DST`, `Date.now` | Behavior changes with the current date, time, or region | Freeze time (freezegun, jest fake timers, injected clock) and pin `TZ=UTC` |
| **float** | `toBeCloseTo`, `assertAlmostEqual`, `precision`, `1e-9` | Exact comparison of floating-point results | Compare with a tolerance/epsilon |

## How many runs do I need?

Flakiness is a probability, so a single green run proves almost nothing. If a
test fails with probability *p*, the chance it passes all *n* runs is `(1-p)^n`:

| fail rate | 5 runs | 10 runs | 20 runs | 50 runs |
| --- | --- | --- | --- | --- |
| 1% | 95% | 90% | 82% | 61% |
| 5% | 77% | 60% | 36% | 8% |
| 10% | 59% | 35% | 12% | 0.5% |
| 30% | 17% | 3% | 0.1% | ~0% |

Read the table as "probability the flake hides from you." **10 runs** is a
reasonable default for catching a >10% flake; use **30–50** when hunting a rare
one, and be explicit that a clean short run is weak evidence — the tool prints
this caveat itself.

## Isolating an order-dependency

If you suspect the **ordering** category:

```bash
pytest path/to/test.py::test_name        # alone — does it pass?
pytest -p randomly                       # shuffled order (pytest-randomly)
go test -shuffle=on ./...
npx jest --runInBand                     # serial: does parallelism matter?
```

A test that passes alone but fails in the suite is order-dependent, not
"randomly" broken.

## What not to do

- **Don't add a retry** (`--reruns`, `jest.retryTimes`) as the fix. It converts a
  visible flake into a hidden one and keeps the underlying race/leak in your
  product code. Retries are a temporary shield while you fix the cause — pair
  them with a tracking issue.
- **Don't lengthen the sleep.** It makes the suite slower and the flake rarer,
  not absent.
- **Don't skip the test** without recording why; a skipped flaky test is an
  untested code path.

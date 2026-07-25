#!/usr/bin/env python3
"""flaky-test-hunter: find non-deterministic tests by repeated execution.

Runs a test command N times, parses per-test results from the output, and ranks
tests by instability — those that both passed and failed across runs are flaky by
definition. Also classifies likely root causes from the failure output (timing,
ordering/shared state, randomness, network/IO, concurrency, resource leaks) and
suggests the corresponding fix.

Because a flaky test's whole problem is that a single run proves nothing, this
tool treats "ran it N times" as the unit of evidence and reports the observed
failure rate per test rather than a pass/fail verdict.

Supported result formats (auto-detected, best effort):
  * pytest      PASSED/FAILED/ERROR lines and `-v` output
  * unittest    `test_x (module.Class) ... ok/FAIL/ERROR`
  * Jest/Vitest `✓ / ✕ / ✗` and `PASS/FAIL` lines
  * Go          `--- PASS: TestX` / `--- FAIL: TestX`
  * Mocha/TAP   `ok 3 - name` / `not ok 3 - name`
  * fallback    if no per-test lines are found, run-level pass/fail is used

Design goals:
  * Pure Python standard library. Runs only the command you pass it.
  * Deterministic reporting for a given set of observations.
  * CI-friendly: non-zero exit when a test is flaky (configurable).

Usage:
    hunt_flaky.py -n 10 --cmd "pytest -q tests/"
    hunt_flaky.py -n 20 -- pytest -q tests/test_api.py
    hunt_flaky.py -n 10 --cmd "npm test" --json
    hunt_flaky.py --selftest

Exit codes:
    0  no flaky tests observed
    1  at least one flaky test observed (or --fail-on-any-failure and a failure)
    2  usage / runtime error
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict

# --------------------------------------------------------------------------- #
# Per-test result extraction
# --------------------------------------------------------------------------- #

PASS = "pass"
FAIL = "fail"

# Each entry: (compiled regex, status, group index holding the test id)
PATTERNS = [
    # pytest -v / -q with result keywords. The id must not swallow the trailing
    # status word, so ids here disallow spaces (parametrized `[a b]` still works
    # via the bracket alternative).
    (re.compile(r"^(?P<id>[\w./\-:]+::[\w\-.]+(?:\[[^\]]*\])?)\s+(?:PASSED|✓)\b",
                re.M), PASS, "id"),
    (re.compile(r"^(?P<id>[\w./\-:]+::[\w\-.]+(?:\[[^\]]*\])?)\s+(?:FAILED|ERROR|✕|✗)\b",
                re.M), FAIL, "id"),
    (re.compile(r"^(?:PASSED|✓)\s+(?P<id>[\w./\-:]+::[\w\-.]+(?:\[[^\]]*\])?)\s*$",
                re.M), PASS, "id"),
    (re.compile(r"^(?:FAILED|ERROR)\s+(?P<id>[\w./\-:]+(?:::[\w\-.]+(?:\[[^\]]*\])?)?)",
                re.M), FAIL, "id"),
    # unittest verbose
    (re.compile(r"^(?P<id>test\w+\s+\([\w.]+\))\s*\.\.\.\s*ok\s*$", re.M), PASS, "id"),
    (re.compile(r"^(?P<id>test\w+\s+\([\w.]+\))\s*\.\.\.\s*(FAIL|ERROR)", re.M), FAIL, "id"),
    # Go test
    (re.compile(r"^\s*--- PASS:\s+(?P<id>[\w/]+)", re.M), PASS, "id"),
    (re.compile(r"^\s*--- (?:FAIL|SKIP-FAIL):\s+(?P<id>[\w/]+)", re.M), FAIL, "id"),
    # TAP
    (re.compile(r"^ok\s+\d+\s*[-–]?\s*(?P<id>.+?)\s*$", re.M), PASS, "id"),
    (re.compile(r"^not ok\s+\d+\s*[-–]?\s*(?P<id>.+?)\s*$", re.M), FAIL, "id"),
    # Jest / Vitest / Mocha check marks
    (re.compile(r"^\s*[✓✔]\s+(?P<id>.+?)(?:\s+\(\d+\s*m?s\))?\s*$", re.M), PASS, "id"),
    (re.compile(r"^\s*[✕✗×]\s+(?P<id>.+?)(?:\s+\(\d+\s*m?s\))?\s*$", re.M), FAIL, "id"),
]

NOISE = re.compile(r"^(=+|-+|\.+|\s*)$")


def extract_failure_context(output: str, test_id: str, window: int = 1500) -> str:
    """Return the output slice most likely to describe *this* test's failure.

    Root-cause classification is only as good as the text it reads. Handing it the
    whole run's output lets one test's error message contaminate another's
    diagnosis, so anchor on the last mention of the test id and take the text
    around it; fall back to the tail of the output.
    """
    if not test_id or test_id == "<entire test run>":
        return output[-window * 2:]
    # Prefer a mention on a line that also signals failure.
    best = -1
    for m in re.finditer(re.escape(test_id), output):
        line_start = output.rfind("\n", 0, m.start()) + 1
        line_end = output.find("\n", m.end())
        line = output[line_start: line_end if line_end != -1 else len(output)]
        if re.search(r"\b(FAIL(ED)?|ERROR|✕|✗|not ok)\b", line, re.I):
            best = m.start()
    if best == -1:
        idx = output.rfind(test_id)
        if idx == -1:
            return output[-window * 2:]
        best = idx
    # Start *at* the test id (never before it) so the previous test's error text
    # can't bleed in, and stop at the next test's result line if one follows.
    chunk = output[best: best + window]
    next_test = re.search(
        r"\n(?=\S*::|\s*(?:--- (?:PASS|FAIL)|ok \d|not ok \d|[✓✔✕✗×]\s))",
        chunk[len(test_id):])
    if next_test:
        chunk = chunk[: len(test_id) + next_test.start()]
    return chunk


def parse_results(output: str) -> dict[str, str]:
    """Return {test_id: status}. A test that appears as failing anywhere in a
    single run is recorded as failing for that run."""
    results: dict[str, str] = {}
    for pattern, status, group in PATTERNS:
        for m in pattern.finditer(output):
            tid = (m.group(group) or "").strip()
            if not tid or NOISE.match(tid) or len(tid) > 200:
                continue
            # Ignore summary-ish lines that aren't test ids.
            if tid.lower().startswith(("test suites", "tests:", "snapshots", "time:")):
                continue
            if status == FAIL:
                results[tid] = FAIL
            else:
                results.setdefault(tid, PASS)
    return results


# --------------------------------------------------------------------------- #
# Root-cause classification
# --------------------------------------------------------------------------- #

CAUSE_RULES = [
    ("timing", re.compile(
        r"\b(timeout|timed out|exceeded \d+ ?ms|deadline exceeded|"
        r"waitfor|wait_for|sleep|retry|retries|eventually|"
        r"async callback was not invoked|exceeded timeout)\b", re.I),
     "Test depends on wall-clock timing or a fixed sleep",
     "Replace sleeps with explicit waits on the condition itself (poll with a "
     "deadline, or await a real signal/event). Never assert on elapsed time; "
     "inject a fake clock instead of sleeping."),
    ("ordering", re.compile(
        r"\b(already exists|duplicate key|unique constraint|not found|"
        r"no such (table|file|column)|stale|leaked|left over|"
        r"database is locked|fixture|setup|teardown|previous test)\b", re.I),
     "Shared state leaks between tests (order-dependent)",
     "Make each test create and destroy its own data. Reset global/module state "
     "and DB rows in setup/teardown, and confirm by running the test alone "
     "(`pytest path::test`) and in a shuffled order (`pytest -p randomly`)."),
    ("randomness", re.compile(
        # No trailing \b: identifiers like `uuid4(`, `random_`, `randint` must match.
        r"\b(randint|random|shuffle|uuid|faker|seed|hypothesis|arbitrary|"
        r"getrandbits|choice\(|sample\()", re.I),
     "Unseeded randomness makes the assertion non-deterministic",
     "Seed the RNG to a fixed value in test setup, or assert on invariants "
     "(shape, membership, count) rather than exact random values."),
    ("network", re.compile(
        r"\b(connection (refused|reset|error)|econnrefused|econnreset|etimedout|"
        r"dns|socket|502|503|504|rate limit|429|ssl|certificate|"
        r"getaddrinfo|network is unreachable|failed to fetch)\b", re.I),
     "Real network or external service dependency",
     "Stub the boundary: use a local fake/mock server or record-replay "
     "fixtures. A test that touches the internet inherits the internet's "
     "reliability."),
    ("concurrency", re.compile(
        r"\b(race|deadlock|lock|mutex|concurrent|thread|goroutine|"
        r"data race|atomic|not thread-safe|event loop)\b", re.I),
     "Race condition between concurrent operations",
     "Add proper synchronization, or make the test deterministic by "
     "serializing the interleaving under test. Run with a race detector "
     "(`go test -race`, thread sanitizer) to find the real ordering bug."),
    ("resource", re.compile(
        r"\b(out of memory|oom|too many open files|emfile|enospc|disk|"
        r"port .* (?:in use|already)|eaddrinuse|address already in use|"
        r"resource temporarily unavailable)\b", re.I),
     "Resource exhaustion or a port/file collision",
     "Close handles in teardown, and bind to an ephemeral port (port 0) "
     "instead of a hard-coded one so parallel runs don't collide."),
    ("time-of-day", re.compile(
        r"\b(datetime|timestamp|timezone|utc|today|tomorrow|midnight|"
        r"date\.now|time\.now|strftime|dst)\b", re.I),
     "Depends on the current date/time or timezone",
     "Freeze time (freezegun, jest fake timers, an injected clock) and pin "
     "the timezone (`TZ=UTC`) so the test can't drift across midnight, DST, "
     "or CI regions."),
    ("float", re.compile(
        r"\b(0\.0000\d+|1e-\d+|floating|precision|round(ing)? error|"
        r"assertalmostequal|toBeCloseTo)\b", re.I),
     "Floating-point precision sensitivity",
     "Compare with a tolerance (`pytest.approx`, `toBeCloseTo`, epsilon) "
     "instead of exact equality."),
]


def classify(failure_text: str) -> list[dict]:
    """Return ranked likely root causes for a failure blob."""
    hits = []
    for name, pattern, summary, fix in CAUSE_RULES:
        found = pattern.findall(failure_text or "")
        if found:
            # Count distinct matched keywords as a weak confidence signal.
            terms = {(" ".join(f) if isinstance(f, tuple) else f).lower()
                     for f in found}
            terms = {t.strip() for t in terms if t and t.strip()}
            hits.append({"category": name, "summary": summary, "fix": fix,
                         "signals": sorted(terms)[:6], "weight": len(terms)})
    hits.sort(key=lambda h: (-h["weight"], h["category"]))
    return hits


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


@dataclass
class TestStats:
    test_id: str
    passes: int = 0
    failures: int = 0
    fail_runs: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passes + self.failures

    @property
    def fail_rate(self) -> float:
        return (self.failures / self.total) if self.total else 0.0

    @property
    def flaky(self) -> bool:
        return self.passes > 0 and self.failures > 0

    @property
    def always_failed(self) -> bool:
        return self.failures > 0 and self.passes == 0

    def sort_key(self) -> tuple:
        # Flaky first, then by how close to 50/50 (max instability), then id.
        instability = 1.0 - abs(self.fail_rate - 0.5) * 2 if self.flaky else -1.0
        return (0 if self.flaky else 1, -instability, self.test_id)


@dataclass
class RunRecord:
    index: int
    exit_code: int
    duration: float
    n_pass: int
    n_fail: int


def aggregate(run_results: list[dict[str, str]]) -> dict[str, TestStats]:
    stats: dict[str, TestStats] = {}
    for i, results in enumerate(run_results, start=1):
        for tid, status in results.items():
            st = stats.setdefault(tid, TestStats(test_id=tid))
            if status == FAIL:
                st.failures += 1
                st.fail_runs.append(i)
            else:
                st.passes += 1
    return stats


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def run_once(cmd: list[str], shell: bool, timeout: int | None) -> tuple[int, str, float]:
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd if not shell else cmd[0], shell=shell,
                              text=True, capture_output=True, timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out, time.monotonic() - start
    except subprocess.TimeoutExpired as e:
        partial = ""
        for stream in (e.stdout, e.stderr):
            if stream:
                partial += stream if isinstance(stream, str) else stream.decode(
                    "utf-8", "replace")
        return 124, partial + f"\n[flaky-test-hunter] run timed out after {timeout}s\n", \
            time.monotonic() - start


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def render_text(runs: list[RunRecord], stats: dict[str, TestStats],
                failure_samples: dict[str, str], per_test: bool) -> str:
    n = len(runs)
    flaky = [s for s in stats.values() if s.flaky]
    always = [s for s in stats.values() if s.always_failed]
    failed_runs = [r for r in runs if r.exit_code != 0]

    out = []
    out.append(f"flaky-test-hunter: {n} run(s), "
               f"{len(failed_runs)} with a non-zero exit code")
    if not per_test:
        out.append("  (no per-test results parsed — reporting at run level; pass a "
                   "verbose flag such as `-v` for per-test detail)")
    out.append("")

    for r in runs:
        mark = "ok  " if r.exit_code == 0 else "FAIL"
        detail = f"{r.n_pass} passed, {r.n_fail} failed" if per_test else \
                 f"exit {r.exit_code}"
        out.append(f"  run {r.index:>3}  {mark}  {detail}  ({r.duration:.1f}s)")
    out.append("")

    if flaky:
        out.append(f"FLAKY — passed in some runs and failed in others "
                   f"({len(flaky)} test(s), most unstable first):")
        out.append("")
        for s in sorted(flaky, key=lambda x: x.sort_key()):
            out.append(f"  {s.test_id}")
            out.append(f"    failed {s.failures}/{s.total} runs "
                       f"({s.fail_rate * 100:.0f}%)  — runs {s.fail_runs}")
            causes = classify(failure_samples.get(s.test_id, ""))
            if causes:
                top = causes[0]
                out.append(f"    likely cause: {top['category']} — {top['summary']}")
                out.append(f"      signals: {', '.join(top['signals'])}")
                out.append(f"      fix: {top['fix']}")
                for c in causes[1:3]:
                    out.append(f"    also possible: {c['category']} — {c['summary']}")
            else:
                out.append("    likely cause: not classified from the captured output")
                out.append("      fix: re-run with a verbose failure trace, then "
                           "check for shared state, timing, and randomness.")
            out.append("")
    else:
        out.append("No flaky tests observed: no test both passed and failed across "
                   "these runs.")
        out.append("")

    if always:
        out.append(f"CONSISTENTLY FAILING ({len(always)}) — broken, not flaky:")
        for s in sorted(always, key=lambda x: x.test_id):
            out.append(f"  {s.test_id}  (failed {s.failures}/{s.total})")
        out.append("")

    if flaky:
        out.append("FAIL: flaky tests observed.")
    elif always:
        out.append("OK (no flakiness), but consistently failing tests are present.")
    else:
        out.append("OK: stable across all runs.")

    # Statistical caveat — important for honest interpretation.
    if n < 10 and not flaky:
        out.append("")
        out.append(f"Note: {n} run(s) is weak evidence. A test that fails 5% of the "
                   f"time has a ~{(0.95 ** n) * 100:.0f}% chance of passing all "
                   f"{n} runs here. Increase -n for more confidence.")
    return "\n".join(out)


def render_json(runs: list[RunRecord], stats: dict[str, TestStats],
                failure_samples: dict[str, str], per_test: bool) -> str:
    flaky = sorted([s for s in stats.values() if s.flaky], key=lambda x: x.sort_key())
    payload = {
        "tool": "flaky-test-hunter",
        "runs": len(runs),
        "per_test_parsing": per_test,
        "runs_detail": [asdict(r) for r in runs],
        "flaky_count": len(flaky),
        "passed": len(flaky) == 0,
        "flaky": [{
            "test_id": s.test_id,
            "failures": s.failures,
            "total": s.total,
            "fail_rate": round(s.fail_rate, 4),
            "failed_in_runs": s.fail_runs,
            "likely_causes": classify(failure_samples.get(s.test_id, "")),
        } for s in flaky],
        "consistently_failing": sorted(
            [s.test_id for s in stats.values() if s.always_failed]),
    }
    return json.dumps(payload, indent=2)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #


def run_selftest() -> int:
    failures = 0

    def check(cond, label):
        nonlocal failures
        if not cond:
            failures += 1
            print(f"  FAIL: {label}")

    # --- parsing ---
    pytest_out = """
tests/test_a.py::test_ok PASSED
tests/test_a.py::test_bad FAILED
FAILED tests/test_b.py::test_other
"""
    r = parse_results(pytest_out)
    check(r.get("tests/test_a.py::test_ok") == PASS, "pytest PASSED parsed")
    check(r.get("tests/test_a.py::test_bad") == FAIL, "pytest FAILED parsed")
    check(r.get("tests/test_b.py::test_other") == FAIL, "pytest FAILED-prefix parsed")

    go_out = "--- PASS: TestAlpha (0.00s)\n--- FAIL: TestBeta (0.01s)\n"
    r = parse_results(go_out)
    check(r.get("TestAlpha") == PASS, "go PASS parsed")
    check(r.get("TestBeta") == FAIL, "go FAIL parsed")

    tap_out = "ok 1 - adds numbers\nnot ok 2 - handles empty\n"
    r = parse_results(tap_out)
    check(r.get("adds numbers") == PASS, "tap ok parsed")
    check(r.get("handles empty") == FAIL, "tap not ok parsed")

    jest_out = "  ✓ renders header (12 ms)\n  ✕ fetches data (30 ms)\n"
    r = parse_results(jest_out)
    check(r.get("renders header") == PASS, "jest pass parsed")
    check(r.get("fetches data") == FAIL, "jest fail parsed")

    ut_out = ("test_alpha (mod.TestCase) ... ok\n"
              "test_beta (mod.TestCase) ... FAIL\n")
    r = parse_results(ut_out)
    check(r.get("test_alpha (mod.TestCase)") == PASS, "unittest ok parsed")
    check(r.get("test_beta (mod.TestCase)") == FAIL, "unittest FAIL parsed")

    # A failure anywhere in a run wins over a pass for the same id
    mixed = "tests/t.py::x PASSED\nFAILED tests/t.py::x\n"
    check(parse_results(mixed).get("tests/t.py::x") == FAIL,
          "failure takes precedence within a run")

    # --- aggregation ---
    st = aggregate([
        {"a": PASS, "b": PASS},
        {"a": FAIL, "b": PASS},
        {"a": PASS, "b": PASS},
        {"a": FAIL, "b": FAIL},
    ])
    check(st["a"].flaky and st["a"].failures == 2 and st["a"].total == 4,
          "flaky detection + counts")
    check(st["a"].fail_runs == [2, 4], "records which runs failed")
    check(st["b"].flaky, "b flaky (failed once)")
    st2 = aggregate([{"c": FAIL}, {"c": FAIL}])
    check(st2["c"].always_failed and not st2["c"].flaky,
          "always-failing is not flaky")
    st3 = aggregate([{"d": PASS}, {"d": PASS}])
    check(not st3["d"].flaky, "always-passing is not flaky")

    # ordering: the more unstable test (closer to 50%) ranks first
    stats = aggregate([{"near50": FAIL, "rare": PASS}] * 5 +
                      [{"near50": PASS, "rare": PASS}] * 5 +
                      [{"near50": PASS, "rare": FAIL}])
    ranked = sorted([s for s in stats.values() if s.flaky],
                    key=lambda x: x.sort_key())
    check(ranked and ranked[0].test_id == "near50",
          f"most unstable ranked first (got {[r.test_id for r in ranked]})")

    # --- classification ---
    cats = [c["category"] for c in classify("TimeoutError: timed out after 5000ms")]
    check("timing" in cats, f"timing classified (got {cats})")
    cats = [c["category"] for c in classify("psycopg2 duplicate key value violates unique constraint")]
    check("ordering" in cats, f"ordering classified (got {cats})")
    cats = [c["category"] for c in classify("ECONNREFUSED 127.0.0.1:5432")]
    check("network" in cats, f"network classified (got {cats})")
    cats = [c["category"] for c in classify("DATA RACE detected between goroutines")]
    check("concurrency" in cats, f"concurrency classified (got {cats})")
    cats = [c["category"] for c in classify("Error: listen EADDRINUSE address already in use :::3000")]
    check("resource" in cats, f"resource classified (got {cats})")
    cats = [c["category"] for c in classify("assert uuid4() == 'fixed'")]
    check("randomness" in cats, f"randomness classified (got {cats})")
    cats = [c["category"] for c in classify("expected 2026-01-01 but got today's date in UTC")]
    check("time-of-day" in cats, f"time-of-day classified (got {cats})")
    check(classify("") == [], "empty text yields no causes")

    # --- failure-context isolation ---
    # Each test's diagnosis must read only its own error, not its neighbour's.
    multi = (
        "t.py::test_slow FAILED\n"
        "  TimeoutError: operation timed out after 5000ms\n"
        "t.py::test_rand FAILED\n"
        "  assert uuid4() == 'fixed': random token mismatch\n"
    )
    slow_ctx = extract_failure_context(multi, "t.py::test_slow", window=80)
    rand_ctx = extract_failure_context(multi, "t.py::test_rand", window=80)
    check("timed out" in slow_ctx and "uuid4" not in slow_ctx,
          "context for test_slow excludes the other test's error")
    check("uuid4" in rand_ctx, "context for test_rand includes its own error")
    check([c["category"] for c in classify(slow_ctx)][:1] == ["timing"],
          f"test_slow classified as timing "
          f"(got {[c['category'] for c in classify(slow_ctx)]})")
    check("randomness" in [c["category"] for c in classify(rand_ctx)],
          "test_rand classified as randomness")
    check(extract_failure_context("no mention here", "t.py::missing") ==
          "no mention here", "missing id falls back to output tail")

    if failures:
        print(f"selftest: {failures} failure(s)")
        return 1
    print("selftest: all parsing, aggregation, ranking and classification "
          "cases passed ✓")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="hunt_flaky.py",
        description="Find non-deterministic tests by running a command repeatedly.")
    p.add_argument("-n", "--runs", type=int, default=10,
                   help="number of times to run the command (default: 10)")
    p.add_argument("--cmd", help="test command (run via the shell)")
    p.add_argument("--timeout", type=int,
                   help="per-run timeout in seconds (a timeout counts as a failure)")
    p.add_argument("--stop-after-flaky", type=int, default=0, metavar="K",
                   help="stop early once K distinct flaky tests are found")
    p.add_argument("--json", action="store_true", help="emit JSON output")
    p.add_argument("--quiet", action="store_true",
                   help="don't stream per-run progress to stderr")
    p.add_argument("--selftest", action="store_true", help="run built-in checks")
    p.add_argument("rest", nargs=argparse.REMAINDER,
                   help="command after -- (alternative to --cmd)")
    args = p.parse_args(argv)

    if args.selftest:
        return run_selftest()

    rest = args.rest[1:] if args.rest and args.rest[0] == "--" else args.rest
    if args.cmd:
        cmd, shell = [args.cmd], True
    elif rest:
        cmd, shell = list(rest), False
    else:
        p.print_usage()
        print("error: provide a test command via --cmd or after --", file=sys.stderr)
        return 2
    if args.runs < 2:
        print("error: -n must be at least 2 (one run cannot show flakiness)",
              file=sys.stderr)
        return 2

    run_records: list[RunRecord] = []
    run_results: list[dict[str, str]] = []
    failure_samples: dict[str, str] = {}
    any_per_test = False

    for i in range(1, args.runs + 1):
        code, output, dur = run_once(cmd, shell, args.timeout)
        results = parse_results(output)
        if results:
            any_per_test = True
        else:
            # Fall back to a synthetic run-level "test" so flakiness is still
            # detectable when we can't parse individual tests.
            results = {"<entire test run>": FAIL if code != 0 else PASS}
        run_results.append(results)
        n_fail = sum(1 for v in results.values() if v == FAIL)
        run_records.append(RunRecord(i, code, round(dur, 2),
                                     len(results) - n_fail, n_fail))
        for tid, status in results.items():
            if status == FAIL and tid not in failure_samples:
                failure_samples[tid] = extract_failure_context(output, tid)
        if not args.quiet:
            print(f"[flaky-test-hunter] run {i}/{args.runs}: exit {code}, "
                  f"{n_fail} failing ({dur:.1f}s)", file=sys.stderr)
        if args.stop_after_flaky:
            interim = aggregate(run_results)
            if sum(1 for s in interim.values() if s.flaky) >= args.stop_after_flaky:
                if not args.quiet:
                    print("[flaky-test-hunter] stopping early: flaky threshold met",
                          file=sys.stderr)
                break

    stats = aggregate(run_results)
    print(render_json(run_records, stats, failure_samples, any_per_test)
          if args.json else
          render_text(run_records, stats, failure_samples, any_per_test))
    return 1 if any(s.flaky for s in stats.values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

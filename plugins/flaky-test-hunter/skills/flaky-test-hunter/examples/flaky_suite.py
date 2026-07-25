"""A deliberately flaky test suite (fixture for flaky-test-hunter).

Run it repeatedly and the results differ — which is exactly the point:
    hunt_flaky.py -n 12 --cmd "python3 examples/flaky_suite.py"

Emits pytest-style result lines so the hunter can parse per-test outcomes
without pytest installed.
"""
import random
import sys

FILE = "examples/flaky_suite.py"


def test_stable_pass():
    return True


def test_race_condition():
    # Simulates a race: fails ~40% of the time. Failure text mentions a race so
    # the classifier can categorize it.
    if random.random() < 0.40:
        raise AssertionError("DATA RACE detected between goroutines: concurrent "
                             "map write while another thread held the lock")
    return True


def test_timeout_prone():
    # Simulates a slow dependency: fails ~25% of the time on a timeout.
    if random.random() < 0.25:
        raise AssertionError("TimeoutError: operation timed out after 5000ms "
                             "waiting for the response")
    return True


def test_unseeded_random():
    # Asserts on an unseeded random value: fails ~30% of the time.
    if random.random() < 0.30:
        raise AssertionError("assert uuid4() == 'fixed-id': random token did not "
                             "match the expected seed")
    return True


def test_always_broken():
    # Not flaky — deterministically broken. Should be reported separately.
    raise AssertionError("expected 5 but got 4")


TESTS = [test_stable_pass, test_race_condition, test_timeout_prone,
         test_unseeded_random, test_always_broken]


def main() -> int:
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"{FILE}::{fn.__name__} PASSED")
        except AssertionError as e:
            failed += 1
            print(f"{FILE}::{fn.__name__} FAILED")
            print(f"  {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

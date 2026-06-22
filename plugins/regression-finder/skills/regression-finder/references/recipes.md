# regression-finder recipes

Ready-to-use `--cmd` values. The command must exit **0 when good** and
**non-zero when bad**. Keep it fast and deterministic — bisect runs it
~log2(commits) times.

## Test runners

| Stack | Command |
| --- | --- |
| pytest (one test) | `pytest -x -q tests/test_foo.py::test_bar` |
| pytest (keyword) | `pytest -x -q -k "bar and not slow"` |
| unittest | `python -m unittest tests.test_foo.TestFoo.test_bar` |
| Jest / Vitest | `npm test -- -t "name of test"` |
| Node tap/node:test | `node --test test/foo.test.js` |
| Go | `go test ./pkg -run '^TestThing$' -count=1` |
| Rust | `cargo test thing_name -- --exact` |
| RSpec | `bundle exec rspec spec/foo_spec.rb -e "does the thing"` |
| PHPUnit | `vendor/bin/phpunit --filter testThing tests/FooTest.php` |
| Maven (Java) | `mvn -q -Dtest=FooTest#testBar test` |

## Skip commits that can't be tested

If the project won't build/install at some commits in the range, make those
**skip** instead of bad — exit code `125`:

```sh
--cmd 'make build || exit 125; pytest -x -q tests/test_foo.py::test_bar'
```

## Behavioral / output regressions (no test exists yet)

Write a tiny repro that asserts the behavior, then bisect on it:

```sh
# repro.sh — exit 0 if correct, 1 if broken
#!/bin/sh
out=$(./mytool --flag input.txt)
[ "$out" = "expected output" ]
```

```sh
--cmd './repro.sh'
```

## Performance cliffs

Fail when a command exceeds a time budget:

```sh
--cmd 'start=$(date +%s); ./bench.sh >/dev/null; end=$(date +%s); [ $((end-start)) -lt 5 ]'
```

## Flaky tests

Bisect is only reliable on a deterministic signal. If the test is flaky, retry
it so a single fluke doesn't mislead the search:

```sh
--cmd 'for i in 1 2 3; do pytest -x -q tests/test_foo.py::test_bar && exit 0; done; exit 1'
```

## Picking a good ref when you don't know one

```sh
# last commit before a date
git rev-list -1 --before=2026-01-01 HEAD
# an older release tag
git tag --sort=-creatordate | head
```

#!/usr/bin/env python3
"""llm-app-doctor: a static auditor for LLM/AI integration code.

Scans application code that calls LLM chat/completions APIs (Anthropic, OpenAI,
and compatible SDKs in Python and JavaScript/TypeScript) and flags issues that
bite AI apps in production: leaked API keys, prompt-injection sinks, retired or
deprecated model IDs, parameters that now 400 on current models, missing or
unsafe generation limits, and prompt-cache busters.

Design goals:
  * Pure Python standard library. No third-party deps, no network access.
  * Deterministic: the same input always produces the same findings.
  * CI-friendly: a non-zero exit code when findings meet a severity threshold.

Model and API-drift facts are current as of the embedded knowledge date below
and are intentionally conservative — the analyzer only flags things it is
confident are retired/removed, and explains the safe replacement.

Usage:
    analyze_llm_code.py FILE [FILE ...]      # scan files
    analyze_llm_code.py DIR                  # recurse a directory
    analyze_llm_code.py --json FILE          # machine-readable output
    analyze_llm_code.py --fail-on medium DIR # gate CI at a threshold
    analyze_llm_code.py --selftest           # run built-in checks

Exit codes:
    0  no findings at or above the --fail-on threshold (default: high)
    1  one or more findings at or above the threshold
    2  usage / runtime error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from typing import Iterable

KNOWLEDGE_DATE = "2026-06"

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SEVERITY_LABEL = {k: k.upper() for k in SEVERITY_ORDER}

SOURCE_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


# --------------------------------------------------------------------------- #
# Model knowledge (Anthropic authoritative; small OpenAI legacy set)
# --------------------------------------------------------------------------- #

# Retired Anthropic models — these now return HTTP 404.
ANTHROPIC_RETIRED = re.compile(
    r"\bclaude-(?:"
    r"2\.0|2\.1|instant[\w.-]*|"
    r"3-opus[\w.-]*|3-sonnet[\w.-]*|"
    r"3-5-sonnet[\w.-]*|3-7-sonnet[\w.-]*|3-5-haiku[\w.-]*"
    r")\b",
    re.IGNORECASE,
)

# Deprecated Anthropic models — still active, retiring soon.
ANTHROPIC_DEPRECATED = re.compile(
    r"\bclaude-(?:opus-4-1|opus-4-0|opus-4(?![.\d-])|sonnet-4-0|sonnet-4(?![.\d-])|3-haiku)[\w.-]*\b",
    re.IGNORECASE,
)

# Current Anthropic models where temperature/top_p/top_k and budget_tokens
# now return a 400 (sampling params removed; budget_tokens removed).
ANTHROPIC_NO_SAMPLING = re.compile(
    r"\bclaude-(?:fable-5|mythos-5|opus-4-8|opus-4-7)\b", re.IGNORECASE
)

# Anthropic models where budget_tokens is deprecated but not yet a hard error.
ANTHROPIC_BUDGET_DEPRECATED = re.compile(
    r"\bclaude-(?:opus-4-6|sonnet-4-6)\b", re.IGNORECASE
)

# Appending a date suffix to a current alias is unnecessary and can 404.
ANTHROPIC_DATE_SUFFIXED_ALIAS = re.compile(
    r"\bclaude-(?:opus-4-8|opus-4-7|opus-4-6|sonnet-4-6|fable-5)-\d{8}\b", re.IGNORECASE
)

# Retired/legacy OpenAI model families (conservative list).
OPENAI_LEGACY = re.compile(
    r"\b(?:text-(?:davinci|curie|babbage|ada)[\w.-]*|code-davinci[\w.-]*|"
    r"gpt-3\.5-turbo-0301|gpt-4-0314|gpt-4-32k-0314)\b",
    re.IGNORECASE,
)

# Hard-coded provider API keys.
API_KEY_LITERAL = re.compile(
    r"""(['"])(sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9]{20,}|sk-proj-[A-Za-z0-9_\-]{8,})\1"""
)

# Identifier word-parts that suggest untrusted user input.
USER_INPUT_WORDS = {
    "user", "users", "input", "inputs", "query", "queries", "question",
    "questions", "message", "messages", "msg", "comment", "comments", "body",
    "payload", "request", "req", "params", "search", "prompt", "submission",
}
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WORD_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")


def _has_user_hint(s: str) -> bool:
    """True if any identifier in ``s`` contains a user-input word part,
    splitting on both snake_case and camelCase (e.g. user_question, userMessage)."""
    for ident in _IDENT.findall(s):
        for part in _WORD_PART.findall(ident):
            if part.lower() in USER_INPUT_WORDS:
                return True
    return False

# The SDK methods we treat as a generation call.
CALL_OPENER = re.compile(
    r"\.(?:beta\.)?(?:chat\.)?(?:messages|completions|responses)\.(?:create|stream|parse)\b"
)


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #


@dataclass
class Finding:
    rule: str
    severity: str
    title: str
    detail: str
    fix: str
    file: str = ""
    line: int = 0
    snippet: str = ""

    def sort_key(self) -> tuple:
        return (-SEVERITY_ORDER[self.severity], self.file, self.line, self.rule)


def _snippet(text: str, limit: int = 140) -> str:
    s = re.sub(r"\s+", " ", text).strip()
    return s if len(s) <= limit else s[: limit - 3] + "..."


# --------------------------------------------------------------------------- #
# Source scanning helpers
# --------------------------------------------------------------------------- #


def line_starts(text: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def line_of(starts: list[int], offset: int) -> int:
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _skip_string(text: str, i: int) -> int:
    """Given i at a quote char, return the index just past the string literal.
    Handles Python triple-quotes and JS template literals (treated as opaque)."""
    n = len(text)
    q = text[i]
    # Python triple-quoted strings.
    if q in "\"'" and text[i : i + 3] in ('"""', "'''"):
        triple = text[i : i + 3]
        j = i + 3
        while j < n:
            if text[j] == "\\":
                j += 2
                continue
            if text[j : j + 3] == triple:
                return j + 3
            j += 1
        return n
    j = i + 1
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == q:
            return j + 1
        j += 1
    return n


def extract_call_args(text: str, paren_idx: int) -> tuple[str, int]:
    """paren_idx points at '('. Return (inner_args_text, index_after_close)."""
    n = len(text)
    depth = 0
    i = paren_idx
    while i < n:
        c = text[i]
        if c in "\"'`":
            i = _skip_string(text, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return text[paren_idx + 1 : i], i + 1
        i += 1
    return text[paren_idx + 1 :], n


@dataclass
class Call:
    args: str
    offset: int
    line: int
    method: str
    streaming: bool


def find_calls(text: str, starts: list[int]) -> list[Call]:
    calls: list[Call] = []
    for m in CALL_OPENER.finditer(text):
        # find the opening paren after the method name
        j = m.end()
        while j < len(text) and text[j] in " \t":
            j += 1
        if j >= len(text) or text[j] != "(":
            continue
        args, _ = extract_call_args(text, j)
        method = m.group(0)
        streaming = method.endswith(".stream") or bool(
            re.search(r"\bstream\s*[:=]\s*(?:True|true)\b", args)
        )
        calls.append(
            Call(args=args, offset=m.start(), line=line_of(starts, m.start()),
                 method=method, streaming=streaming)
        )
    return calls


def find_model(args: str) -> str | None:
    m = re.search(r"""\bmodel\s*[:=]\s*(['"`])([^'"`]+)\1""", args)
    return m.group(2) if m else None


def find_max_tokens(args: str) -> int | None:
    m = re.search(r"\bmax_tokens\s*[:=]\s*(\d[\d_]*)", args)
    if m:
        return int(m.group(1).replace("_", ""))
    return None


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def rules_for_call(call: Call, file: str) -> Iterable[Finding]:
    args = call.args
    model = find_model(args)
    snip = _snippet(call.method + "(" + args + ")")

    def f(rule, sev, title, detail, fix):
        return Finding(rule=rule, severity=sev, title=title, detail=detail,
                       fix=fix, file=file, line=call.line, snippet=snip)

    is_anthropic = ".messages." in call.method or ".beta.messages." in call.method
    is_completion = ".completions." in call.method or ".responses." in call.method

    # --- model hygiene ---
    if model:
        if ANTHROPIC_RETIRED.search(model):
            yield f("MOD001", "critical", "Retired Anthropic model — returns 404",
                    f"`{model}` has been retired and the API responds with a 404. "
                    "Any request using it fails outright.",
                    "Switch to a current model: `claude-opus-4-8` (most capable), "
                    "`claude-sonnet-4-6` (balanced), or `claude-haiku-4-5` (fast).")
        elif ANTHROPIC_DATE_SUFFIXED_ALIAS.search(model):
            yield f("MOD003", "medium", "Date suffix appended to a current alias",
                    f"`{model}` adds a date snapshot to an alias that is complete on "
                    "its own. The dated form may not resolve and can 404.",
                    "Use the bare alias (e.g. `claude-opus-4-8`) with no date suffix.")
        elif ANTHROPIC_DEPRECATED.search(model):
            yield f("MOD002", "medium", "Deprecated Anthropic model — retiring soon",
                    f"`{model}` still works but is deprecated and scheduled for "
                    "retirement; pin to a current model before it 404s.",
                    "Migrate to `claude-opus-4-8` / `claude-sonnet-4-6` / "
                    "`claude-haiku-4-5`.")
        elif OPENAI_LEGACY.search(model):
            yield f("MOD002", "medium", "Legacy/retired model family",
                    f"`{model}` is a legacy model family that has been deprecated or "
                    "retired by the provider.",
                    "Move to the provider's current-generation chat model.")

    # --- API drift (Anthropic, current) ---
    if "budget_tokens" in args:
        if model and ANTHROPIC_NO_SAMPLING.search(model):
            yield f("API001", "high", "budget_tokens removed on this model (400)",
                    f"`thinking.budget_tokens` is removed on `{model}` and returns a "
                    "400. Fixed thinking budgets are gone in favor of adaptive thinking.",
                    "Use `thinking={'type': 'adaptive'}` and control depth with "
                    "`output_config={'effort': 'high'}` (low|medium|high|max).")
        elif model and ANTHROPIC_BUDGET_DEPRECATED.search(model):
            yield f("API001", "medium", "budget_tokens deprecated on this model",
                    f"`budget_tokens` is deprecated on `{model}`; adaptive thinking is "
                    "the supported path.",
                    "Use `thinking={'type': 'adaptive'}` plus `output_config.effort`.")
        elif is_anthropic:
            yield f("API001", "medium", "budget_tokens is legacy thinking config",
                    "Fixed `budget_tokens` thinking budgets are removed on current "
                    "Opus/Fable models (400) and deprecated elsewhere.",
                    "Prefer `thinking={'type': 'adaptive'}` + `output_config.effort`.")

    if model and ANTHROPIC_NO_SAMPLING.search(model):
        for param in ("temperature", "top_p", "top_k"):
            if re.search(rf"\b{param}\s*[:=]", args):
                yield f("API002", "high", f"`{param}` removed on this model (400)",
                        f"Sampling parameters (`temperature`/`top_p`/`top_k`) are "
                        f"removed on `{model}` and return a 400 — `{param}` is set here.",
                        "Delete the sampling parameter; steer behavior with prompting "
                        "(and `output_config.effort` for depth).")
                break

    if re.search(r"\boutput_format\s*[:=]", args) and "output_config" not in args:
        yield f("API003", "medium", "Deprecated output_format parameter",
                "The top-level `output_format` parameter is deprecated API-wide.",
                "Use `output_config={'format': {...}}` (or the SDK's "
                "`messages.parse()` helper) instead.")

    # --- reliability ---
    if is_anthropic and ".parse" not in call.method and find_max_tokens(args) is None \
            and "max_tokens" not in args:
        yield f("REL001", "medium", "Anthropic call missing max_tokens",
                "The Anthropic Messages API requires `max_tokens`; omitting it errors. "
                "Even where optional, an unset cap risks runaway output and cost.",
                "Set an explicit `max_tokens` (e.g. 16000 for non-streaming, up to "
                "64000+ when streaming).")

    mt = find_max_tokens(args)
    if mt is not None and mt > 16000 and not call.streaming:
        yield f("REL002", "high", "Large max_tokens without streaming",
                f"`max_tokens={mt}` on a non-streaming request can exceed the SDK's "
                "HTTP timeout window — large outputs idle the connection until it drops "
                "(the Python SDK raises before sending).",
                "Stream the request (`.stream(...)` / `stream=True`) for large "
                "`max_tokens`, then read `.get_final_message()` / `.finalMessage()`.")

    # --- cost / cache ---
    m = re.search(r"""\bsystem\s*[:=]\s*(?:f?(['"`]))""", args)
    if m:
        # look at the system value region for volatile calls
        region = args[m.start(): m.start() + 400]
        if re.search(r"\b(datetime\.now|time\.time|Date\.now|uuid4|randomUUID|"
                     r"random\.|Math\.random)\b", region):
            yield f("COST001", "low", "Volatile value in the system prompt",
                    "A timestamp / UUID / random value rendered into the system prompt "
                    "changes the cached prefix every request, so prompt caching never "
                    "hits (cache_read stays 0).",
                    "Keep the system prompt byte-stable; move per-request values into a "
                    "later user message instead of the system prompt.")

    # silence unused
    _ = (is_completion,)


def rules_for_lines(text: str, starts: list[int], file: str) -> Iterable[Finding]:
    # SEC001: hard-coded API keys
    for m in API_KEY_LITERAL.finditer(text):
        ln = line_of(starts, m.start())
        yield Finding("SEC001", "critical", "Hard-coded API key in source",
                      "A provider API key literal is committed in source. Anyone with "
                      "repo access (or anyone the repo leaks to) can use it, and key "
                      "rotation means a code change.",
                      "Load the key from an environment variable or secrets manager; "
                      "never commit it. Rotate this key — assume it is compromised.",
                      file=file, line=ln, snippet="<redacted key literal>")

    # SEC002: untrusted input interpolated into a system prompt
    for sm in re.finditer(
        r"""\bsystem\s*[:=]\s*(f?(['"`]))""", text
    ):
        is_fstring = sm.group(1).startswith("f")
        quote = sm.group(2)
        end = _skip_string(text, sm.end(1) - 1)
        value = text[sm.end(1) - 1: end]
        interpolated = False
        if quote == "`" and "${" in value:
            interpolated = _has_user_hint(value)
        elif is_fstring and "{" in value:
            interpolated = _has_user_hint(value)
        # string concatenation: system = "..." + user_var
        tail = text[end: end + 80]
        if re.match(r"\s*\+\s*", tail) and _has_user_hint(tail):
            interpolated = True
        if interpolated:
            ln = line_of(starts, sm.start())
            yield Finding(
                "SEC002", "high", "Possible prompt-injection sink in system prompt",
                "Untrusted-looking input is interpolated into the system prompt. "
                "Content placed there carries operator authority, so a crafted input "
                "can override your instructions, and it also busts prompt caching.",
                "Keep the system prompt static. Put user-supplied text in a user "
                "message, and on Opus 4.8 deliver trusted runtime context via a "
                "`{'role': 'system'}` message in `messages[]` rather than string "
                "interpolation.",
                file=file, line=ln, snippet=_snippet(value))


# --------------------------------------------------------------------------- #
# File / directory analysis
# --------------------------------------------------------------------------- #


def analyze_text(text: str, file: str) -> list[Finding]:
    starts = line_starts(text)
    findings: list[Finding] = []
    for call in find_calls(text, starts):
        findings.extend(rules_for_call(call, file))
    findings.extend(rules_for_lines(text, starts, file))
    return findings


def analyze_file(path: str) -> list[Finding]:
    if path == "-":
        return analyze_text(sys.stdin.read(), "<stdin>")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return analyze_text(fh.read(), path)


def iter_source_files(root: str) -> Iterable[str]:
    skip_dirs = {"node_modules", ".git", ".venv", "venv", "__pycache__",
                 "dist", "build", ".next", ".mypy_cache"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in filenames:
            if name.endswith(SOURCE_EXTS):
                yield os.path.join(dirpath, name)


def collect_findings(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        if path != "-" and os.path.isdir(path):
            for fp in iter_source_files(path):
                findings.extend(analyze_file(fp))
        else:
            findings.extend(analyze_file(path))
    findings.sort(key=lambda x: x.sort_key())
    return findings


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def render_text(findings: list[Finding], threshold: str) -> str:
    if not findings:
        return "llm-app-doctor: no issues detected. ✓"
    counts: dict[str, int] = {}
    for x in findings:
        counts[x.severity] = counts.get(x.severity, 0) + 1
    summary = ", ".join(f"{counts[s]} {s}" for s in
                        ["critical", "high", "medium", "low", "info"] if counts.get(s))
    lines = [f"llm-app-doctor found {len(findings)} issue(s): {summary}\n"]
    for x in findings:
        loc = f"{x.file}:{x.line}" if x.line else x.file
        lines.append(f"[{SEVERITY_LABEL[x.severity]}] {x.rule}  {x.title}")
        lines.append(f"  at {loc}")
        if x.snippet:
            lines.append(f"  > {x.snippet}")
        lines.append(f"  why: {x.detail}")
        lines.append(f"  fix: {x.fix}")
        lines.append("")
    gate = SEVERITY_ORDER[threshold]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    if worst >= gate:
        lines.append(f"FAIL: findings at or above '{threshold}'.")
    else:
        lines.append(f"OK: no findings at or above '{threshold}' (advisory items only).")
    return "\n".join(lines)


def render_json(findings: list[Finding], threshold: str) -> str:
    gate = SEVERITY_ORDER[threshold]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    return json.dumps({
        "tool": "llm-app-doctor",
        "knowledge_date": KNOWLEDGE_DATE,
        "threshold": threshold,
        "passed": worst < gate,
        "count": len(findings),
        "findings": [asdict(x) for x in findings],
    }, indent=2)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

SELFTEST_POSITIVE = [
    ('client.messages.create(model="claude-2.1", max_tokens=10, messages=m)', "MOD001"),
    ('client.messages.create(model="claude-opus-4-8-20251101", max_tokens=10, messages=m)', "MOD003"),
    ('client.messages.create(model="claude-opus-4-1", max_tokens=10, messages=m)', "MOD002"),
    ('client.messages.create(model="claude-opus-4-8", max_tokens=10, temperature=0.7, messages=m)', "API002"),
    ('client.messages.create(model="claude-opus-4-8", max_tokens=10, thinking={"type":"enabled","budget_tokens":5000}, messages=m)', "API001"),
    ('client.messages.create(model="claude-opus-4-8", max_tokens=10, output_format=Foo, messages=m)', "API003"),
    ('client.messages.create(model="claude-opus-4-8", messages=m)', "REL001"),
    ('client.messages.create(model="claude-opus-4-8", max_tokens=128000, messages=m)', "REL002"),
    ('api_key = "sk-ant-api03-abcdefghij1234567890"', "SEC001"),
    ('resp = client.messages.create(model="claude-opus-4-8", max_tokens=10, system=f"You are {user_input}", messages=m)', "SEC002"),
    ('client.messages.create(model="claude-opus-4-8", max_tokens=10, system=f"Time is {datetime.now()}", messages=m)', "COST001"),
]

SELFTEST_NEGATIVE = [
    'client.messages.create(model="claude-opus-4-8", max_tokens=10, messages=m)',
    'client.messages.stream(model="claude-opus-4-8", max_tokens=64000, messages=m)',
    'client.messages.create(model="claude-sonnet-4-5", max_tokens=10, temperature=0.5, messages=m)',
    'key = os.environ["ANTHROPIC_API_KEY"]',
    'client.messages.create(model="claude-opus-4-8", max_tokens=10, system="You are a helpful assistant.", messages=m)',
]


def run_selftest() -> int:
    failures = 0
    for src, expected in SELFTEST_POSITIVE:
        rules = {x.rule for x in analyze_text(src, "<t>")}
        if expected not in rules:
            failures += 1
            print(f"  MISS: expected {expected} for {src!r} -> {sorted(rules)}")
    for src in SELFTEST_NEGATIVE:
        bad = [x.rule for x in analyze_text(src, "<t>")
               if x.severity in ("high", "critical")]
        if bad:
            failures += 1
            print(f"  FALSE POSITIVE: {bad} for {src!r}")
    if failures:
        print(f"selftest: {failures} failure(s)")
        return 1
    print(f"selftest: all {len(SELFTEST_POSITIVE)} positive and "
          f"{len(SELFTEST_NEGATIVE)} negative cases passed ✓")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="analyze_llm_code.py",
        description="Static auditor for LLM/AI integration code.")
    p.add_argument("paths", nargs="*", help="files or directories, or - for stdin")
    p.add_argument("--json", action="store_true", help="emit JSON output")
    p.add_argument("--fail-on", default="high", choices=list(SEVERITY_ORDER),
                   help="minimum severity that causes a non-zero exit (default: high)")
    p.add_argument("--selftest", action="store_true", help="run built-in checks")
    args = p.parse_args(argv)

    if args.selftest:
        return run_selftest()
    if not args.paths:
        p.print_usage()
        print("error: provide at least one file or directory (or - for stdin)",
              file=sys.stderr)
        return 2
    for path in args.paths:
        if path != "-" and not os.path.exists(path):
            print(f"error: path not found: {path}", file=sys.stderr)
            return 2

    findings = collect_findings(args.paths)
    if args.json:
        print(render_json(findings, args.fail_on))
    else:
        print(render_text(findings, args.fail_on))
    gate = SEVERITY_ORDER[args.fail_on]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    return 1 if worst >= gate else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

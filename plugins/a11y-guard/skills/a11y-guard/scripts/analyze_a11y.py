#!/usr/bin/env python3
"""a11y-guard: an accessibility linter for HTML and JSX/TSX markup.

Finds the accessibility defects that most often lock keyboard and screen-reader
users out of a UI:

  * images without alt text, and icon-only buttons/links with no accessible name,
  * form inputs with no associated label,
  * click handlers on non-interactive elements (div/span) with no keyboard path,
  * positive `tabindex` (breaks natural focus order),
  * `<html>` with no `lang`, and pages with no `<title>`,
  * `aria-hidden` on a focusable element (focusable but invisible to AT),
  * `<a>` used as a button with no `href`, empty/uninformative link text,
  * user-scalable=no / maximum-scale (blocks zoom), and
  * `autoplay` media without `muted`/controls.

Design goals:
  * Pure Python standard library — a tag-level regex scan (no parser deps).
  * Deterministic and CI-friendly (non-zero exit at/above --fail-on).

Usage:
    analyze_a11y.py FILE [FILE ...]      # lint files
    analyze_a11y.py DIR                   # recurse (html/jsx/tsx/vue/svelte)
    analyze_a11y.py --json FILE
    analyze_a11y.py --fail-on medium DIR
    analyze_a11y.py --selftest

Exit codes: 0 clean, 1 findings at/above threshold, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from typing import Iterable

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SEVERITY_LABEL = {k: k.upper() for k in SEVERITY_ORDER}

SOURCE_EXTS = (".html", ".htm", ".jsx", ".tsx", ".vue", ".svelte", ".astro")

TAG = re.compile(r"<\s*([A-Za-z][A-Za-z0-9._-]*)((?:[^<>\"']|\"[^\"]*\"|'[^']*'|\{[^{}]*\})*?)(/?)>",
                 re.DOTALL)
INTERACTIVE = {"a", "button", "input", "select", "textarea", "summary", "details",
               "option", "label"}
NON_INTERACTIVE = {"div", "span", "li", "td", "tr", "p", "section", "article",
                   "header", "footer", "main", "aside", "nav", "h1", "h2", "h3",
                   "h4", "h5", "h6", "img", "table", "ul", "ol"}
UNINFORMATIVE_LINK = {"click here", "here", "read more", "more", "link", "this",
                      "click", "learn more", "details"}


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


def _snip(text: str, limit: int = 140) -> str:
    s = re.sub(r"\s+", " ", text).strip()
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _attrs(attr_text: str) -> dict:
    """Parse tag attributes into {lower_name: raw_value_or_empty}."""
    out: dict[str, str] = {}
    pattern = re.compile(
        r"([A-Za-z_:@#][\w:.\-]*)\s*=\s*(\"[^\"]*\"|'[^']*'|\{[^{}]*\}|[^\s>]+)"
        r"|([A-Za-z_:@#][\w:.\-]*)")
    for m in pattern.finditer(attr_text):
        if m.group(1):
            val = m.group(2) or ""
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            out[m.group(1).lower()] = val
        elif m.group(3):
            out[m.group(3).lower()] = ""
    return out


def _truthy_jsx(val: str) -> bool:
    """A JSX attribute value that isn't obviously empty/false."""
    v = val.strip()
    if v in ("", "{}", '{""}', "{''}", "{false}", "{null}", "{undefined}"):
        return False
    return True


def _has_name(a: dict) -> bool:
    """Does this element carry an accessible name via an attribute?"""
    for key in ("aria-label", "arialabel", "aria-labelledby", "title", "alt",
                "accessiblename"):
        if key in a and _truthy_jsx(a[key]):
            return True
    return False


def _mask_comments(text: str) -> str:
    """Blank out HTML/JSX comments, preserving length and newlines.

    Without this, markup mentioned inside a comment (a very common thing in real
    files and docs) would be linted as if it were live.
    """
    def blank(m: re.Match) -> str:
        return "".join("\n" if c == "\n" else " " for c in m.group(0))

    text = re.sub(r"<!--.*?-->", blank, text, flags=re.DOTALL)      # HTML
    text = re.sub(r"\{\s*/\*.*?\*/\s*\}", blank, text, flags=re.DOTALL)  # JSX {/* */}
    return text


def analyze_text(text: str, file: str) -> list[Finding]:
    findings: list[Finding] = []
    is_html = file.endswith((".html", ".htm"))
    text = _mask_comments(text)
    lower_all = text.lower()

    def line_of(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    def add(rule, sev, title, detail, fix, pos, snippet=""):
        findings.append(Finding(rule, sev, title, detail, fix, file,
                                line_of(pos), snippet))

    seen_html_tag = False

    for m in TAG.finditer(text):
        raw_name = m.group(1)
        name = raw_name.lower()
        attr_text = m.group(2) or ""
        a = _attrs(attr_text)
        snippet = _snip(m.group(0))
        pos = m.start()
        # Skip closing-ish and component tags for element-specific rules.
        is_component = raw_name[0].isupper()

        # A11Y001: img without alt
        if name == "img" and not is_component:
            if "alt" not in a and "aria-label" not in a and "aria-labelledby" not in a:
                add("A11Y001", "high", "Image without alt text",
                    "Screen readers announce the file name (or nothing) for an image "
                    "with no `alt`, so its meaning is lost.",
                    'Add descriptive `alt="..."`, or `alt=""` if the image is purely '
                    "decorative (which correctly hides it from assistive tech).",
                    pos, snippet)

        # A11Y002: interactive element with no accessible name
        if name in ("button", "a") and not is_component:
            # Find the element's inner text up to its closing tag.
            close = re.search(rf"</\s*{re.escape(raw_name)}\s*>", text[m.end():],
                              re.IGNORECASE)
            inner = text[m.end(): m.end() + close.start()] if close else ""
            inner_text = re.sub(r"<[^>]*>", " ", inner)
            inner_text = re.sub(r"\{[^{}]*\}", " ", inner_text)  # JSX expressions
            has_text = bool(inner_text.strip())
            if not has_text and not _has_name(a):
                add("A11Y002", "high",
                    f"<{name}> has no accessible name",
                    "An icon-only control with no text and no `aria-label` is "
                    "announced as just \"button\" or \"link\", so its purpose is "
                    "unknowable without sight.",
                    'Add `aria-label="Close dialog"` (or visible text, or visually '
                    "hidden text). If it wraps an icon, label the control, not the icon.",
                    pos, snippet)

        # A11Y003: click handler on a non-interactive element
        if name in NON_INTERACTIVE and not is_component:
            has_click = any(k in a for k in ("onclick", "on:click", "v-on:click", "@click"))
            if has_click:
                has_key = any(k in a for k in
                              ("onkeydown", "onkeyup", "onkeypress", "on:keydown",
                               "@keydown", "v-on:keydown"))
                has_role = "role" in a and _truthy_jsx(a["role"])
                focusable = "tabindex" in a
                if not (has_key and has_role and focusable):
                    missing = []
                    if not has_role:
                        missing.append("`role`")
                    if not focusable:
                        missing.append("`tabIndex={0}`")
                    if not has_key:
                        missing.append("a keyboard handler")
                    add("A11Y003", "high",
                        f"Click handler on non-interactive <{name}>",
                        "A click handler on a div/span is unreachable by keyboard and "
                        "invisible to assistive tech: it can't be focused, activated "
                        f"with Enter/Space, or announced as a control. Missing: "
                        f"{', '.join(missing)}.",
                        "Use a real `<button>` (best — you get focus, Enter/Space, and "
                        'the right role for free). If you must keep the element, add '
                        '`role="button"`, `tabIndex={0}`, and an Enter/Space key handler.',
                        pos, snippet)

        # A11Y004: positive tabindex
        if "tabindex" in a:
            tv = a["tabindex"].strip().strip("{}").strip("\"'")
            try:
                if int(tv) > 0:
                    add("A11Y004", "medium", "Positive tabindex",
                        f"`tabindex={tv}` pulls the element out of document order into "
                        "a manual tab sequence, which is fragile and usually surprises "
                        "keyboard users.",
                        "Use `tabindex=\"0\"` (focusable, natural order) or `-1` "
                        "(programmatically focusable only), and fix the order by "
                        "reordering the DOM instead.",
                        pos, snippet)
            except ValueError:
                pass

        # A11Y005: form control without a label
        if name in ("input", "select", "textarea") and not is_component:
            input_type = (a.get("type") or "").strip().strip("{}\"'").lower()
            if input_type not in ("hidden", "submit", "button", "reset", "image"):
                labelled = _has_name(a) or "id" in a  # id may pair with <label for>
                if "id" in a and _truthy_jsx(a["id"]):
                    idv = a["id"].strip().strip("{}\"'")
                    if idv and f'for="{idv}"' not in text and \
                            f"for='{idv}'" not in text and \
                            f"htmlFor=\"{idv}\"" not in text and \
                            f"htmlFor='{idv}'" not in text:
                        labelled = _has_name(a)
                if not labelled:
                    add("A11Y005", "high",
                        f"<{name}> has no associated label",
                        "Without a label, a screen reader announces only the field "
                        "type, so users can't tell what to enter. Placeholder text is "
                        "not a label — it disappears on input and is often unreadable.",
                        "Associate a `<label for=\"id\">` (React: `htmlFor`) with the "
                        "control, wrap the control in a `<label>`, or add "
                        "`aria-label`/`aria-labelledby`.",
                        pos, snippet)

        # A11Y006: aria-hidden on a focusable element
        if "aria-hidden" in a and _truthy_jsx(a["aria-hidden"]) and \
                a["aria-hidden"].strip().strip("{}\"'").lower() not in ("false",):
            tv = (a.get("tabindex") or "").strip().strip("{}\"'")
            focusable_tag = name in INTERACTIVE and name != "label"
            positive_tab = tv not in ("", "-1")
            if focusable_tag or positive_tab:
                add("A11Y006", "high", "aria-hidden on a focusable element",
                    "The element is still keyboard-focusable but hidden from "
                    "assistive tech, so a screen-reader user can focus something that "
                    "announces nothing — a classic 'ghost focus' trap.",
                    "Remove `aria-hidden`, or make the element unfocusable too "
                    "(`tabindex=\"-1\"` plus `disabled` where applicable).",
                    pos, snippet)

        # A11Y007: <a> without href used as a control
        if name == "a" and not is_component and "href" not in a:
            add("A11Y007", "medium", "<a> without href",
                "An anchor with no `href` is not focusable or activatable by keyboard "
                "and is not announced as a link or a button.",
                "If it navigates, give it a real `href`. If it performs an action, use "
                "`<button>` instead.",
                pos, snippet)

        # A11Y009: viewport blocks zoom
        if name == "meta" and (a.get("name", "").lower() == "viewport"):
            content = (a.get("content") or "").lower()
            if "user-scalable=no" in content or re.search(r"maximum-scale\s*=\s*1", content):
                add("A11Y009", "medium", "Viewport prevents zooming",
                    "`user-scalable=no` / `maximum-scale=1` blocks pinch-zoom, which "
                    "low-vision users rely on to read content.",
                    "Drop those parameters: "
                    '`<meta name="viewport" content="width=device-width, initial-scale=1">`.',
                    pos, snippet)

        # A11Y010: autoplaying media without muted
        if name in ("video", "audio") and "autoplay" in a:
            if "muted" not in a:
                add("A11Y010", "medium", "Autoplaying media is not muted",
                    "Audio that starts on its own can drown out a screen reader and is "
                    "disorienting for everyone.",
                    "Add `muted` (and `controls`) so playback can't hijack audio, or "
                    "don't autoplay.",
                    pos, snippet)

        if name == "html":
            seen_html_tag = True
            if "lang" not in a or not _truthy_jsx(a.get("lang", "")):
                add("A11Y008", "high", "<html> has no lang attribute",
                    "Without `lang`, screen readers guess the language and may read "
                    "the page with the wrong pronunciation rules, which can make it "
                    "unintelligible.",
                    'Add the page language: `<html lang="en">`.',
                    pos, snippet)

    # A11Y011: uninformative link text (scan link bodies)
    for lm in re.finditer(r"<\s*a\b[^>]*>(.*?)</\s*a\s*>", text, re.DOTALL | re.IGNORECASE):
        body = re.sub(r"<[^>]*>", " ", lm.group(1))
        body = re.sub(r"\{[^{}]*\}", " ", body)
        txt = re.sub(r"\s+", " ", body).strip().lower().rstrip(".!?›»>→ ")
        if txt in UNINFORMATIVE_LINK:
            add("A11Y011", "low", "Uninformative link text",
                f'Link text "{txt}" gives no destination. Screen-reader users often '
                "navigate by pulling up a list of links out of context, where "
                '"click here" is meaningless.',
                "Make the link text describe its destination — e.g. "
                '"View the pricing page" instead of "click here".',
                lm.start(), _snip(lm.group(0)))

    # A11Y012: HTML document with no <title>
    if is_html and "<head" in lower_all and not re.search(r"<\s*title\s*>", lower_all):
        add("A11Y012", "medium", "Document has no <title>",
            "The title is the first thing announced on page load and is how users "
            "distinguish tabs and history entries.",
            "Add a unique, descriptive `<title>` in `<head>`.", 0, "<head> …")

    # A11Y008 also applies when an HTML file has no <html lang> at all.
    if is_html and not seen_html_tag and "<body" in lower_all:
        add("A11Y008", "high", "No <html lang> declared",
            "The document has no `<html>` element with a `lang` attribute, so "
            "assistive tech must guess the language.",
            'Wrap the document in `<html lang="en">`.', 0, "")

    findings.sort(key=lambda x: x.sort_key())
    return findings


# --------------------------------------------------------------------------- #
# File / directory handling
# --------------------------------------------------------------------------- #


def analyze_file(path: str) -> list[Finding]:
    if path == "-":
        return analyze_text(sys.stdin.read(), "<stdin>")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return analyze_text(fh.read(), path)


def iter_sources(root: str) -> Iterable[str]:
    skip = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist",
            "build", ".next", "coverage", "vendor"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in sorted(filenames):
            if name.endswith(SOURCE_EXTS):
                yield os.path.join(dirpath, name)


def collect_findings(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        if path != "-" and os.path.isdir(path):
            for fp in iter_sources(path):
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
        return "a11y-guard: no accessibility issues detected. ✓"
    counts: dict[str, int] = {}
    for x in findings:
        counts[x.severity] = counts.get(x.severity, 0) + 1
    summary = ", ".join(f"{counts[s]} {s}" for s in
                        ["critical", "high", "medium", "low", "info"] if counts.get(s))
    out = [f"a11y-guard found {len(findings)} issue(s): {summary}\n"]
    for x in findings:
        loc = f"{x.file}:{x.line}" if x.line else x.file
        out.append(f"[{SEVERITY_LABEL[x.severity]}] {x.rule}  {x.title}")
        out.append(f"  at {loc}")
        if x.snippet:
            out.append(f"  > {x.snippet}")
        out.append(f"  why: {x.detail}")
        out.append(f"  fix: {x.fix}")
        out.append("")
    gate = SEVERITY_ORDER[threshold]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    out.append(f"FAIL: findings at or above '{threshold}'." if worst >= gate
               else f"OK: no findings at or above '{threshold}' (advisory items only).")
    return "\n".join(out)


def render_json(findings: list[Finding], threshold: str) -> str:
    gate = SEVERITY_ORDER[threshold]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    return json.dumps({
        "tool": "a11y-guard",
        "threshold": threshold,
        "passed": worst < gate,
        "count": len(findings),
        "findings": [asdict(x) for x in findings],
    }, indent=2)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

SELFTEST_POSITIVE = [
    ('<img src="a.png">', "a.html", "A11Y001"),
    ('<button><svg /></button>', "a.jsx", "A11Y002"),
    ('<div onClick={go}>Save</div>', "a.jsx", "A11Y003"),
    ('<span tabindex="3">x</span>', "a.html", "A11Y004"),
    ('<input type="text" name="q">', "a.html", "A11Y005"),
    ('<button aria-hidden="true">Go</button>', "a.html", "A11Y006"),
    ('<a onClick={go}>Open</a>', "a.jsx", "A11Y007"),
    ('<html><body>x</body></html>', "a.html", "A11Y008"),
    ('<meta name="viewport" content="width=device-width, user-scalable=no">',
     "a.html", "A11Y009"),
    ('<video autoplay src="v.mp4"></video>', "a.html", "A11Y010"),
    ('<a href="/pricing">click here</a>', "a.html", "A11Y011"),
    ('<html lang="en"><head><meta charset="utf-8"></head><body>x</body></html>',
     "a.html", "A11Y012"),
]

SELFTEST_NEGATIVE = [
    ('<img src="a.png" alt="Team photo">', "a.html"),
    ('<img src="d.png" alt="">', "a.html"),
    ('<button aria-label="Close dialog"><svg /></button>', "a.jsx"),
    ('<button onClick={go}>Save</button>', "a.jsx"),
    ('<div role="button" tabIndex={0} onClick={go} onKeyDown={key}>Save</div>', "a.jsx"),
    ('<label for="q">Query</label><input id="q" type="text">', "a.html"),
    ('<input type="text" aria-label="Search">', "a.html"),
    ('<a href="/pricing">View the pricing page</a>', "a.html"),
    ('<span tabindex="0">x</span>', "a.html"),
    ('<span tabindex="-1">x</span>', "a.html"),
    ('<video autoplay muted controls src="v.mp4"></video>', "a.html"),
    ('<html lang="en"><head><title>Home</title></head><body>ok</body></html>', "a.html"),
]


def run_selftest() -> int:
    failures = 0
    for src, fname, expected in SELFTEST_POSITIVE:
        rules = {x.rule for x in analyze_text(src, fname)}
        if expected not in rules:
            failures += 1
            print(f"  MISS: expected {expected} for {src!r} -> {sorted(rules)}")
    for src, fname in SELFTEST_NEGATIVE:
        bad = [x.rule for x in analyze_text(src, fname)
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


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="analyze_a11y.py",
        description="Accessibility linter for HTML and JSX/TSX markup.")
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
        print("error: provide a file or directory (or - for stdin)", file=sys.stderr)
        return 2
    for path in args.paths:
        if path != "-" and not os.path.exists(path):
            print(f"error: path not found: {path}", file=sys.stderr)
            return 2

    findings = collect_findings(args.paths)
    print(render_json(findings, args.fail_on) if args.json
          else render_text(findings, args.fail_on))
    gate = SEVERITY_ORDER[args.fail_on]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    return 1 if worst >= gate else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

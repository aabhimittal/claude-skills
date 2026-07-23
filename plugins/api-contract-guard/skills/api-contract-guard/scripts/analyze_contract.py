#!/usr/bin/env python3
"""api-contract-guard: detect breaking changes between two API schema versions.

Diffs an OLD and a NEW schema and reports changes that would break existing
clients — so a breaking API change gets caught in code review instead of in
production. Supports:

  * OpenAPI / Swagger (JSON): removed paths/operations, newly-required
    parameters, parameters that became required, removed required parameters,
    and newly-required request-body fields.
  * GraphQL SDL (.graphql): removed types/fields/enum values, changed field
    types, newly-required arguments, and arguments that became required.

Additive changes (new endpoints, new optional fields, new enum values) are
reported as `info` so the diff is complete but does not fail CI.

Design goals:
  * Pure Python standard library (json only; a small hand-rolled SDL reader).
  * Deterministic and CI-friendly (non-zero exit at/above --fail-on).

Usage:
    analyze_contract.py OLD NEW          # two schema files (same format)
    analyze_contract.py --json OLD NEW
    analyze_contract.py --fail-on high OLD NEW
    analyze_contract.py --selftest

Exit codes: 0 no breaking change at/above threshold, 1 breaking change found,
2 usage error (missing file, format mismatch).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from typing import Iterable

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SEVERITY_LABEL = {k: k.upper() for k in SEVERITY_ORDER}
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


@dataclass
class Finding:
    rule: str
    severity: str
    title: str
    where: str
    detail: str
    fix: str

    def sort_key(self) -> tuple:
        return (-SEVERITY_ORDER[self.severity], self.where, self.rule)


# --------------------------------------------------------------------------- #
# OpenAPI (JSON) diff
# --------------------------------------------------------------------------- #


def _params(op: dict) -> dict:
    out = {}
    for p in op.get("parameters", []) or []:
        if isinstance(p, dict) and "name" in p:
            out[(p["name"], p.get("in", "query"))] = bool(p.get("required", False))
    return out


def _body_required(op: dict) -> set:
    rb = op.get("requestBody", {})
    req: set = set()
    for _mt, media in (rb.get("content", {}) or {}).items():
        schema = media.get("schema", {}) if isinstance(media, dict) else {}
        for r in schema.get("required", []) or []:
            req.add(r)
    return req


def diff_openapi(old: dict, new: dict) -> list[Finding]:
    f: list[Finding] = []
    old_paths = old.get("paths", {}) or {}
    new_paths = new.get("paths", {}) or {}

    for path in old_paths:
        if path not in new_paths:
            f.append(Finding(
                "OAS001", "high", "Removed endpoint", path,
                "The path no longer exists; clients calling it get a 404.",
                "Restore the path, or deprecate it for a release before removal."))
            continue
        old_ops = {m: op for m, op in old_paths[path].items()
                   if m.lower() in HTTP_METHODS and isinstance(op, dict)}
        new_ops = {m: op for m, op in new_paths[path].items()
                   if m.lower() in HTTP_METHODS and isinstance(op, dict)}
        for method in old_ops:
            if method not in new_ops:
                f.append(Finding(
                    "OAS002", "high", "Removed operation", f"{method.upper()} {path}",
                    "The HTTP method was removed from an existing path; clients "
                    "using it break.",
                    "Keep the operation, or deprecate it before removal."))
                continue
            op_old, op_new = old_ops[method], new_ops[method]
            po, pn = _params(op_old), _params(op_new)
            for key, req in pn.items():
                if key not in po and req:
                    f.append(Finding(
                        "OAS003", "high", "New required parameter",
                        f"{method.upper()} {path} ({key[0]} in {key[1]})",
                        "A new required parameter means existing clients that omit "
                        "it now get rejected.",
                        "Make the parameter optional, or version the endpoint."))
            for key, req in po.items():
                if key in pn and not req and pn[key]:
                    f.append(Finding(
                        "OAS004", "high", "Parameter became required",
                        f"{method.upper()} {path} ({key[0]} in {key[1]})",
                        "A previously optional parameter is now required; clients "
                        "that don't send it break.",
                        "Keep it optional, or version the endpoint."))
                if key not in pn and req:
                    f.append(Finding(
                        "OAS005", "medium", "Removed required parameter",
                        f"{method.upper()} {path} ({key[0]} in {key[1]})",
                        "Removing a required parameter can change behavior for "
                        "clients that still send it and for server-side logic.",
                        "Confirm no client/server depends on it before removing."))
            bo, bn = _body_required(op_old), _body_required(op_new)
            for field in bn - bo:
                f.append(Finding(
                    "OAS006", "high", "New required request-body field",
                    f"{method.upper()} {path} (body.{field})",
                    "A newly-required body field rejects requests from existing "
                    "clients that don't include it.",
                    "Make the field optional, or version the request schema."))

    for path in new_paths:
        if path not in old_paths:
            f.append(Finding(
                "OAS100", "info", "New endpoint (additive)", path,
                "A new path was added — non-breaking.", "No action needed."))
    return f


# --------------------------------------------------------------------------- #
# GraphQL SDL reader + diff
# --------------------------------------------------------------------------- #

DEF = re.compile(r"\b(type|input|interface|enum|scalar|union)\s+([A-Za-z_]\w*)")
TYPE_TOKEN = re.compile(r"[\[\]\w!]+")


def _strip_sdl(text: str) -> str:
    text = re.sub(r'"""(?:.|\n)*?"""', " ", text)          # block descriptions
    text = re.sub(r'"(?:[^"\\]|\\.)*"', " ", text)         # inline strings
    text = re.sub(r"#[^\n]*", " ", text)                    # comments
    return text


def _match_braces(text: str, open_idx: int) -> tuple[str, int]:
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i], i + 1
        i += 1
    return text[open_idx + 1:], n


def _split_top(s: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _parse_args(s: str) -> dict:
    args = {}
    for part in _split_top(s):
        m = re.match(r"\s*([A-Za-z_]\w*)\s*:\s*([\[\]\w!]+)\s*(=\s*(.+))?", part, re.S)
        if m:
            typ = m.group(2)
            has_default = m.group(3) is not None
            args[m.group(1)] = {"type": typ,
                                "required": typ.endswith("!") and not has_default}
    return args


def _parse_fields(body: str) -> dict:
    fields = {}
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c.isspace() or c == ",":
            i += 1
            continue
        if c == "@":  # field/arg directive — skip its name and optional (...) args
            i += 1
            dm = re.match(r"[A-Za-z_]\w*", body[i:])
            if dm:
                i += len(dm.group(0))
            while i < n and body[i].isspace():
                i += 1
            if i < n and body[i] == "(":
                _seg, i = _match_braces_generic(body, i, "(", ")")
            continue
        nm = re.match(r"[A-Za-z_]\w*", body[i:])
        if not nm:
            i += 1
            continue
        name = nm.group(0)
        i += len(name)
        while i < n and body[i].isspace():
            i += 1
        args = {}
        if i < n and body[i] == "(":
            seg, i = _match_braces_generic(body, i, "(", ")")
            args = _parse_args(seg)
            while i < n and body[i].isspace():
                i += 1
        if i < n and body[i] == ":":
            i += 1
            while i < n and body[i].isspace():
                i += 1
            tm = TYPE_TOKEN.match(body[i:])
            ftype = tm.group(0) if tm else ""
            i += len(ftype)
            fields[name] = {"type": ftype, "args": args}
        # keep scanning; the loop skips separators and directives on its own
    return fields


def _match_braces_generic(text: str, open_idx: int, op: str, cl: str) -> tuple[str, int]:
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        if text[i] == op:
            depth += 1
        elif text[i] == cl:
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i], i + 1
        i += 1
    return text[open_idx + 1:], n


def parse_sdl(text: str) -> dict:
    text = _strip_sdl(text)
    types: dict = {}
    for m in DEF.finditer(text):
        kind, name = m.group(1), m.group(2)
        entry = {"kind": kind, "fields": {}, "values": set()}
        rest = text[m.end():]
        if kind == "scalar":
            types[name] = entry
            continue
        if kind == "union":
            nl = rest.find("\n")
            decl = rest[: nl if nl != -1 else len(rest)]
            members = re.findall(r"[A-Za-z_]\w*", decl.split("=", 1)[-1]) \
                if "=" in decl else []
            entry["values"] = set(members)
            types[name] = entry
            continue
        brace = text.find("{", m.end())
        if brace == -1:
            types[name] = entry
            continue
        body, _ = _match_braces(text, brace)
        if kind == "enum":
            clean = re.sub(r"@\w+(\([^)]*\))?", " ", body)  # drop directives
            entry["values"] = set(re.findall(r"[A-Za-z_]\w*", clean))
        else:
            entry["fields"] = _parse_fields(body)
        types[name] = entry
    return types


def diff_sdl(old: dict, new: dict) -> list[Finding]:
    f: list[Finding] = []
    for name, t in old.items():
        if name not in new:
            f.append(Finding("GQL001", "high", "Removed type", name,
                             "The type was deleted; any query/field referencing it "
                             "breaks.", "Deprecate before removing, or restore it."))
            continue
        nt = new[name]
        if nt["kind"] != t["kind"]:
            f.append(Finding("GQL002", "high", "Type kind changed",
                             f"{name} ({t['kind']} -> {nt['kind']})",
                             "Changing a type's kind is incompatible with clients "
                             "using the old shape.", "Introduce a new type instead."))
            continue
        if t["kind"] in ("enum", "union"):
            for v in t["values"] - nt["values"]:
                f.append(Finding("GQL003", "high", "Removed enum/union member",
                                 f"{name}.{v}",
                                 "Clients that send or match this value break.",
                                 "Keep the member, or deprecate it first."))
            continue
        for fname, fld in t["fields"].items():
            if fname not in nt["fields"]:
                f.append(Finding("GQL004", "high", "Removed field",
                                 f"{name}.{fname}",
                                 "Clients selecting this field get a validation "
                                 "error.", "Deprecate with @deprecated before removal."))
                continue
            nf = nt["fields"][fname]
            if fld["type"] != nf["type"]:
                f.append(Finding("GQL005", "high", "Field type changed",
                                 f"{name}.{fname} ({fld['type']} -> {nf['type']})",
                                 "A changed field type can break client "
                                 "deserialization or non-null expectations.",
                                 "Add a new field instead of changing this one."))
            for aname, arg in nf["args"].items():
                if aname not in fld["args"] and arg["required"]:
                    f.append(Finding("GQL006", "high", "New required argument",
                                     f"{name}.{fname}({aname}:)",
                                     "A new non-null argument without a default "
                                     "rejects existing queries that omit it.",
                                     "Make it nullable or give it a default value."))
            for aname, arg in fld["args"].items():
                if aname not in nf["args"]:
                    f.append(Finding("GQL007", "medium", "Removed argument",
                                     f"{name}.{fname}({aname}:)",
                                     "Clients passing this argument get a validation "
                                     "error.", "Deprecate the argument before removal."))
                elif not arg["required"] and nf["args"][aname]["required"]:
                    f.append(Finding("GQL008", "high", "Argument became required",
                                     f"{name}.{fname}({aname}:)",
                                     "A previously optional argument is now required.",
                                     "Keep it optional or provide a default."))
    for name in new:
        if name not in old:
            f.append(Finding("GQL100", "info", "New type (additive)", name,
                             "A new type was added — non-breaking.", "No action needed."))
    return f


# --------------------------------------------------------------------------- #
# Format detection + orchestration
# --------------------------------------------------------------------------- #


def detect_and_diff(old_text: str, new_text: str) -> tuple[str, list[Finding]]:
    def as_openapi(t: str):
        try:
            d = json.loads(t)
        except json.JSONDecodeError:
            return None
        if isinstance(d, dict) and ("openapi" in d or "swagger" in d or "paths" in d):
            return d
        return None

    o, nw = as_openapi(old_text), as_openapi(new_text)
    if o is not None and nw is not None:
        return "openapi", sorted(diff_openapi(o, nw), key=lambda x: x.sort_key())
    if o is not None or nw is not None:
        raise ValueError("format mismatch: one file looks like OpenAPI JSON, the "
                         "other does not")
    return "graphql", sorted(diff_sdl(parse_sdl(old_text), parse_sdl(new_text)),
                             key=lambda x: x.sort_key())


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def render_text(fmt: str, findings: list[Finding], threshold: str) -> str:
    breaking = [x for x in findings if x.severity != "info"]
    if not breaking:
        extra = f" ({len(findings)} additive change(s) noted)" if findings else ""
        return f"api-contract-guard [{fmt}]: no breaking changes. ✓{extra}"
    counts: dict[str, int] = {}
    for x in findings:
        counts[x.severity] = counts.get(x.severity, 0) + 1
    summary = ", ".join(f"{counts[s]} {s}" for s in
                        ["critical", "high", "medium", "low", "info"] if counts.get(s))
    out = [f"api-contract-guard [{fmt}] found {len(findings)} change(s): {summary}\n"]
    for x in findings:
        out.append(f"[{SEVERITY_LABEL[x.severity]}] {x.rule}  {x.title}")
        out.append(f"  at {x.where}")
        out.append(f"  why: {x.detail}")
        out.append(f"  fix: {x.fix}")
        out.append("")
    gate = SEVERITY_ORDER[threshold]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    out.append(f"FAIL: breaking changes at or above '{threshold}'." if worst >= gate
               else f"OK: no changes at or above '{threshold}'.")
    return "\n".join(out)


def render_json(fmt: str, findings: list[Finding], threshold: str) -> str:
    gate = SEVERITY_ORDER[threshold]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    return json.dumps({
        "tool": "api-contract-guard",
        "format": fmt,
        "threshold": threshold,
        "passed": worst < gate,
        "count": len(findings),
        "findings": [asdict(x) for x in findings],
    }, indent=2)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #


def run_selftest() -> int:
    failures = 0

    def expect(old_t, new_t, rule, present, label):
        nonlocal failures
        _fmt, fs = detect_and_diff(old_t, new_t)
        rules = {x.rule for x in fs}
        if (rule in rules) != present:
            failures += 1
            print(f"  {label}: expected {rule} present={present}, got {sorted(rules)}")

    # OpenAPI
    oa_old = json.dumps({"openapi": "3.0.0", "paths": {
        "/users": {"get": {}, "post": {"parameters": [{"name": "role", "in": "query"}]}},
        "/legacy": {"get": {}}}})
    oa_new = json.dumps({"openapi": "3.0.0", "paths": {
        "/users": {"get": {}, "post": {"parameters": [
            {"name": "role", "in": "query", "required": True},
            {"name": "team", "in": "query", "required": True}]}},
        "/new": {"get": {}}}})
    expect(oa_old, oa_new, "OAS001", True, "removed endpoint /legacy")
    expect(oa_old, oa_new, "OAS003", True, "new required param team")
    expect(oa_old, oa_new, "OAS004", True, "param role became required")
    expect(oa_old, oa_new, "OAS100", True, "additive /new")

    # GraphQL
    gq_old = """
    type Query { user(id: ID!): User, feed: [Post!]! }
    type User { id: ID!, name: String, age: Int }
    enum Role { ADMIN MEMBER GUEST }
    """
    gq_new = """
    type Query { user(id: ID!, region: String!): User }
    type User { id: ID!, name: Int }
    enum Role { ADMIN MEMBER }
    """
    expect(gq_old, gq_new, "GQL004", True, "removed field User.age")
    expect(gq_old, gq_new, "GQL005", True, "User.name type changed")
    expect(gq_old, gq_new, "GQL006", True, "new required arg region")
    expect(gq_old, gq_new, "GQL003", True, "removed enum GUEST")
    expect(gq_old, gq_new, "GQL004", True, "removed field feed")  # feed removed

    # No-change / additive-only must NOT be breaking
    gq_same_old = "type Query { a: String }"
    gq_same_new = "type Query { a: String, b: Int }\ntype New { x: ID }"
    _fmt, fs = detect_and_diff(gq_same_old, gq_same_new)
    if any(x.severity != "info" for x in fs):
        failures += 1
        print(f"  additive-only flagged breaking: {[x.rule for x in fs]}")

    if failures:
        print(f"selftest: {failures} failure(s)")
        return 1
    print("selftest: all OpenAPI and GraphQL diff cases passed ✓")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="analyze_contract.py",
        description="Detect breaking changes between two API schema versions.")
    p.add_argument("old", nargs="?", help="old schema file")
    p.add_argument("new", nargs="?", help="new schema file")
    p.add_argument("--json", action="store_true", help="emit JSON output")
    p.add_argument("--fail-on", default="high", choices=list(SEVERITY_ORDER),
                   help="minimum severity that causes a non-zero exit (default: high)")
    p.add_argument("--selftest", action="store_true", help="run built-in checks")
    args = p.parse_args(argv)

    if args.selftest:
        return run_selftest()
    if not args.old or not args.new:
        p.print_usage()
        print("error: provide OLD and NEW schema files", file=sys.stderr)
        return 2
    try:
        old_text = _read(args.old)
        new_text = _read(args.new)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        fmt, findings = detect_and_diff(old_text, new_text)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(render_json(fmt, findings, args.fail_on) if args.json
          else render_text(fmt, findings, args.fail_on))
    gate = SEVERITY_ORDER[args.fail_on]
    worst = max((SEVERITY_ORDER[x.severity] for x in findings), default=-1)
    return 1 if worst >= gate else 0


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

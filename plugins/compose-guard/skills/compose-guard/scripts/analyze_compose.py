#!/usr/bin/env python3
"""compose-guard: a security/misconfiguration linter for Docker Compose files.

Scans docker-compose.yml for the settings that quietly hand a container the keys
to its host or leak credentials:

  * `privileged: true` and dangerous added capabilities (SYS_ADMIN, SYS_PTRACE…),
  * bind-mounting the Docker socket (container escape → root on host),
  * `network_mode: host` / `pid: host` / `ipc: host` (namespace escape),
  * mutable `:latest` (or untagged) images,
  * secrets in plain `environment:` values,
  * host root or sensitive host paths bind-mounted read-write,
  * `security_opt` disabling seccomp/AppArmor, and database ports published to
    all interfaces (0.0.0.0).

Design goals:
  * Pure Python standard library (no PyYAML) — an indentation-aware scan.
  * Deterministic and CI-friendly (non-zero exit at/above --fail-on).

Usage:
    analyze_compose.py FILE [FILE ...]      # lint compose files
    analyze_compose.py DIR                   # find docker-compose*.y*ml
    analyze_compose.py --json FILE
    analyze_compose.py --fail-on medium DIR
    analyze_compose.py --selftest

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

DANGEROUS_CAPS = {
    "sys_admin", "sys_ptrace", "sys_module", "dac_read_search", "dac_override",
    "sys_boot", "net_admin", "all",
}
SENSITIVE_HOST_PATHS = (
    "/", "/etc", "/var/run", "/run", "/proc", "/sys", "/boot", "/dev",
    "/var/lib/docker", "/root", "/usr", "/var/log",
)
SECRET_KEY = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"credential|client[_-]?secret)",
    re.IGNORECASE,
)
PLACEHOLDER_VALUE = re.compile(
    r"^\s*(?:|x{2,}|changeme|change[-_ ]?me|your[-_ ]?[\w-]*|<[^>]*>|placeholder|"
    r"example|sample|todo|tbd|none|null|\$\{[^}]*\}|\$\w+|[\w-]*here)\s*$",
    re.IGNORECASE,
)
DB_PORTS = {
    "3306": "MySQL", "5432": "PostgreSQL", "27017": "MongoDB",
    "6379": "Redis", "9200": "Elasticsearch", "5984": "CouchDB",
    "11211": "Memcached", "1433": "SQL Server", "2379": "etcd",
    "9042": "Cassandra", "5672": "RabbitMQ", "8086": "InfluxDB",
}


@dataclass
class Finding:
    rule: str
    severity: str
    title: str
    detail: str
    fix: str
    file: str = ""
    line: int = 0
    service: str = ""
    snippet: str = ""

    def sort_key(self) -> tuple:
        return (-SEVERITY_ORDER[self.severity], self.file, self.line, self.rule)


def _snip(text: str, limit: int = 140) -> str:
    s = re.sub(r"\s+", " ", text).strip()
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _strip_inline_comment(s: str) -> str:
    """Drop a trailing ` # comment`, respecting quotes.

    Compose files routinely carry inline comments; without this, patterns
    anchored with `$` (e.g. `privileged: true`) silently stop matching.
    """
    out = []
    quote = None
    for idx, ch in enumerate(s):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (idx == 0 or s[idx - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _service_of(lines: list[str], idx: int) -> str:
    """Walk back to find the enclosing service name under `services:`."""
    target = None
    svc_indent = None
    for j in range(idx, -1, -1):
        line = lines[j]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        ind = _indent(line)
        m = re.match(r"\s*([A-Za-z0-9_.-]+):\s*$", line)
        if m and svc_indent is None and ind < _indent(lines[idx]):
            target = m.group(1)
            svc_indent = ind
        if re.match(r"\s*services:\s*$", line):
            if target and svc_indent is not None and svc_indent > ind:
                return target
            return target or ""
    return target or ""


def analyze_text(text: str, file: str) -> list[Finding]:
    lines = text.split("\n")
    findings: list[Finding] = []

    def add(rule, sev, title, detail, fix, lineno, svc="", snippet=""):
        findings.append(Finding(rule, sev, title, detail, fix, file, lineno, svc,
                                snippet))

    in_services = False
    services_indent = 0
    # Track the most recent block key so bare `- ITEM` list entries are attributed
    # to the right option (cap_add vs cap_drop matters: one is unsafe, one is the fix).
    current_key = ""
    current_key_indent = -1

    for i, raw in enumerate(lines):
        line = raw.rstrip("\n")
        lineno = i + 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        line = _strip_inline_comment(line)
        stripped = line.strip()
        if not stripped:
            continue

        if re.match(r"\s*services:\s*$", line):
            in_services = True
            services_indent = _indent(line)
            continue
        if in_services and _indent(line) <= services_indent and \
                re.match(r"\s*\S+:\s*$", line) and \
                not re.match(r"\s*services:\s*$", line):
            in_services = False

        svc = _service_of(lines, i) if in_services else ""
        low = stripped.lower()

        # Maintain the enclosing block key for bare list items.
        km = re.match(r"([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", stripped)
        if km:
            current_key = km.group(1).lower()
            current_key_indent = _indent(line)
        elif stripped.startswith("-") and _indent(line) <= current_key_indent:
            current_key = ""
            current_key_indent = -1

        # privileged: true
        if re.match(r"-?\s*privileged:\s*(true|yes)\s*$", low):
            add("CMP001", "critical", "Service runs privileged",
                "`privileged: true` disables nearly all container isolation — the "
                "container gets access to host devices and can trivially escape to "
                "root on the host.",
                "Remove `privileged: true`. If a specific capability is genuinely "
                "needed, add only that one via `cap_add:` and keep the rest dropped.",
                lineno, svc, _snip(stripped))

        # docker socket bind mount
        if re.search(r"/var/run/docker\.sock|/run/docker\.sock", low):
            ro = low.rstrip().endswith(":ro")
            add("CMP002", "critical", "Docker socket mounted into the container",
                "Mounting the Docker socket gives the container full control of the "
                "Docker daemon — it can start a privileged container and take over "
                "the host. " + ("`:ro` does not help: the API itself is the "
                                "privilege." if ro else ""),
                "Don't mount the socket. If the container must orchestrate "
                "containers, use a scoped socket proxy that allow-lists only the "
                "endpoints it needs.",
                lineno, svc, _snip(stripped))

        # host namespaces
        hm = re.match(r"-?\s*(network_mode|pid|ipc|userns_mode|uts):\s*(.+)$", low)
        if hm and _unquote(hm.group(2)).strip() == "host":
            key = hm.group(1)
            sev = "high" if key in ("network_mode", "pid", "ipc") else "medium"
            add("CMP003", sev, f"Host namespace shared ({key}: host)",
                f"`{key}: host` removes the namespace boundary between the container "
                "and the host — host network interfaces, processes, or shared memory "
                "become directly reachable.",
                f"Remove `{key}: host` and use normal bridge networking / isolated "
                "namespaces; publish only the ports you need.",
                lineno, svc, _snip(stripped))

        # dangerous capabilities
        cm = re.match(r"-?\s*cap_add:\s*\[?(.*)\]?$", low)
        if cm and cm.group(1).strip():
            caps = re.findall(r"[a-z_]+", cm.group(1))
            bad = [c for c in caps if c in DANGEROUS_CAPS]
            if bad:
                add("CMP004", "high", "Dangerous capability added",
                    f"`cap_add: {', '.join(bad).upper()}` grants host-level power "
                    "(e.g. SYS_ADMIN is close to root; ALL removes the capability "
                    "boundary entirely).",
                    "Drop the capability, or scope it to the narrowest one that "
                    "works and pair it with `cap_drop: [ALL]`.",
                    lineno, svc, _snip(stripped))
        if current_key == "cap_add" and \
                re.match(r"-\s*[\"']?(sys_admin|sys_ptrace|sys_module|all|"
                         r"dac_read_search|dac_override|net_admin|sys_boot)[\"']?\s*$", low):
            add("CMP004", "high", "Dangerous capability added",
                f"`{_unquote(stripped.lstrip('- ')).upper()}` grants host-level power "
                "(e.g. SYS_ADMIN is close to root; ALL removes the capability "
                "boundary entirely).",
                "Drop it, or use the narrowest capability plus `cap_drop: [ALL]`.",
                lineno, svc, _snip(stripped))

        # image tag hygiene
        im = re.match(r"-?\s*image:\s*(.+)$", stripped, re.IGNORECASE)
        if im:
            ref = _unquote(im.group(1)).split()[0] if im.group(1).strip() else ""
            if ref and not ref.startswith("$"):
                name = ref.split("/")[-1]
                if "@sha256:" in ref:
                    pass
                elif ":" not in name:
                    add("CMP005", "medium", "Image has no tag (implies :latest)",
                        f"`{ref}` resolves to the mutable `:latest` tag — deploys are "
                        "not reproducible and the image can change under you.",
                        "Pin an explicit version tag, or a digest "
                        "(`image@sha256:...`) for full reproducibility.",
                        lineno, svc, _snip(stripped))
                elif name.endswith(":latest"):
                    add("CMP005", "medium", "Image pinned to :latest",
                        f"`{ref}` uses the mutable `:latest` tag — deploys are not "
                        "reproducible.",
                        "Pin an explicit version tag or a digest.",
                        lineno, svc, _snip(stripped))

        # security_opt disabling sandboxing
        if re.search(r"seccomp[:=]\s*unconfined|apparmor[:=]\s*unconfined", low):
            add("CMP008", "high", "Container sandboxing disabled via security_opt",
                "Setting seccomp or AppArmor to `unconfined` removes the syscall / "
                "MAC filter that blocks most container-escape techniques.",
                "Remove the `unconfined` setting; if one syscall is blocked, ship a "
                "custom seccomp profile that allows just that syscall.",
                lineno, svc, _snip(stripped))

        # secrets in environment values
        em = re.match(r"-?\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.*)$", stripped)
        if em and SECRET_KEY.search(em.group(1)):
            val = _unquote(em.group(2))
            if val and not PLACEHOLDER_VALUE.match(val):
                add("CMP006", "high", "Secret hard-coded in the compose file",
                    f"`{em.group(1)}` has a literal value. Compose files are "
                    "committed, so the credential is in version control and visible "
                    "to anyone with repo access.",
                    "Reference an environment variable (`${DB_PASSWORD}`) supplied at "
                    "deploy time, or use Docker/orchestrator secrets; rotate the "
                    "exposed value.",
                    lineno, svc, _snip(em.group(1) + ": ***"))

        # sensitive host path bind-mounted
        vm = re.match(r"-\s*[\"']?(/[^:\"'\s]*)\s*:\s*([^:\"'\s]+)(:([a-z,]+))?", stripped)
        if vm:
            host_path = vm.group(1).rstrip("/") or "/"
            mode = (vm.group(4) or "rw").lower()
            if "docker.sock" not in host_path:
                if host_path in SENSITIVE_HOST_PATHS:
                    writable = "ro" not in mode
                    sev = "high" if writable else "medium"
                    add("CMP007", sev,
                        f"Sensitive host path bind-mounted ({'read-write' if writable else 'read-only'})",
                        f"`{host_path}` is a sensitive host location. Mounting it "
                        + ("read-write lets the container modify host state (and often "
                           "escalate to host root)."
                           if writable else
                           "exposes host configuration and secrets to the container."),
                        "Mount only the specific subdirectory the service needs, and "
                        "add `:ro` unless writes are genuinely required.",
                        lineno, svc, _snip(stripped))

        # database port published on all interfaces
        pm = re.match(r"-\s*[\"']?(?:(\d+\.\d+\.\d+\.\d+|\[?::\]?):)?(\d+):(\d+)",
                      stripped)
        if pm:
            host_ip, host_port, container_port = pm.group(1), pm.group(2), pm.group(3)
            exposed_all = host_ip in (None, "", "0.0.0.0", "::", "[::]")
            svc_name = DB_PORTS.get(container_port) or DB_PORTS.get(host_port)
            if exposed_all and svc_name:
                add("CMP009", "medium",
                    f"{svc_name} port published on all interfaces",
                    f"Publishing `{host_port}:{container_port}` with no host IP binds "
                    "to 0.0.0.0, exposing the datastore beyond the host (often to the "
                    "public internet on a cloud VM).",
                    f"Don't publish it at all if only other compose services need it "
                    "(use the internal network), or bind to loopback: "
                    f"`127.0.0.1:{host_port}:{container_port}`.",
                    lineno, svc, _snip(stripped))

    findings.sort(key=lambda x: x.sort_key())
    return findings


# --------------------------------------------------------------------------- #
# File / directory handling
# --------------------------------------------------------------------------- #

COMPOSE_NAME = re.compile(r"^(docker-)?compose([.-][\w.-]+)?\.ya?ml$", re.IGNORECASE)


def is_compose(path: str) -> bool:
    return bool(COMPOSE_NAME.match(os.path.basename(path)))


def analyze_file(path: str) -> list[Finding]:
    if path == "-":
        return analyze_text(sys.stdin.read(), "<stdin>")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return analyze_text(fh.read(), path)


def iter_compose(root: str) -> Iterable[str]:
    skip = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if is_compose(full):
                yield full


def collect_findings(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        if path != "-" and os.path.isdir(path):
            for fp in iter_compose(path):
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
        return "compose-guard: no issues detected. ✓"
    counts: dict[str, int] = {}
    for x in findings:
        counts[x.severity] = counts.get(x.severity, 0) + 1
    summary = ", ".join(f"{counts[s]} {s}" for s in
                        ["critical", "high", "medium", "low", "info"] if counts.get(s))
    out = [f"compose-guard found {len(findings)} issue(s): {summary}\n"]
    for x in findings:
        loc = f"{x.file}:{x.line}" if x.line else x.file
        out.append(f"[{SEVERITY_LABEL[x.severity]}] {x.rule}  {x.title}")
        out.append(f"  at {loc}" + (f"  (service: {x.service})" if x.service else ""))
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
        "tool": "compose-guard",
        "threshold": threshold,
        "passed": worst < gate,
        "count": len(findings),
        "findings": [asdict(x) for x in findings],
    }, indent=2)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

SELFTEST_POSITIVE = [
    ("services:\n  app:\n    image: nginx:1.27\n    privileged: true\n", "CMP001"),
    ("services:\n  app:\n    image: nginx:1.27\n    volumes:\n"
     "      - /var/run/docker.sock:/var/run/docker.sock\n", "CMP002"),
    ("services:\n  app:\n    image: nginx:1.27\n    network_mode: host\n", "CMP003"),
    ("services:\n  app:\n    image: nginx:1.27\n    cap_add:\n      - SYS_ADMIN\n", "CMP004"),
    ("services:\n  app:\n    image: nginx:latest\n", "CMP005"),
    ("services:\n  app:\n    image: nginx:1.27\n"
     "    environment:\n      DB_PASSWORD: hunter2realvalue\n", "CMP006"),
    ("services:\n  app:\n    image: nginx:1.27\n    volumes:\n      - /etc:/host-etc\n",
     "CMP007"),
    ("services:\n  app:\n    image: nginx:1.27\n    security_opt:\n"
     "      - seccomp:unconfined\n", "CMP008"),
    ("services:\n  db:\n    image: postgres:16\n    ports:\n      - \"5432:5432\"\n",
     "CMP009"),
]

SELFTEST_NEGATIVE = [
    "services:\n  app:\n    image: nginx:1.27-alpine\n    cap_drop:\n      - ALL\n"
    "    read_only: true\n",
    "services:\n  db:\n    image: postgres:16.2\n"
    "    environment:\n      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}\n"
    "    ports:\n      - \"127.0.0.1:5432:5432\"\n",
    "services:\n  app:\n    image: myapp@sha256:"
    "aaaabbbbccccddddeeeeffff00001111222233334444555566667777888899990000\n"
    "    volumes:\n      - ./src:/app/src:ro\n",
]


def run_selftest() -> int:
    failures = 0
    for src, expected in SELFTEST_POSITIVE:
        rules = {x.rule for x in analyze_text(src, "docker-compose.yml")}
        if expected not in rules:
            failures += 1
            print(f"  MISS: expected {expected} -> {sorted(rules)}\n    src={src!r}")
    for src in SELFTEST_NEGATIVE:
        bad = [x.rule for x in analyze_text(src, "docker-compose.yml")
               if x.severity in ("high", "critical")]
        if bad:
            failures += 1
            print(f"  FALSE POSITIVE: {bad}\n    src={src!r}")
    if failures:
        print(f"selftest: {failures} failure(s)")
        return 1
    print(f"selftest: all {len(SELFTEST_POSITIVE)} positive and "
          f"{len(SELFTEST_NEGATIVE)} negative cases passed ✓")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="analyze_compose.py",
        description="Security linter for Docker Compose files.")
    p.add_argument("paths", nargs="*",
                   help="compose files or a directory, or - for stdin")
    p.add_argument("--json", action="store_true", help="emit JSON output")
    p.add_argument("--fail-on", default="high", choices=list(SEVERITY_ORDER),
                   help="minimum severity that causes a non-zero exit (default: high)")
    p.add_argument("--selftest", action="store_true", help="run built-in checks")
    args = p.parse_args(argv)

    if args.selftest:
        return run_selftest()
    if not args.paths:
        p.print_usage()
        print("error: provide a compose file or directory (or - for stdin)",
              file=sys.stderr)
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

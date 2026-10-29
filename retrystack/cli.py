from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .core import SEVERITIES, analyze, load_file, to_dict, worst_latency


def render_text(findings, g) -> str:
    lines = []
    for f in findings:
        lines.append(f"[{f.severity.upper():8}] {f.rule:17} {f.where}\n           {f.message}")
    worst = worst_latency(g)
    entries = [s.name for s in g.services.values() if s.entry]
    lines.append("")
    for e in entries:
        lines.append(f"entry {e}: worst-case latency {worst[e]:g} ms")
    lines.append(f"{len(findings)} finding(s)")
    return "\n".join(lines)


def render_md(findings) -> str:
    rows = ["| severity | rule | where | message |", "|---|---|---|---|"]
    for f in findings:
        rows.append(f"| {f.severity} | {f.rule} | `{f.where}` | {f.message} |")
    return "\n".join(rows)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="retrystack",
                                description="Find retry amplification and timeout inversions in a service call graph.")
    p.add_argument("graph", help="call graph file (.json or .toml)")
    p.add_argument("--format", choices=["text", "json", "md"], default="text")
    p.add_argument("--amp-threshold", type=int, default=10,
                   help="flag services receiving at least this many requests per entry request (default 10)")
    p.add_argument("--fail-on", choices=SEVERITIES, default=None,
                   help="exit 1 if any finding is at or above this severity")
    p.add_argument("--version", action="version", version=f"retrystack {__version__}")
    a = p.parse_args(argv)
    try:
        g = load_file(a.graph)
    except (OSError, ValueError, KeyError) as exc:
        print(f"retrystack: error: {exc}", file=sys.stderr)
        return 2
    findings = analyze(g, a.amp_threshold)
    if a.format == "json":
        print(json.dumps([to_dict(f) for f in findings], indent=2))
    elif a.format == "md":
        print(render_md(findings))
    else:
        print(render_text(findings, g))
    if a.fail_on:
        bar = SEVERITIES.index(a.fail_on)
        if any(SEVERITIES.index(f.severity) >= bar for f in findings):
            return 1
    return 0

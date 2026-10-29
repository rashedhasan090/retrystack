"""Static analysis of layered retry policies in a service call graph.

Model: each service has an intrinsic latency_ms and calls its outgoing
dependencies sequentially. Each edge has a retry policy: attempts (total
tries, 1 = no retry), timeout_ms per attempt (None = no timeout), backoff_ms
base delay, backoff kind ("fixed" or "exponential"), max_backoff_ms, jitter.
Entry services may declare deadline_ms, the budget a client gives a request.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict

SEVERITIES = ["info", "low", "medium", "high", "critical"]


@dataclass
class Edge:
    caller: str
    callee: str
    attempts: int = 1
    timeout_ms: float | None = None
    backoff_ms: float = 0.0
    backoff: str = "fixed"
    max_backoff_ms: float | None = None
    jitter: bool = False
    retry_on_timeout: bool = True

    def backoff_total(self) -> float:
        total = 0.0
        for i in range(self.attempts - 1):
            d = self.backoff_ms * (2 ** i if self.backoff == "exponential" else 1)
            if self.max_backoff_ms is not None:
                d = min(d, self.max_backoff_ms)
            total += d
        return total


@dataclass
class Service:
    name: str
    latency_ms: float = 0.0
    deadline_ms: float | None = None
    entry: bool = False
    capacity_rps: float | None = None


@dataclass
class Finding:
    rule: str
    severity: str
    where: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class Graph:
    services: dict[str, Service]
    edges: list[Edge]

    def out(self, name: str) -> list[Edge]:
        return [e for e in self.edges if e.caller == name]


def load(data: dict) -> Graph:
    services: dict[str, Service] = {}
    for s in data.get("services", []):
        services[s["name"]] = Service(
            name=s["name"],
            latency_ms=float(s.get("latency_ms", 0)),
            deadline_ms=s.get("deadline_ms"),
            entry=bool(s.get("entry", False)),
            capacity_rps=s.get("capacity_rps"),
        )
    edges = []
    for e in data.get("calls", []):
        for n in (e["from"], e["to"]):
            services.setdefault(n, Service(name=n))
        attempts = int(e.get("attempts", 1))
        if attempts < 1:
            raise ValueError(f"attempts must be >= 1 on {e['from']}->{e['to']}")
        edges.append(Edge(
            caller=e["from"], callee=e["to"], attempts=attempts,
            timeout_ms=e.get("timeout_ms"), backoff_ms=float(e.get("backoff_ms", 0)),
            backoff=e.get("backoff", "fixed"), max_backoff_ms=e.get("max_backoff_ms"),
            jitter=bool(e.get("jitter", False)),
            retry_on_timeout=bool(e.get("retry_on_timeout", True)),
        ))
    if not any(s.entry for s in services.values()):
        callees = {e.callee for e in edges}
        for s in services.values():
            if s.name not in callees:
                s.entry = True
    return Graph(services, edges)


def load_file(path: str) -> Graph:
    if path.endswith(".toml"):
        import tomllib
        with open(path, "rb") as f:
            return load(tomllib.load(f))
    with open(path, encoding="utf-8") as f:
        return load(json.load(f))


def find_cycles(g: Graph) -> list[list[str]]:
    cycles, color, stack = [], {}, []

    def dfs(n):
        color[n] = 1
        stack.append(n)
        for e in g.out(n):
            c = color.get(e.callee, 0)
            if c == 1:
                cycles.append(stack[stack.index(e.callee):] + [e.callee])
            elif c == 0:
                dfs(e.callee)
        stack.pop()
        color[n] = 2

    for n in g.services:
        if color.get(n, 0) == 0:
            dfs(n)
    return cycles


def worst_latency(g: Graph) -> dict[str, float]:
    """Worst-case wall time for a service to finish one inbound request."""
    memo: dict[str, float] = {}

    def w(n, seen=()):
        if n in memo:
            return memo[n]
        if n in seen:
            return float("inf")
        total = g.services[n].latency_ms
        for e in g.out(n):
            total += edge_cost(e, w(e.callee, seen + (n,)))
        memo[n] = total
        return total

    for n in g.services:
        w(n)
    return memo


def edge_cost(e: Edge, callee_worst: float) -> float:
    per = callee_worst if e.timeout_ms is None else min(e.timeout_ms, callee_worst)
    return e.attempts * per + e.backoff_total()


def amplification(g: Graph, entry: str) -> dict[str, int]:
    """Worst-case number of requests each service receives per one entry request."""
    counts: dict[str, int] = {}

    def walk(n, mult, seen):
        counts[n] = counts.get(n, 0) + mult
        for e in g.out(n):
            if e.callee in seen:
                continue
            walk(e.callee, mult * e.attempts, seen | {e.callee})

    walk(entry, 1, {entry})
    return counts


def retry_paths(g: Graph, entry: str):
    """Yield paths (list of edges) from entry to leaves."""
    def walk(n, path, seen):
        outs = [e for e in g.out(n) if e.callee not in seen]
        if not outs:
            yield path
            return
        for e in outs:
            yield from walk(e.callee, path + [e], seen | {e.callee})
    yield from walk(entry, [], {entry})


def analyze(g: Graph, amp_threshold: int = 10) -> list[Finding]:
    out: list[Finding] = []
    for cyc in find_cycles(g):
        out.append(Finding("CYCLE", "high", " -> ".join(cyc),
                           "call cycle: retries can loop and amplify without bound",
                           {"cycle": cyc}))
    worst = worst_latency(g)

    for e in g.edges:
        where = f"{e.caller} -> {e.callee}"
        cw = worst[e.callee]
        if e.timeout_ms is None:
            sev = "high" if e.attempts > 1 else "medium"
            out.append(Finding("NO_TIMEOUT", sev, where,
                               "no per-attempt timeout; a hung callee holds the caller indefinitely",
                               {"attempts": e.attempts}))
        elif e.timeout_ms < cw:
            downstream_retries = any(x.attempts > 1 for x in g.out(e.callee))
            sev = "high" if (e.attempts > 1 and downstream_retries) else "medium"
            out.append(Finding(
                "TIMEOUT_INVERSION", sev, where,
                f"caller gives up after {e.timeout_ms:g} ms but {e.callee} can take "
                f"{cw:g} ms worst case; abandoned work keeps running"
                + (" while the caller retries on top of it" if e.attempts > 1 else ""),
                {"timeout_ms": e.timeout_ms, "callee_worst_ms": cw,
                 "callee_retries_downstream": downstream_retries}))
        if e.attempts > 1 and e.backoff_ms > 0 and not e.jitter:
            out.append(Finding("NO_JITTER", "low", where,
                               "retries with backoff but no jitter; synchronized clients retry in waves",
                               {"attempts": e.attempts}))
        if e.attempts > 1 and e.backoff_ms == 0:
            out.append(Finding("ZERO_BACKOFF", "medium", where,
                               "immediate retries hammer a struggling callee",
                               {"attempts": e.attempts}))

    for entry in [s for s in g.services.values() if s.entry]:
        counts = amplification(g, entry.name)
        for svc, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            if svc == entry.name or n < amp_threshold:
                continue
            sev = "critical" if n >= amp_threshold * 5 else "high"
            cap = g.services[svc].capacity_rps
            msg = f"one request to {entry.name} can become {n} requests to {svc}"
            if cap:
                msg += f"; at capacity {cap:g} rps it saturates above {cap / n:.3g} entry rps"
            out.append(Finding("AMPLIFICATION", sev, f"{entry.name} => {svc}", msg,
                               {"entry": entry.name, "service": svc, "factor": n}))
        for path in retry_paths(g, entry.name):
            layered = [e for e in path if e.attempts > 1]
            if len(layered) >= 2:
                hops = " -> ".join([entry.name] + [e.callee for e in path])
                out.append(Finding(
                    "LAYERED_RETRY", "medium", hops,
                    f"{len(layered)} hops retry on the same path; retry at one layer only",
                    {"retrying_hops": [f"{e.caller}->{e.callee}" for e in layered]}))
        if entry.deadline_ms is not None and worst[entry.name] > entry.deadline_ms:
            out.append(Finding(
                "DEADLINE_OVERRUN", "high", entry.name,
                f"worst case {worst[entry.name]:g} ms exceeds client deadline "
                f"{entry.deadline_ms:g} ms; late retries do work nobody waits for",
                {"worst_ms": worst[entry.name], "deadline_ms": entry.deadline_ms}))
    out.sort(key=lambda f: (-SEVERITIES.index(f.severity), f.rule, f.where))
    return out


def to_dict(f: Finding) -> dict:
    return asdict(f)

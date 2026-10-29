# retrystack

Retries are configured one hop at a time, but they fail as a system. A client retries 3 times, the service it calls retries 3 times, and the database driver retries 4 times, so one user click can become 36 queries during exactly the outage you were trying to ride out. Meanwhile an upstream timeout that is shorter than the downstream retry chain means the caller has given up while the callee is still grinding, and then the caller retries on top of it.

`retrystack` is a small offline CLI (Python stdlib only) that reads a declarative call graph with per-edge retry policies and reports these cross-hop problems statically, before a retry storm finds them for you.

## What is novel

Linters for retry config look at one client at a time. `retrystack` composes the policies along every path:

- **AMPLIFICATION** multiplies attempts along each path and reports the worst-case number of requests each service receives per entry request, and, when you give a `capacity_rps`, the entry rate at which that service saturates.
- **TIMEOUT_INVERSION** computes each callee's worst-case completion time (its own latency plus its downstream attempts, timeouts, and backoff) and flags callers whose per-attempt timeout is shorter, which means abandoned work keeps running.
- **DEADLINE_OVERRUN** compares the entry service's worst-case latency against the client deadline.
- **LAYERED_RETRY** flags paths where more than one hop retries.
- **NO_TIMEOUT**, **ZERO_BACKOFF**, **NO_JITTER**, and **CYCLE** cover the single-edge and structural hazards.

Backoff is modeled as fixed or exponential with an optional cap, so worst-case timing is a real number, not a guess.

## Install

```bash
pip install git+https://github.com/rashedhasan090/retrystack
# or run from a checkout with no install
python3 -m retrystack examples/checkout.toml
```

Requires Python 3.11+ (uses `tomllib`).

## Graph format

TOML or JSON. Services are optional (they are created from calls), and services nobody calls are treated as entries unless you mark entries yourself.

```toml
[[services]]
name = "web"
entry = true
deadline_ms = 2000

[[services]]
name = "ledger-db"
latency_ms = 300
capacity_rps = 500

[[calls]]
from = "payments"
to = "ledger-db"
attempts = 4          # total tries, 1 = no retry
timeout_ms = 400      # per attempt; omit for no timeout
backoff_ms = 0        # base delay between tries
backoff = "fixed"     # or "exponential"
max_backoff_ms = 1000 # optional cap
jitter = false
```

Each service is assumed to call its dependencies sequentially, which gives an upper bound on latency.

## Usage

```bash
retrystack graph.toml                       # human-readable report
retrystack graph.toml --format json         # machine-readable
retrystack graph.toml --format md           # Markdown table for PR comments
retrystack graph.toml --fail-on high        # exit 1 in CI if anything high or worse
retrystack graph.toml --amp-threshold 5     # stricter amplification bar
```

Exit codes: 0 clean (or below the gate), 1 gate tripped, 2 input error.

## Demo

`python3 -m retrystack examples/checkout.toml`

```
[HIGH    ] AMPLIFICATION     web => ledger-db
           one request to web can become 36 requests to ledger-db; at capacity 500 rps it saturates above 13.9 entry rps
[HIGH    ] DEADLINE_OVERRUN  web
           worst case 3300 ms exceeds client deadline 2000 ms; late retries do work nobody waits for
[HIGH    ] TIMEOUT_INVERSION orders -> payments
           caller gives up after 800 ms but payments can take 1250 ms worst case; abandoned work keeps running while the caller retries on top of it
[HIGH    ] TIMEOUT_INVERSION web -> orders
           caller gives up after 1000 ms but orders can take 2520 ms worst case; abandoned work keeps running while the caller retries on top of it
[MEDIUM  ] LAYERED_RETRY     web -> orders -> payments -> ledger-db
           3 hops retry on the same path; retry at one layer only
[MEDIUM  ] ZERO_BACKOFF      payments -> ledger-db
           immediate retries hammer a struggling callee
[LOW     ] NO_JITTER         web -> orders
           retries with backoff but no jitter; synchronized clients retry in waves

entry web: worst-case latency 3300 ms
7 finding(s)
```

`examples/healthy.json` shows a graph that passes: retry happens at one layer, with jitter, and every timeout covers the callee's worst case.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT

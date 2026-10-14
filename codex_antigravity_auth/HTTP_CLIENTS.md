# Provider HTTP connection reuse

The gateway lifespan owns at most six lazy HTTPX clients: one for each of the
Google, native OpenAI and BYOK lanes under each `trust_env` policy. This keeps
loopback proxy bypass separate from ordinary remote requests that use HTTPX
proxy and certificate environment settings. Each client has up to the configured
global admission limit of connections and at most eight idle connections, with
30-second idle expiry. HTTPX keeps origins separate and bounds connections
across all BYOK origins. Generation admission and request/stream budgets still
apply; pooling does not add a retry or replay policy.

Credentials, project/account headers, URLs, bodies and timeouts are supplied for
each operation through a request-local lease. Shared client defaults never hold
a provider credential. Response cookies are rejected, including cookies from an
account on the same origin as another account. Redirect following stays disabled.
API endpoints must authenticate via explicit request headers, not cookie sessions.
Plaintext loopback requests use `trust_env=False`; ordinary remote HTTPS keeps
HTTPX proxy and certificate environment support. A caller's explicit trusted
`trust_env=False` policy gets its own lane client. Restart the gateway after
changing environment transport settings. No prompt or output cache is introduced.

Request cleanup closes its HTTP response and releases its lease, leaving other
requests' connections usable. Lifespan shutdown attempts to close each client once
under the existing bounded cleanup shield, even if refresh shutdown fails.
Standalone transport helpers used without gateway lifespan retain operation-owned
clients. OAuth nonstream results use the incremental, bounded native SSE collector
introduced with [resource limits](RESOURCE_LIMITS.md), retaining the terminal
Response rather than the entire wire body. The cooperative cleanup limitations in
[request deadlines](REQUEST_DEADLINES.md) still apply.

## Reproducible synthetic experiment

Run the opt-in benchmark separately from the normal regression suite:

```sh
python -m pytest tests/benchmarks/http_clients.py -q -s
```

It generates a temporary certificate/key and starts only its own loopback TLS
listener. All authorization headers are fixed synthetic values. No application
credentials, real provider or existing gateway are used. For the reported run,
the same credential-free wrapper as the regression suite additionally supplied
a temporary HOME, null keyring and blocked external/gateway networking.

The benchmark warms imports and the async backend, then alternates three pairs
of 30 sequential tiny JSON requests. Both modes use identical TLS trust and
synthetic payloads. The candidate uses the actual lifespan pool and request
leases; the baseline creates/closes a client per operation. Tracemalloc covers
both client and local server Python allocations; it does not measure process RSS,
OpenSSL allocations, network latency or provider inference.

Observed locally on 2026-10-01: macOS, Python 3.14.5, HTTPX 0.28.1, OpenSSL 3.6.2.
A preliminary shared-client experiment showed the same 30-to-one connection
reduction before implementing the pool. The implemented comparison produced:

| Measurement across three repetitions | Per-operation client | Lifespan pool |
| --- | --- | --- |
| TLS connections per 30 requests | 30 | 1 |
| Total elapsed, ms | 132.68–134.52 | 59.68–61.49 |
| Median request, ms | 4.395–4.454 | 1.877–1.945 |
| p95 request, ms | 4.689–4.692 | 1.963–2.023 |
| Peak traced Python bytes | 861,833–921,251 | 772,109–773,946 |

The repeatable connection reduction justifies reuse; these timings do not claim
a live provider speedup. Timing/memory values are observations, not CI thresholds.
The benchmark asserts connection counts and equivalent results.

A separate mock-stream comparison sends identical 2,224,373-byte SSE bodies with
1,048,576 bytes of final text to buffered and incremental collectors. Source
fixture chunks are allocated before tracing, and neither path retains delta
objects. Both return the same complete text:

| Measurement across three repetitions | Buffered collection | Incremental collection |
| --- | --- | --- |
| Peak traced Python bytes | 5,660,550–5,674,184 | 3,176,977–3,180,357 |
| Elapsed, ms | 152.12–171.99 | 157.57–160.09 |

This measures reduced retained wire-body memory in this fixture, not a parsing
speedup or a bound on total process memory. Normal tests separately cover
concurrent credentials/timeouts, cookie rejection, all four auth/route modes,
stream/nonstream cancellation and deadlines, admission return and shutdown.

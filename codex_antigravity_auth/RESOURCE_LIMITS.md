# Resource limits and generation admission

The gateway rejects oversized input with HTTP 413 while reading it, before
routing or credential acquisition. Both declared `Content-Length` and actual
received bytes are checked; missing or understated length cannot bypass the
byte cap. Invalid lengths, length mismatches, invalid UTF-8, non-finite JSON
numbers and malformed inline base64 return HTTP 400. A request is never clipped
and then accepted as complete.

Generation requests reserve a process-wide slot before reading the body and a
route slot after classification. Routes are `google`, `openai` (API key and
OAuth together), and `byok` (all configured providers together). Overload returns
HTTP 503 with `Retry-After: 1`, using `gateway_overloaded` or `route_overloaded`.
There is no waiting queue. Slots follow the existing request owner through
success, error, cancellation, streaming-body closure and pre-iteration
disconnect. Health/model-catalog inspection is outside generation admission.
Each worker process has its own counters; additional workers multiply capacity.

## Operator configuration

Set environment variables on the gateway process. Values must be positive ASCII
decimal integers in the ranges below. Invalid settings refuse startup; a policy
changed to an invalid value inside a running process also fails subsequent
requests. Policy is snapshotted per request. Lowering limits does not cancel
already admitted work.

| Environment variable suffix (prefix `ANTIGRAVITY_MAX_`) | Default | Allowed range |
| --- | --- | --- |
| `BODY_BYTES` | 32 MiB | 1 KiB–128 MiB |
| `ATTACHMENT_BYTES` | 16 MiB | 1 KiB–64 MiB |
| `ATTACHMENTS_BYTES` | 24 MiB | 1 KiB–128 MiB |
| `JSON_DEPTH` | 64 | 8–128 |
| `JSON_NODES` | 100,000 | 1,000–1,000,000 |
| `SCHEMA_DEPTH` | 32 | 4–64 |
| `SSE_FRAME_CHARS` | 8 Mi characters | 1 Ki–16 Mi characters |
| `SSE_TOTAL_BYTES` | 64 MiB | 1 KiB–256 MiB |
| `PROVIDER_BODY_BYTES` | 32 MiB | 1 KiB–128 MiB |
| `INFLIGHT` | 32 | 1–256 |
| `ROUTE_INFLIGHT` | 16 | 1–256 |

For example, `ANTIGRAVITY_MAX_BODY_BYTES=67108864` permits a 64 MiB incoming
body. Attachment budgets are independent so a large text context does not
implicitly permit equally large decoded images. Inline image/file-shaped
base64 values are sized before decoding and checked individually and in
aggregate; remote URLs are not fetched. Capability validation still determines
which input types a route supports.

JSON depth and token/node counts are checked before allocating the parsed tree.
Numeric literals longer than 1,024 characters are refused. Tool and response
schemas have the separate schema depth cap. Google schema reference expansion
shares a node and conservative escaped-string budget across all tools in each
transformation, using `JSON_NODES` and `BODY_BYTES`; it cannot multiply a compact
reference graph into an unbounded output. Optional embedded JSON parsing in
compatibility helpers uses the same parser budget and retains its existing
opaque-string fallback.

## Provider responses

Incremental SSE keeps the existing frame/data-line rules. The cumulative cap
counts decoded UTF-8 event/framing bytes, including comments, before further
accumulation. Completed events preceding a limit remain ordered regardless of
transport chunk boundaries. Pending line storage is coalesced, so one-byte
chunks do not create an unbounded list of fragments. Output text, reasoning and
tool argument/name assembly appends fragments and joins at item/terminal
completion, avoiding repeated copies of an ever-growing string. Legacy multiline
JSON tracks lexical state incrementally and parses a potentially complete root
once, rather than repeatedly reparsing every growing prefix. Oversized/incomplete input
never becomes a successful clipped event.

Stream limits produce `response.failed` with `provider_output_limit` when the
limit is encountered. Existing partial deltas remain visible; translated text
is finalized into the failed response. Native failures retain validated
complete output snapshots or contiguous complete item prefixes, without
inventing opaque continuation from partial events. A policy failure does not
trigger Google account rotation.

OAuth nonstream Responses collection now feeds SSE into the validator as bytes
arrive, retaining terminal state instead of the entire wire body. Normal HTTPX
JSON response collection is incremental and capped before accumulation; an
oversized provider body or JSON parser budget failure returns HTTP 502. Error
body inspection is capped at 64 KiB. Trusted in-process transports exposing only
`post` retain compatibility, with their already-buffered body checked before use.

These bounds cover application-owned buffers, decoded structures and active
generation slots. Interpreter/object overhead, transport/OS allocations and
non-cooperative work are separate; no exact process-RSS or throughput claim is
made. The [request deadline and cleanup contract](REQUEST_DEADLINES.md) still
applies. Tests use synthetic bytes, isolated stores and mocked transports;
no live credentialed exhaustion or native Windows validation is claimed.

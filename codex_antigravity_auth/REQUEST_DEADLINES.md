# Request deadlines and owned cleanup

All `/v1/responses` routes use the same monotonic operation budget: Google,
BYOK Chat Completions, native OpenAI API-key JSON, and OAuth-buffered Responses
SSE. It covers request decoding, route/auth/store preparation, account
acquisition, HTTP connect/body collection, response translation and diagnostics.
The deadline is anchored at handler entry; account rotation never resets it.

| Policy | Default | Request metadata override |
| --- | --- | --- |
| Preparation / nonstream operation | 60 seconds | `antigravity_request_timeout_seconds`, 1–600 |
| Stream event idle | 60 seconds | `antigravity_stream_idle_timeout_seconds`, 1–600 |
| Stream total, including preparation | 1,800 seconds | `antigravity_stream_total_timeout_seconds`, 1–7,200 |

The initial request-body/validation phase uses the default budget because
metadata is not trusted until validation completes. Overrides are measured from
handler entry, never from when they were parsed. Invalid, non-finite or boolean
values fail request validation. These gateway metadata fields are removed before
provider dispatch. `antigravity_backend_timeout_seconds` and existing transport
read timeouts remain independent limits; increasing a whole-operation/stream
budget does not disable those limits.

Idle means waiting for the next normalized event; transport keepalive comments
alone do not extend it. Regular output can continue past the nonstream budget,
up to the separate stream total. Downstream backpressure is also bounded by the
stream total. A 50 ms send grace permits final failure/DONE delivery after a
stream timer fires; a blocked downstream may close without receiving it.

Before response headers, expiry produces HTTP 504. During streaming, expiry
produces one `response.failed` and a DONE marker when the client can receive
them. Previously emitted partial output remains visible. Failure uses
`stream_idle_timeout` or `request_deadline_exceeded`; no retry is added after
visible output. An already observed terminal remains authoritative if only
trailing diagnostics/cleanup stall. Existing Google retry eligibility remains;
a deadline itself never initiates another account/provider attempt.

## Ownership and cancellation

The operation/deadline/disconnect race is extracted from the existing Google
implementation. It drains cancellation for at most 200 ms, consumes late task
exceptions and releases late account acquisitions. Synchronous preparation runs
in a worker so a blocked store/keyring/platform lookup does not hold the event
loop. Abandoned preparation cannot resume the parent request or dispatch a
provider call.

Request ownership spans early HTTP open through response-body handoff. Clients,
entered response contexts and account leases each have one close/release owner.
Teardown also runs if the client disconnects before body iteration starts. The
streaming response uses Starlette's established task-group disconnect pattern
across ASGI versions. Close attempts run under a shield, independently, with a
2-second bound so a stalled close cannot prevent peer cleanup. A late context
entry is closed when it arrives and is never transferred to the client.

Ordinary diagnostic writes get at most two seconds and the remaining operation
budget. Failure diagnostics get a 50 ms grace and are best effort; they cannot
hold essential lease/client cleanup. Python cannot safely terminate a thread in
an OS write or third-party lookup. Such work may finish later. A terminal log
write completing after a stop appends the authoritative failure/cancellation
behind its stale row, preserving logical-request outcome selection.

These are cooperative application bounds, not OS hard-real-time guarantees.
Non-cooperative asynchronous code may remain pending after the bounded drain;
its result is ignored and owned resources receive close attempts. A filesystem
call already in progress cannot be rolled back by cancelling its waiter. No
admission/body-size policy or new provider retry framework is introduced here.

## Verification

Synthetic fault tests cover slow preparation/body/connect, blocked diagnostic
writes, late acquisitions and native context entry, stream idle/total expiry,
downstream backpressure and disconnects before/after the first event. They check
one terminal outcome, no timeout-driven replay, metadata stripping and one
client/context/lease release. Tests run with isolated homes, null keyring and
blocked external/gateway connections. No live credentialed throughput, provider
cancellation, or native Windows result is claimed.

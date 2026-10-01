# Run deadlines and destination scheduling

Generation commands share a monotonic deadline from command entry, including
preflight elapsed time, chunk calls, lane attempts, retries, fallback and judge
synthesis. `--run-timeout` defaults to1800 seconds and accepts a finite positive
value up to86400 seconds. Workflow expansion retains the original control object;
worker lanes do not start a fresh clock. `--timeout` remains an individual HTTP
upper bound, reduced to the remaining run time on every actual attempt.

```bash
python3 scripts/anti.py panel --mode ask --prompt "Compare these approaches" --run-timeout 120 --timeout 60
python3 scripts/anti.py review --scope staged --run-timeout 300 --save-output summary
```

## Actual destination permits

Existing primary-provider queues remain an admission/fairness layer. A shared
process-local permit additionally surrounds each actual HTTP generation attempt.
Google Antigravity and OpenRouter retain their cap of2; other configured providers
use the command's max-parallel bound (2 when absent). Catalog route/family metadata
resolves native aliases and provider identities. Legacy colon-prefix routing is
used only when the catalog lacks a contract; ambiguous bare/slash identities share
one conservative unknown-route permit. This is a configured provider identity,
not a claim that an intermediary exposes its physical downstream provider.

Primary and fallback attempts are sequential. Each attempt releases its permit
on success, HTTP/translation failure, exception or cancellation, before retry
sleep or fallback acquisition. Judge and chunk attempts use the same mechanism.
Separate gateway URLs are separate destinations. Multiple Anti processes do not
share permits; this is not a cross-process quota or provider rate-limit guarantee.

## Deadline outcomes and retained evidence

Admission checks the remaining budget before acquiring and again after waiting.
Expired queued lanes are marked deferred/not submitted. Retry-After or retry
backoff that cannot fit is deferred without sleeping or starting a fallback.
The control records bounded reason events and aggregate started/released/deferred
counts; it does not store prompts, output or credentials.

Immediately before entering the HTTP transport, the deadline is rechecked after
body/header preparation and the timeout is tightened again. Submitted records
mean transport entry, not remote acceptance; expired preparation is unsent and
releases any monetary reservation without charging an estimate.

No new provider attempt starts after expiry. HTTP connection/read timeouts and
gateway timeout hints are tightened for each attempt. Body reads check elapsed
time between chunks so a continuing body cannot reset the run budget. OS DNS and
blocking system calls, local file preparation, CPU parsing and final record writes
are not forcibly preempted; this is a cooperative deadline, not a hard real-time
process-kill bound. There is no abandoned background provider thread. Already
accepted provider work may incur usage even if the client deadline expires.

Completed chunk/lane evidence remains available under the chosen existing
recording mode when later work or synthesis cannot run. Deferred chunks cannot be
counted as sent or failed attempts; plan/review completed, failed and unsent counts
remain disjoint. Compare stops scheduling and labels all remaining models deferred.
If a second judge call is deferred, the completed first judge attempt remains in
structured metadata and its output remains in the full-mode execution ledger. Failure stays nonzero; run/scope metadata
identifies partial work. Never-mode keeps lifecycle/coverage metadata without
adding raw prompt or output retention. Full mode can preserve the completed
execution ledger; summary mode retains its bounded projection. Monetary budget
estimates remain a separate mechanism, not authoritative provider billing.

Synthetic verification covers converging fallback lanes, deadline-aware waits and
Retry-After, retry timeout reduction, exception release, workflow propagation,
partial chunk/judge evidence, queued lanes and owned HTTP fixtures. These tests
also run against installed wheel/rebuilt-sdist artifacts. No live provider
performance, rate quota or native resolver cancellation is claimed.

An explicit [chunk resume](CHUNK_RESUME.md) is a new invocation with its own
deadline and admission allowances. Reused completed chunks acquire no provider
permit and submit no request. The resume report combines prior/current counters
without treating old calls as new submissions or refunding their costs.

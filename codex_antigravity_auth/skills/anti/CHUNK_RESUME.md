# Explicit immutable chunk checkpoints

Direct `review` and `plan` commands can checkpoint their chunk work for a later
invocation. This is opt-in, requires `--save-output full`, and forces chunking.
It can add a synthesis call even for a small scope. Review the call limits before
starting. Never/summary runs and older runs without an explicit checkpoint cannot
be resumed.

```bash
python codex_antigravity_auth/skills/anti/scripts/anti.py review --scope files --file app.py --model sonnet --checkpoint-chunks --save-output full --run-id original --max-prompt-chars 12000
python codex_antigravity_auth/skills/anti/scripts/anti.py runs show original
python codex_antigravity_auth/skills/anti/scripts/anti.py review --scope files --file app.py --model sonnet --resume-from original --rerun-chunk 3 --save-output full --run-id continued --max-prompt-chars 12000
```

Repeat the original task/source/model/chunk settings. `--resume-from` implies
checkpoint mode; use a new run ID or let Anti allocate one. In this example chunk
three previously failed. `metadata.checkpoint.chunks` lists the immutable indices,
outcomes and reusability. Select each failed/truncated/non-answer chunk to rerun
with repeatable `--rerun-chunk N`. Unsent chunks run automatically. Completed
matching chunks are reused unless explicitly selected to rerun. Missing or
corrupted files refuse the resume; they are not treated as equivalent outputs.

A new synthesis always runs. Omissions or incomplete source coverage stay partial,
even if all retained chunks were completed. Raising a source/chunk cap can change
the exact prompts and therefore requires a new checkpoint run. Checkpoint flags
are not accepted with `--dry-run`/`--print-prompt`; inspect an ordinary plan without
those flags. Panel and workflow wrappers do not expose checkpoint execution in
this version; use the direct review/plan command.

## What must match

The recipe binds the captured source commit (when present), selected content,
exact assembled chunk prompt hashes, output/synthesis settings, restrictive data
policy and acknowledgements, local-only settings, explicit pricing identity,
helper tree hash/schema, selected catalog capabilities and route receipts. A
changed source, policy, helper, model or route refuses reuse before another
chunk is submitted. Prompt-only plans without a Git commit retain that unknown
and bind the complete assembled prompt instead. No raw prompt is saved solely
for checkpoint reuse; the store retains prompt hashes and bounded outcome data.

The compatible gateway advertises a versioned `routing_identity` in each model's
capabilities and emits `X-Antigravity-Route-Identity` for the actual prepared
request. The opaque digest covers the adapter build, destination/adapter/model
configuration and relevant declarations. Credentials are not included; provider
header configuration is hashed, not disclosed. Anti refreshes selected catalog
entries before each chunk and synthesis, and checks the returned receipt plus
actual reported model. Older gateways or unavailable identities cannot supply
reusable results. A changed receipt during a call stops the checkpoint workflow
with the submitted attempt recorded. This identifies the selected gateway
route; it does not attest to a provider's hidden routing or exact model weights.

Reused outputs must be complete, hash-verified and unchanged by storage redaction.
A redacted output or unknown actual route is not silently reused. Data policy and
local-only restrictions still apply to reconstructed prompts and synthesis.

## Ownership, durability and recovery

Checkpoint events and manifests are immutable private JSON files beneath
`<runs>/<id>/checkpoints/`. The ordinary run index points to the committed
manifest. The existing per-run lock and writer ID guard publication, signal
writes are deferred until publication settles, and a failed replacement retains
the last usable reference. Unreferenced files are preserved for inspection and
removed only with their owning run by normal terminal-run cleanup.

Resume reads a terminal full-retention source under its lock, verifies all
referenced bytes and copies the required immutable history to the new run. It
never edits the source. A running or uncertain crash placeholder must not be
assumed dead; it is refused for automatic reuse. Preserve damaged records for
manual inspection or start a fresh run. Cleanup tombstones forbid new writes to
reserved IDs. Checkpoints are bounded to 512 selected chunks, 2,048 stored events,
8 MiB per file, 64 MiB of referenced event payloads and 64 runs of lineage.
Immutable manifest history adds bounded metadata beyond the payload budget.

## Accounting and coverage

`resume_accounting` retains prior and current per-run counters and combines
submitted attempts, available heuristic units and explicitly declared currency
ceilings. Unknown currency/usage remains unknown; ceilings are not bills. Current
invocation call/token/currency allowances govern **new** calls. They are not
retroactively applied to old calls and do not refund earlier attempts. Adjusting
a current allowance or deadline is explicit; changing the pricing identity
refuses reuse. Inspect the combined report when bounding the entire lineage.

Coverage describes the immutable review lineage. Reused chunk rows have
`submitted: false`, `submitted_this_run: false`, and `reused: true`; coverage may
count those earlier completed reviews, while current `run_control` counts only
new transport attempts. Prior failed attempts remain in accounting. Source
results, verification caveats and provider-diversity requirements are not upgraded
by reuse. See [run-control ownership](RUN_CONTROL.md), [spend controls](SPEND_CONTROL.md)
and [artifact publication](ARTIFACTS.md).

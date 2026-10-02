# Opt-in repository submission policy

Pass `--data-policy PATH` to `consult`, `review`, `plan`, `compare`, `panel` or
`workflow`. No policy is discovered or activated automatically. A policy can
only reject the helper's selected routes and context; it never adds a model,
changes the gateway, selects a fallback, runs commands or supplies an acknowledgement.
Existing explicit model/fallback/workflow controls still choose the work.

A version 1 policy is a UTF-8 JSON object with exactly these fields:

```json
{
  "schemaVersion": 1,
  "destinations": [
    {
      "baseUrl": "http://127.0.0.1:51122/v1",
      "model": "claude-sonnet-4-6",
      "stages": ["primary", "summary", "judge"]
    }
  ],
  "forbiddenPaths": ["confidential/**", "*.pem"],
  "maxScanChars": 524288
}
```

`destinations` contains 1–64 exact gateway URL/model pairs. Use resolved model
IDs, such as `claude-sonnet-4-6` or `deepseek:deepseek-chat`, not convenience
aliases. Stages are `primary`, `summary`, `judge`, and `fallback`. A fallback must
be allowed for both its parent stage and `fallback`. Models selected by catalog
matching are checked again before dispatch. No model or URL wildcards are allowed.
This controls gateway-local routes; trust the configured gateway and its routing.
It does not independently attest to a remote gateway's backend configuration.
Redirects are refused by the shared endpoint policy.

`forbiddenPaths` contains up to 256 repository-relative shell-style globs,
matched against both logical and resolved source paths. `*` can span `/`;
matching is case-insensitive on Windows and case-sensitive elsewhere. No parent
traversal or backslash syntax is allowed. Paths outside the repository fail.
Checks precede selected file/diff reads, prompt-file reads and consult automatic
pre-reads. Policy-enabled plans omit the optional repository directory-profile
preamble. Pasted prose has no trustworthy file provenance; path rules cannot
identify where pasted content originated. Source is never silently redacted to
make a rejected submission pass.

`maxScanChars` is a positive integer up to 8,388,608. An oversized assembled
prompt fails; the scanner never checks just a prefix and calls the rest safe.
Each actual submitted prompt is checked, including chunk summaries, synthesis,
judge input, fallback and retries. The policy is loaded once for the invocation;
its exact bytes are hashed, so edits require a new invocation.

## Secret checks and explicit acknowledgements

The bounded scanner flags private-key headers, known provider token forms, AWS
access-key IDs and credential-labelled string assignments. It is a high-confidence
pattern check, not proof that arbitrary source contains no secrets. Remove a
flagged value or explicitly acknowledge the **exact assembled prompt** using the
SHA-256 displayed by the error:

```sh
python3 scripts/anti.py consult --data-policy ./anti-policy.json --model sonnet --prompt-file ./question.txt --dry-run
```

Repeat the intended command with `--acknowledge-secret-hash <64 lowercase hex>`
only after inspecting and approving that exact content. The flag is repeatable
(up to 128 hashes). A changed prompt, retry instruction, chunk wrapper or generated
summary needs its own acknowledgement if it contains a matching secret pattern.
An acknowledgement never overrides a denied destination, path or scan limit.
Policy files, repository/model prose and model output cannot grant acknowledgement.
The helper sends the original approved bytes, not a redacted substitute.

Policy dry runs print content-free decisions/hashes and make no HTTP requests
or recording writes. They describe the assembled initial source and selected
routes; future model-generated judge/summary content is unknown and is checked
at its own submission boundary. Every non-dry policy failure exits nonzero with
fixed messages and hashes, never the matched value or denied source path.

## Evidence and retention

Run indexes reserve `metadata.dataPolicy` in all three recording modes, including
`never`. It contains schema version, policy-file hash, at most 128 decision rows
and an explicit omitted-decision count. Rows contain prompt/route SHA-256 hashes,
fixed stage/reason enums and numeric scan/pattern counts. They contain no source,
policy-file body, matched credentials, path strings, URLs or model names. Existing
normal run metadata continues to follow the chosen recording mode.

A decision records preflight authorization, not successful provider execution or
independent verification of the gateway. Retries of an unchanged immutable payload
reuse its authorization; ordinary generation metadata records attempt counts.
Saved-file shape/checksum validation does not make these records signed evidence.

# Offline review-quality replay

`benchmark` starts the controlled evaluation work from issue #76 with an offline,
versioned corpus and replay evaluator. It does not call models, read credentials,
install tools, execute replay content, or change routing/quality ranks. Ordinary
`compare` remains an operational output/latency comparison, not a quality verdict.

```sh
python scripts/anti.py benchmark corpus --output corpus.json
python scripts/anti.py benchmark template --output replay.json
python scripts/anti.py benchmark replay --replay replay.json \
  --adjudications adjudications.json --output report.json
```

All output paths must be new files in existing directories. Without `--output`,
JSON goes to stdout. Input files are bounded to 4 MiB and must be regular files.
Exit 0 means fully scored replay (or successful corpus/template export), 1 means
invalid input/arm or output failure, and 2 means inconclusive replay. A template
contains **unavailable**, unexecuted arms; it cannot be mistaken for measurements.
Start the separate adjudication file as:

```json
{"schemaVersion":1,"kind":"anti-benchmark-adjudications","corpusSha256":"COPY_FROM_CORPUS","records":[]}
```

The initial public synthetic corpus has three defects (expiry boundary, UTF-8
byte accounting, false-value defaults) and two no-defect equivalence controls.
Before/after snapshots, exact diffs and prompts have reproducible SHA-256
identities. `sourceRevisionKind: sha256_fixture_snapshot` denotes content-addressed
fixtures, **not Git commit IDs**. `tests/test_review_benchmark.py` runs independent
behavioral assertions on the shipped fixtures: baseline meets the contract;
defect candidates fail designated probes; controls retain the tested behavior.
These checks do not run during replay. A no-defect control is limited to its stated
contract/probes, not a proof about every possible input.

Expiry fixtures restrict timestamps to finite built-in Python int/float values;
NaN, infinities and custom comparison types are outside that declared domain.
Integer and fractional boundary probes verify the labels within that domain.

Development and holdout partitions are reported separately. The holdout is
public: it enables partition/disagreement auditing, not a blind test or a claim
that a model never encountered the fixture. This small corpus cannot establish
real-world model quality or justify new route defaults.

## Matched arms

The [replay schema](schemas/benchmark-replay-v1.json) describes data shapes. The
runtime also enforces identities, uniqueness, bounds and cross-record consistency.
At most eight arms, 32 cases per arm and 32 findings per case are accepted. Each
arm supplies model/provider labels and exactly the shared `matchedSettings`:
reasoning effort, maximum submitted calls, total estimated input tokens and total
requested output-cap tokens **per case**, including retries/panel/judge attempts.
Those recorded budgets are comparison constraints, not provider billing bounds.

Each case must match the corpus's source, prompt and scope hashes, complete file
coverage and omitted-file list. Replay rows include outcome (`completed`, `failed`,
`unavailable`), submitted-call count, sum of requested output caps, latency,
estimated input tokens and nullable observed input/output tokens. Missing usage
stays unknown, never zero. An unavailable row must have zero submitted calls and
requested output caps, no findings, and null latency/estimated/observed measurements. Completed cases need a submitted attempt, output cap
and input estimate. Exceeded recorded allowances, mismatched effort/budgets,
missing/unknown cases, findings outside the candidate scope or changed hashes
make the arm invalid. Failed/unavailable cases remain explicit and unscored.

Reports separate observed usage from estimated input and requested output caps.
Numbers are attributed replay records: there is no claim that this offline tool
observed a provider, measured its latency or validated a bill. Pairwise comparisons
use only identically scoped, completed, independently adjudicated cases. Excluded
cases are listed. An invalid arm participates in no pairwise comparison; metrics
on other structurally valid rows remain diagnostics, not a winner selection.

## Independent finding evidence

Replay findings carry only ID, file/line, claim and model-claimed severity. A model
cannot embed an accepted verdict. [Local adjudications](schemas/benchmark-adjudications-v1.json)
are supplied separately by an operator who inspected source or reproduced a case:

| Field | Meaning |
| --- | --- |
| `armId`, `caseId`, `findingId` | Exact replay target |
| `armIdentitySha256` | Hash of the arm object containing only `id`, `model`, `provider`, `settings`; rejects stale evidence after identity/effort/budget changes |
| `findingSha256` | SHA-256 of canonical finding JSON |
| `sourceSha256` | Pinned case source identity |
| `verdict` | `confirmed`, `rejected` or `unresolved` |
| `defectId` | Matching corpus label when confirmed; otherwise null |
| `reviewer` | Explicit independent local reviewer label, not the arm model/provider |
| `evidence` | `kind` (`source_inspection` or `reproduction`), concrete single-line `detail`, SHA-256 of that UTF-8 detail, and `independent: true` |

Canonical JSON hashes use sorted keys, compact separators, UTF-8, literal Unicode
and no non-finite numbers. Source hashes bind file path, before, after and exact
diff; prompt/scope hashes are emitted by the corpus command. Inspect evidence
before writing an adjudication. These are attributed local attestations, not
signatures or proof that someone ran a command; copying a model's judgement into
that file is not independent verification.

Aggregate `verifiedDetections`, `verifiedFalsePositives` and severity counters
preserve all verified findings from valid completed cases, including mixed-verdict
cases; `partialVerifiedEvidence` identifies that partial contribution.
`scoredCaseMetrics` contains only fully adjudicated cases and their defect/control
denominators and false negatives. Never divide all-evidence detections by those
subset denominators. Latency/usage summaries include valid submitted failed or
inconclusive cases and state their measurement count/basis. Pairwise reports still
use only the fully adjudicated matched subset; partial findings are not silently
promoted into a comparable quality score.

A known confirmed defect counts once; duplicate confirmations are reported without
inflating detection. Severity comes from the verified corpus label, not model
severity. Explicit rejected findings count as verified false positives. Findings
without local evidence remain unresolved and make their case inconclusive, rather
than being forced into misses or false positives. Confirmed claims that conflict
with a label/no-defect control create a label-conflict audit and need corpus
review; they do not silently change ground truth. Pairwise score disagreements
produce audit entries. Reports keep evidence/finding hashes and counts without
copying arbitrary claims or evidence text. See the [report envelope schema](schemas/benchmark-report-v1.json).

## Future live evaluation gate

This first step has **no live-run command**. A future operator-selected adapter
must first establish advertised route/capability compatibility, exact source and
prompt identity, privacy authorization for every reviewer/judge/fallback, and
explicit call/input/output/currency admission bounds. It must preserve actual
route identities, observed versus estimated usage and independent local
adjudication. No automatic costly tournament, model self-grading, automatic
promotion or default routing change is authorized by this offline command.

# Verification and evidence

[Current status](STATUS.md) owns the source-contract map. Historical release and
credentialed observations are preserved in the
[original verification snapshot](docs/history/2026-10-01/VERIFICATION.md).

## Synthetic source checks

Use a development environment and the checked runner; it isolates credentials,
state, keyring and sockets before test collection. No live key is required.

```bash
uv pip install -e ".[dev]"
python3 scripts/run_tests.py -q
python3 scripts/run_tests.py tests/test_documented_commands.py -q
```

The documentation check parses all gateway and Anti commands in shell code blocks
of README, USAGE, STATUS and this guide using the actual parsers. It never invokes
setup, login, services, probes or Anti generation. This proves CLI grammar, not
successful execution of credentialed operations, shell installation commands or
provider acceptance. Inline prose and historical snapshots are outside that gate.

## Reproducible release evidence

```bash
python3 scripts/release_evidence.py --output /tmp/antigravity-evidence
```

This runs the full checked suite and writes a JSON report, JUnit XML and test log.
The report reads the package version, records source SHA/cleanliness before and
after, actual Python/OS, exit code and JUnit counts. Counts describe the supplied
run; they are never copied into a permanent current-status claim. A dirty or
moving source cannot be labeled an exact revision, even if its tests pass.

Build fresh artifacts first, then optionally validate their full contents and
execute the installed wheel/rebuilt-sdist contract checks:

```bash
python3 -m build --sdist --wheel
python3 scripts/release_evidence.py --output /tmp/antigravity-evidence --dist dist
```

The optional artifact record contains filenames and SHA-256 digests before and
after validation, content-check outcomes and installed-check exit status.
Build provenance remains explicitly unattested: these checks exercise the supplied
artifact bytes; they cannot prove where or how those bytes were built.
It does not claim that a local wheel was uploaded to PyPI, or that local checks
ran on every CI platform. Keep the report outside the tracked source and attach
it to the release/PR as evidence for that revision. A failed check returns nonzero
and still leaves the report. Reports describe local execution, not external CI.

## Explicit operational checks

These commands read local configuration or contact a running gateway. They are
examples for an operator; the documentation parser does not execute them.

```bash
codex-antigravity doctor --codex-ready --json
codex-antigravity models explain gemini-3.8-flash --json
codex-antigravity models probe gemini-3.8-flash --network --json
```

The last command explicitly spends a provider request with configured credentials.
It is not part of synthetic verification. A model catalog response or HTTP 200 is
insufficient: require a completed terminal with usable assistant output, no error
or incomplete detail, and the expected route/model. Record the timestamp, exact
source/artifact and chosen model/configuration without prompts, output text or
credentials. Streaming checks must inspect the terminal event, not merely bytes
arriving. Failed, incomplete, empty and refusal-only responses are not generation
readiness. No new live generation is claimed here.

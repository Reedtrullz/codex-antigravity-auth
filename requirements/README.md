# CI dependency and action policy

`pyproject.toml` owns supported dependency lower bounds. `minimum-runtime.txt`
keeps those direct runtime dependencies at their declared floors on Python 3.10;
transitive and test dependencies resolve normally. This is a compatibility test,
not a secure deployment recommendation. The initial minimum run exposed the
FastAPI 0.110 / Starlette TestClient incompatibility with HTTPX 0.28. The tested
FastAPI floor is now 0.115.6. The peer-address security tests inject the ASGI
client scope directly so older supported TestClient constructor signatures keep
the same security assertions.

`ci-py310-linux.txt` is the complete compatible Python 3.10/Linux runtime, test,
and audit-tool snapshot, resolved on 2026-10-01. It constrains one reproducible
CI lane; the existing Python 3.10/3.11/3.12/3.14 and Windows lanes still exercise
current compatible resolution. It does not impose permanent runtime upper
bounds. The snapshot is Linux-specific; native Windows/macOS extras are not
covered by this snapshot's audit. Existing Windows tests continue separately.

Rebuild the snapshot from the repository root with the command recorded at the
top of that file. Review dependency changes, run the minimum/current/snapshot
suites, then run `python scripts/audit_dependencies.py` before committing it.
Adding a dependency requires updating both the floor file and snapshot in the
same PR. Public package downloads/advisory lookups need network access; tests
use synthetic credentials and temporary storage. Do not point tests at a live
account store, provider or gateway.

The focused static gate runs `python -m ruff check .` with `F` (Pyflakes) and
`E9` rules on the ownership paths listed in `pyproject.toml`. It checks undefined
names, unused imports/variables and invalid syntax without formatting the
repository. Expand its ownership list deliberately as files become maintained;
it is not a claim of full-source lint or type coverage. See the
[official Ruff configuration reference](https://docs.astral.sh/ruff/configuration/).

The audit runs [pip-audit](https://github.com/pypa/pip-audit) against all pinned
packages with dependency resolution disabled. Any advisory blocks the gate
unless its exact ID/package/version has a reviewed exception in
`audit-exceptions.json`. Each exception requires a responsible owner, concrete
reason and expiry date (`YYYY-MM-DD`, expires at the start of that day).
Exceptions are currently empty. Expired, unused, malformed or unowned exceptions
fail. Missing/skipped dependencies, incomplete reports, command failures and
advisory-service timeouts also fail. The gate does not automatically upgrade
packages or imply that a clean advisory result proves software safe.

All external workflow actions are pinned to 40-character commits resolved from
the annotated release refs on 2026-10-01. Dependabot proposes weekly updates;
a maintainer reviews the upstream release and commit diff, confirms the commit
belongs to the intended upstream action, and updates the SHA and release comment
together. Do not auto-merge action or dependency updates. The CI default token
has only `contents: read`, checkouts do not persist credentials, and only the
protected `pypi` publish job receives `id-token: write`. Both regular CI and
release workflows run the quality gates; publishing depends on their success.

Local gate checks after installing `.[dev,quality]`:

```sh
python -m ruff check .
python scripts/run_tests.py tests/test_quality_gates.py -q
python scripts/audit_dependencies.py
```

The gate tests inject synthetic advisory reports and lint defects. They must
fail their specific checks without relying on a real vulnerable dependency or
contacting an advisory service.

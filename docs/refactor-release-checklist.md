# Refactor Release Checklist

Record exact evidence here before merging or tagging. Keep local/mocked, package, running-local, and credentialed-live claims separate.

## Source and local contract gate

- [ ] Clean source status recorded: `git status --short`
- [ ] Python versions recorded: `python --version` for every local lane used
- [ ] `python -m compileall -q codex_antigravity_auth tests`
- [ ] `python -m pytest -q` with exact test and subtest counts
- [ ] `git diff --check`
- [ ] Terminal fixtures: completed, incomplete, refusal, empty failed, malformed failed
- [ ] Lease fixtures: rotation and cancellation each show matching acquire/release and typed attempt records

## Package gate

- [ ] Build sdist and wheel in a bounded clean directory
- [ ] `python -m twine check` passes for every artifact
- [ ] SHA-256 hashes recorded for sdist and wheel
- [ ] Wheel contains `SKILL.md`, agent metadata, `anti.py`, Anti tests, and every `anti_lib` module
- [ ] Fresh-venv `codex-antigravity --help`
- [ ] Fresh-venv `codex-antigravity doctor --help`
- [ ] Fresh-venv `codex-antigravity provider presets`
- [ ] Temporary-home `codex-antigravity install-skill --verify`

## Temporary-home compatibility gate

- [ ] `setup --help`, `setup --check`, and JSON readiness exercised without real config mutation
- [ ] Model overlay add/list/remove exercised with temporary paths
- [ ] Provider set/list/remove exercised with dummy values only
- [ ] macOS/Linux/Windows service renderers exercised without installing a real service
- [ ] Installed Anti direct import/help and tests pass outside the checkout

## Running-local evidence

- [ ] `/health` readback recorded
- [ ] `/v1/models` selected-model readback recorded
- [ ] `doctor --codex-ready --json` store/schema/service/capability diagnostics recorded
- [ ] Sanitized request-log correlation readback recorded

## Credentialed-live evidence

Run only with explicit authorization. Record model/route, terminal state, latency, usage, and sanitized correlation id. Never record prompts, account emails, tokens, or provider keys.

- [ ] Google non-streaming response
- [ ] Google streaming response
- [ ] One configured BYOK route

## Explicit non-claims

- [ ] Service install/uninstall truthfulness is marked unverified unless current-host mutation was explicitly authorized
- [ ] Credentialed Google/BYOK behavior is marked unverified unless the live section above was actually run
- [ ] CI, deploy, publish, merge, and public-package state are not claimed from local evidence


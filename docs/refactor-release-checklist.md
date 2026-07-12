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

## Recorded local evidence — 2026-07-12

Exact source: `6bd82d2718ce438a19f17ff7eb254a9bd8b44680` on `codex/comprehensive-gateway-refactor`; worktree was clean before the evidence build.

- [x] Python 3.10.4: compileall and full suite passed — 554 tests, 187 subtests, one existing Starlette/httpx deprecation warning.
- [x] Python 3.14.5: isolated install, compileall, and the same full suite passed — 554 tests, 187 subtests, the same warning.
- [x] Terminal/lease focused gate passed — 8 tests and 7 subtests covering incomplete, refusal, empty, malformed, and disconnect behavior.
- [x] Wheel and sdist built with `SOURCE_DATE_EPOCH` set to the commit time; Twine and dependency checks passed.
- [x] Wheel: `codex_antigravity_auth-1.6.4-py3-none-any.whl`, 184106 bytes, SHA-256 `02213a7e8072bb85cedb74bd96ab9f4ccf8b4182ce1dfeb9d58ae3554d9e127f`.
- [x] Sdist: `codex_antigravity_auth-1.6.4.tar.gz`, 250174 bytes, SHA-256 `4e2d516527d75e339dd5dd966e06011d0f990f07c7a1408f9200d74c31bcc595`.
- [x] Wheel and sdist contained every required Anti asset and `anti_lib` module.
- [x] Fresh Python 3.12 wheel install passed dependency check, CLI help, doctor help, provider presets, and `install-skill --verify`.
- [x] A temporary-home `setup --check --json` correctly reported not ready and created no files or directories.
- [x] Temporary-home model overlay add/list/remove and dummy-env provider set/list/remove passed.
- [x] macOS LaunchAgent, Linux systemd, and Windows task-name/command rendering passed without installing a service.
- [x] Installed Anti direct help/import and installed-skill tests passed outside the checkout.
- [ ] Running-local `/health`, `/v1/models`, and diagnostics are not claimed for this refactor SHA; an already-running gateway was not treated as proof of the worktree build.
- [ ] Credentialed Google and BYOK generation were not run because provider-spend/live authorization was not explicit.
- [ ] Real service install/uninstall was not run because host service mutation was not explicitly authorized.
- [ ] CI, merge, deploy, publish, and public-package state remain unclaimed.

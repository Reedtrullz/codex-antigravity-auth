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

## Historical `1.6.4` local evidence — 2026-07-12

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

## `1.7.0` release candidate status

- Package metadata is `1.7.0`; the public GitHub release and PyPI package were both verified as `1.6.4` on 2026-07-12.
- The Publish workflow now requires its artifact build and the full Ubuntu 3.10/3.11/3.12/3.14 plus Windows 3.12 test matrix before the PyPI job can run.
- Final source SHA, Python 3.10/3.14 counts, artifact hashes, installed-wheel proof, clean-home proof, and dependency audit are recorded below.
- CI, tag, publish, public `1.7.0`, service mutation, real Codex configuration changes, and credentialed Google/BYOK calls remain explicitly unclaimed.

## Recorded `1.7.0` local evidence — 2026-07-12

Exact artifact source: `c939726f15ea201f7ff0f7b4a06decfa3841ec87` on `codex/release-hardening-1.7.0`. The worktree was clean at that source commit. Disk guard passed with 67 GiB available.

- [x] Python 3.10.4: compileall and full suite passed — 577 tests, 193 subtests, one existing Starlette/httpx deprecation warning.
- [x] Python 3.14.5: isolated `.[dev]` install, compileall, and the same full suite passed — 577 tests, 193 subtests, the same warning.
- [x] `git diff --check` and `git diff --check v1.6.4..HEAD` passed before the artifact build.
- [x] Publish workflow contract tests passed: PyPI requires both build and the Ubuntu 3.10/3.11/3.12/3.14 plus Windows 3.12 matrix; the tag/version guard remains enabled.
- [x] `SOURCE_DATE_EPOCH` was set from the source commit timestamp. Build and Twine checks passed for both artifacts.
- [x] Wheel: `codex_antigravity_auth-1.7.0-py3-none-any.whl`, 186157 bytes, SHA-256 `e682a44c02bce557f53ba99a2f1a9e72def3ddb080e63cd2e761141841edd732`.
- [x] Sdist: `codex_antigravity_auth-1.7.0.tar.gz`, 256235 bytes, SHA-256 `ca3e20986a01d0140edaea846f01433eb768699653141dc0bb902b186cb15063`.
- [x] Wheel contained 39 entries and every required Anti asset; sdist contained 75 entries and the license.
- [x] Clean Python 3.12 wheel install passed `pip check`, reported version `1.7.0`, and passed CLI help, doctor help, provider presets, and temporary-home `install-skill --verify`.
- [x] Installed Anti direct help and all 88 installed-skill tests passed outside the checkout.
- [x] `pip-audit --path <wheel site-packages>` reported no known dependency vulnerabilities. The unpublished local project itself was explicitly skipped because `1.7.0` is not yet on PyPI.
- [x] The installed wheel started under `env -i` on temporary loopback port 51280. `/health` returned `ok=true`, `/v1/models` returned 7 models, and non-live `doctor --codex-ready --json` returned expected exit 1 for the intentionally missing temporary Codex config.
- [x] The running-wheel HOME remained completely empty after startup, health, models, and doctor checks: no directories, locks, keys, account/provider/OAuth stores, or configuration were created.
- [x] An initial runtime check found that health created an account lock in an empty HOME. Commit `c939726` moved startup refresh path checks and health/rotation diagnostics to read-only account paths; the complete source and artifact matrix was rerun after that fix and passed.
- [ ] CI, merge, tag, publish, deploy, and public `1.7.0` state remain unclaimed.
- [ ] Credentialed Google/BYOK generation was not run because live provider authorization was not part of this release-matrix step.
- [ ] Real service install/uninstall and real `~/.codex/config.toml` mutation were not run.

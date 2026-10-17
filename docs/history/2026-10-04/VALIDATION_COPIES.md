# Hidden Antigravity validation copies — 2026-10-04

The visible gateway remains at `027fd313b1f6` and declares Python package 2.4.2. Newer unversioned source copies elsewhere in Projectos **also declare 2.4.2**. Version equality is not implementation equality and does not identify the installed gateway.

| Directory under Projectos | Review scope and role |
|---|---|
| `.antigravity-console-validation-20261002` | Source/test validation copy; inventory and package/source comparison. |
| `.antigravity-pr-validation-20261002` | Source/test validation copy; also supplies the Python path configured by the music demo. |
| `.antigravity-refresh-validation-20261002` | Source/test copy with source-parity artifacts and a separate Python 3.14 environment. |
| `.antigravity-stream-validation-20261002` | Stream/integration validation copy; retained tests/logs are historical evidence. |
| `.anti-music-listening-validation-20261003` | Pinned source mirror used by the authored music demo; 119 configured source hashes matched in this review. |
| `.anti-listening-recovery-validation-20261004` | Recovery validation copy; selected request, resource, route-identity, audio and report boundaries read. Its name is not proof that every recovery behavior works. |
| `.anti-model-selection-validation-20261004` | Appeared during this review through concurrent work. Package 2.4.2; source delta against the recovery copy read, temporary test artifacts preserved. |
| `.anti-music-listening-tmp-20261003` | Owned validation scratch/backups/pytest artifacts; not an independent product or a deletion candidate. |

These are not additional Git checkouts. Source manifests, tracked-file hashes, parity notes, logs and virtual environments were preserved; no packages were installed or imported. Review documentation lives here rather than altering pinned copies.

The late-arriving model-selection copy contains temporary pytest repositories under `.test-tmp/` (24 Git fixtures at the closing inventory), not 24 additional products. Its application Python delta against the recovery copy was in `anti.py`: prefer an exactly advertised model ID before selecting a matching alias, avoiding set iteration retargeting an exact request. This source change and copied Markdown were made by concurrent work, not this documentation pass; tests in that directory were not run by this review. The copy remained a moving snapshot, so its historical/fresh logs are not acceptance evidence here.

## Source differences that matter

The October copies add substantial modules absent from the visible root, including request budgets, parser/resource limits, route identity, endpoint policy, provider clients and native output handling. The recovery copy's `codex_antigravity_auth/resource_limits.py` defines bounded payload/parsing and **process-local** admission controls; it is not a cross-process or account-wide semaphore. `endpoint_policy.py` shares the standalone Anti endpoint policy and request-local restrictions.

`route_identity.py` hashes adapter source and the selected routing declaration into a receipt. Its docstring explicitly says it is not an availability attestation. It binds reuse to a local transformation/configuration identity, not a verified upstream model revision or successful provider request.

The bundled `codex_antigravity_auth/skills/anti/scripts/anti_lib/wav_audio.py` implements a constrained PCM WAV probe: classic RIFF PCM16, mono/stereo, allowed sample rates, bounded duration/bytes/count, canonical base64 and ordered content identity. `input_fidelity.py` applies the audio capability validation before dispatch. This explicit, non-streaming user-audio path requires unverified-probe semantics; forwarding bytes is **not verified listening**.

`anti_lib/reports.py` preserves sanitized advisory findings and separate adjudication/verification state. A report or model verdict is not a tool check. Historical green logs were not rerun and were not promoted into fresh certification.

## Navigation and unresolved scope

See the authored demo (`anti-keyspilli-music-demo-20261003/CURRENT_STATE.md`) and Player study (`anti-keyspilli-player-study-20261003/CURRENT_STATE.md`) in the local Projectos workspace; they are outside this repository. Their source pins and negative controls matter more than a repeated package version. See [visible root review](CURRENT_STATE.md) for that older checkout's actual behavior.

This was a source/delta review, not an exhaustive review of every duplicate copy, every test or each historical log. Installed runtime, provider catalogs, OAuth state, active account quotas and external worktree source were not verified. Do not use these copies to infer what is deployed, merged or safe to clean up.

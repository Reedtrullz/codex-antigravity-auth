# Arming Runbook — Bounded Gemini Listening Qualification

**Status:** Review-only prep. This runbook documents the arming sequence; it does not arm anything. No run ID exists, no binding is minted, no provider call is scheduled, and no OAuth refresh is authorized by this document.

**Date:** 2026-10-09
**Protocol:** docs/superpowers/specs/2026-10-09-bounded-gemini-listening-qualification-protocol.md (sections 8-9)
**Reconciliation:** docs/qualification/2026-10-09-fixture-manifest-reconciliation.md (43-fixture set, owner-confirmed gates)

**Frozen artifacts (verify before any step):**

- Manifest: docs/qualification/fixture-manifest.json — SHA-256 7e0d0822e622407de43fb016dd84c927b063a9bd6c1e2e2b6933a74fc1ba05c9
- Scorer: scripts/qualification/score_qualification_run.py — SHA-256 c0450a0d38874513fe1d1441f17ec44532d343d1f9881fec8be55b697f1dc3fa
- Generator: scripts/qualification/build_qualification_manifest.py — SHA-256 f12063e7c49a2eb285c97e49f27d27feaed24ff6ca2ae166560ac03f993e43a5

All commands below run from the branch worktree with .venv/bin on PATH (the venv is an editable install of this worktree).

## 0. Deviation from spec section 8 (verified against source)

The spec's gateway wording says "--local-only". In the actual CLI, --local-only sets ANTIGRAVITY_LOCAL_ONLY=1, which makes every non-BYOK generation route return 403 local_only_route_forbidden — including the antigravity route this qualification needs. Loopback-only binding is already the default (host 127.0.0.1). The gateway must be started WITHOUT --local-only. Verified in cli.py and server.py at this branch head; recorded here per the spec's amendment path.

## 1. Preflight

1.1 Worktree identity and cleanliness:

    git rev-parse HEAD        # must equal the reviewed PR #178 head
    git status --porcelain    # must be empty

1.2 Verify the three frozen SHA-256 values above against the working tree (shasum -a 256). If any differ, stop: the manifest or scorer was modified after freezing.

1.3 Scorer self-test (expect 10/10 pass):

    python3 scripts/qualification/score_qualification_run.py --self-test

1.4 Isolated state home. Either copy the existing gateway state (stop state writers first) or initialize a fresh one and log in inside it:

    .venv/bin/codex-antigravity namespace copy-state --source <existing-absolute-state-root> --destination <new-absolute-state-root>   # plan; add --write to publish

The qualification gateway must use --state-home <new-absolute-state-root> so the run's account store is isolated from the daily gateway. Fresh state-home logins are performed by the gateway login flow with that state home set; see cli.py namespace/login handling at this head.

1.5 No stale accounts precondition (in the isolated state home):

    .venv/bin/codex-antigravity accounts list

Every account must be eligible with no cooldown or failure state. If any account is stale, stop and resolve; the mint script also re-checks this and refuses on any stale row.

## 2. Gateway start (no --local-only)

    .venv/bin/codex-antigravity start --background \
      --state-home <new-absolute-state-root> \
      --port <isolated-port>

Verify health and model exposure:

    curl -s http://127.0.0.1:<isolated-port>/health | python3 -m json.tool
    curl -s http://127.0.0.1:<isolated-port>/v1/models | python3 -m json.tool

The authorized model is the Gemini audio model advertised there (the 2026-10-09 transport verification used gemini-3.8-flash with audio_input caps: 30 s, 2 MiB, probe opt-in required). Record the exact model id; it is the --model and --authorized-model for every later step.

## 3. Owner-gated one-time refresh (spec section 11.1; skip unless the owner explicitly approves)

    .venv/bin/codex-antigravity accounts reset --all --yes

This clears cooldown/failure state only; it does not mint tokens. The spec pairs it with one bounded text-mode transport probe (16-token cap) to confirm eligibility. That probe is a generation call and is owner-gated like arming itself.

## 4. Binding mint (after the serving gateway is up)

gatewayInstance is per-process (uuid4().hex at gateway start): a binding minted against one gateway process is stale after any restart. Mint immediately before dispatch and re-mint after any gateway restart.

    python3 scripts/qualification/mint_account_binding.py \
      --base-url http://127.0.0.1:<isolated-port>/v1 \
      --model <authorized-model> \
      --out <absolute-non-symlink-binding-path>

The script refuses on stale or in-flight accounts, validates the composed binding with the repository's own parser, writes chmod 600, and never prints binding contents. The binding file must live on a path with no symlinked component (the helper's inventory reader rejects symlinked path components; on macOS /tmp is a symlink to /private/tmp — use a real directory such as the run workdir).

## 5. Expired-token canary (source-verified; no live call)

The server refuses a bound account whose tokenExpiresAt < now + 300 at acquire time (accounts.py acquire_bound_account; tokenExpiresAt is epoch seconds from the inventory). Before dispatch, verify the minted binding's chosen account from the same live inventory:

    curl -s "http://127.0.0.1:<isolated-port>/v1/account-bindings?model=<authorized-model>" \
      | python3 -c "import json,sys,time; inv=json.load(sys.stdin); row=next(r for r in inv['accounts'] if r['accountRef']==json.load(open('<binding-path>'))['accountRef']); assert row['tokenExpiresAt'] >= time.time()+300, row; print('canary pass:', row['accountRef'])"

If the assertion fails, the token is near expiry: stop, resolve the account (owner-gated refresh), and re-mint.

## 6. Refusal controls (zero provider calls)

    python3 scripts/qualification/refusal_controls.py \
      --base-url http://127.0.0.1:<isolated-port>/v1 \
      --model <authorized-model> \
      --workdir <absolute-real-directory> \
      --out <absolute-controls-results-path> \
      --expired-token-evidence "runbook section 5 canary passed at <UTC timestamp>"

Required outcome: 13 rows total — mint-healthy = "minted", all 12 manifest controls = "refused". Any other combination stops arming. The probes exercise: helper dry-run with a missing binding file, mint against a dead port, helper with a wrong accountRef, malformed binding header, fabricated inventory, non-Gemini route, stereo input, oversized file, wrong sample rate, combined-size overflow, unknown model, and the inventory token-expiry canary.

## 7. Owner authorization gate (spec section 9 step 4)

Arming requires the owner's explicit approval recorded against a new immutable run ID (letters, numbers, underscore, hyphen; helper RUN_ID_RE). Without that approval, execution stops here. The frozen studies in spec section 10 remain untouchable regardless of this run's outcome.

## 8. Dispatch (43 fixtures, sequential, one attempt each)

Capture gateway RSS during dispatch (for the scorer's gateway.rssPeakBytes), then dispatch:

    python3 scripts/qualification/dispatch_qualification.py \
      --manifest <absolute-manifest-path> \
      --binding <absolute-binding-path> \
      --model <authorized-model> \
      --authorized-model <authorized-model> \
      --base-url http://127.0.0.1:<isolated-port>/v1 \
      --run-id <owner-minted-run-id> \
      --run-record <absolute-new-run-record-path> \
      --controls-file <absolute-controls-results-path> \
      --gateway-metrics <absolute-json-path-with-rssPeakBytes>

The dispatcher re-verifies every clip SHA-256 and the PCM16/mono/44100 Hz/<=30 s/<=2 MiB transport profile client-side, refuses an existing run record, refuses if model != authorized model, requires the controls file to show all 12 manifest controls as refused, appends each attempt immediately, and never retries or falls back. --stop-after exists only for bounded dev self-checks.

## 9. Scoring (one shot)

    python3 scripts/qualification/score_qualification_run.py \
      --manifest <absolute-manifest-path> \
      --run <absolute-run-record-path> \
      --out <absolute-verdict-path>

The scorer is deterministic, offline, and frozen (gates: core <= 2 wrong, quiet >= 17/19, repeat >= 4/5, all 12 controls refused, 0 confident wrong answers, latency p95 <= 20 s, RSS peak <= 2 GiB). It writes the verdict exactly once.

## 10. Receipt

A final receipt JSON records: run ID, manifest SHA, binding SHA, gateway base URL, per-fixture verdicts, per-class counts, refusal-control rows, latency p95, RSS peak, threshold comparison, and the admission decision (admitted / not-admitted; no partial admission). Store it beside the run record.

## Non-claims

- Nothing in this runbook has been executed against a live gateway or provider.
- The refusal-control script was verified only against a loopback stub that always refuses; stub behavior does not establish live gateway behavior.
- The mint and dispatch scripts are verified with dry-run/parser-level checks only; no binding has been minted from a real inventory.
- No claim is made about live audio perception, musical acceptance, or production admission. This runbook covers transport-level arming and scoring only.

## Frozen-study reminder

queen-original-replay, gemini-test-verification-bound-20261009144311, the 2026-10-07 bounded study, and the 2026-10-08 player-qualification study are immutable. They are not resumed, re-scored, or reinterpreted by this run.


# Codex Antigravity Auth — source review

## Refresh — 2026-10-04

Baseline identity captured before documentation edits: `codex-antigravity-auth`: `main`, `027fd313b1f6`

The visible Python 2.4.2 checkout remains unchanged. Seven newer hidden source/validation copies also declare 2.4.2 but differ materially in implementation; see [validation copies](VALIDATION_COPIES.md). A version string, saved test log or routing receipt does not establish installed source or provider availability.

This repeat review used source/configuration fingerprints, local Git metadata and targeted boundary reads. Earlier structural counts, test statements and dirty-file totals below retain their September observation date; they are not fresh runtime results. See the portfolio coverage (`REVIEW_COVERAGE.md`) and checkout inventory (`WORKTREES.md`) in the local Projectos workspace, outside this repository, for scope and limits.

## Source analysis retained from 2026-09-25

Reviewed 2026-09-25. Baseline: `main`, `027fd313b1f6`, zero tracked changes before documentation. `pyproject.toml` declares **Python package 2.4.2**, Python >=3.10. This is not the npm/TypeScript plugin described in the old portfolio overview.

## What it does and where to work

`codex-antigravity` enters `codex_antigravity_auth/cli.py`; split `cli_*` modules cover setup, diagnostics and service operations. The FastAPI app in `server.py` provides `/v1/models`, `/health` and `/v1/responses`. Incoming Responses requests are validated, classified by `unified.py`, then use the Google native path, an OpenAI upstream path, or a configured BYOK compatible provider. `transform.py`, `google_transport.py` and `openai_transport.py` handle different protocol contracts. Model names in `models.py`/provider catalogs are source declarations, not proof of present upstream availability.

`accounts.py` manages account selection, leases/cooldowns and rotation. `storage.py` and `secure_store.py` manage persistence; OAuth setup lives in `oauth.py` and CLI modules. The bundled `codex_antigravity_auth/skills/anti/` is a substantial independent review workflow with its own scripts, tests and artifacts. It is not the gateway itself.

## Boundaries verified in source

- `server.py::require_remote_gateway_token` accepts local peers through a JSON/Host/Origin guard and requires explicit remote enablement plus a validated bearer token for non-loopback peers. `cli.py::require_safe_gateway_host` separately guards binding. Proxy placement can affect perceived peer identity; no deployed proxy was tested.
- Requests have capability/schema validation before provider dispatch. Streaming success must be judged by terminal events and errors, not HTTP 200 alone.
- `storage.py` writes encrypted JSON with atomic storage/locking, takes an environment key or OS keyring key, and can fall back to a machine-local key. “Encrypted” therefore does not mean “always OS-keyring-only.” Ordinary loading can chmod or migrate plaintext; `load_secure_json_file_read_only` explicitly avoids those mutations.
- Current `unified.py::_resolve_codex_oauth_auth` reads the `tokens` shape with an older compatibility fallback. Current `_open_openai_upstream_stream` checks upstream status before transferring stream ownership. These are concrete changes relative to the review24 copy.
- Anti's generated review prose is advisory. Its scope/coverage/provider records and saved results must agree before claiming complete independent review. Existing STATUS evidence is historical.

## Working and validation

Read the root [contributor guide](../../../AGENTS.md); a separate package guide under `codex_antigravity_auth/` does not exist in this repository. `pyproject.toml` defines pytest and package data; `.github/workflows/ci.yml` includes multi-version/platform tests plus build/install/content checks. `.github/workflows/publish.yml` defines publication. Those jobs were inspected, **not run**; no installed-wheel or PyPI state was checked.

Do not run login/setup/service commands as read-only diagnostics: they can alter account stores, Codex configuration, installed skills and running services. No credentials, provider calls, installs or live gateway checks were used here. Existing root AGENTS model/test statements should be treated as dated snapshots unless freshly verified.

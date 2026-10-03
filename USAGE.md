# Google Antigravity Auth for OpenAI Codex Usage Guide

Start with [current source contracts](STATUS.md) and [verification boundaries](VERIFICATION.md).

This guide describes real-world examples, advanced configurations, and diagnostics routines to run Google Antigravity models inside OpenAI Codex efficiently.

## Local-only gateway and Anti workflows

Use an explicitly configured loopback inference server and start the gateway with
`codex-antigravity start --local-only`. Anti's `--local-only` / `--local-profile`
requires that gateway mode and checks every selected stage, including fallback
and judge. It never downloads models or substitutes hosted defaults. See the
[local-only guide](codex_antigravity_auth/skills/anti/LOCAL_ONLY.md) for setup,
settings export, diversity semantics and the third-party networking boundary.

## 0. Quick Codex Setup

Install the command from PyPI, then run the primary Claude-in-Codex setup:

```bash
uv tool install codex-antigravity-auth
codex-antigravity setup --write --accounts 1 --model claude-sonnet-4-6 --install-skill --start
codex-antigravity setup --check --model claude-sonnet-4-6
```

For a read-only check that does not mutate OAuth state, Codex config, installed skills, or gateway processes:

```bash
codex-antigravity setup --check
codex-antigravity setup --check --live
codex-antigravity setup --json
codex-antigravity status --json
```

The setup command validates the selected model/provider/base URL, preflights Google OAuth or BYOK provider readiness before any config write, prompts for missing Google OAuth desktop-client credentials on an interactive TTY, runs login when needed, writes the Codex provider block, optionally installs the `$anti` helper, optionally starts the gateway in the background, waits for `/v1/models`, and ends with readiness diagnostics. By default, `setup --write` does **not** change top-level `model` or `model_provider`; add `--activate` only when you explicitly want the gateway model to become the active Codex default. When `--base-url` is omitted, setup derives `http://localhost:<port>/v1` from `--port`; if both are supplied with `--start`, their ports must match. Add `--no-input` for automation that should fail instead of prompting, and add `--live` when a read-only setup check should spend one real Google Antigravity `/v1/responses` provider request.

`configure-codex` validates the Codex model id, provider id, provider name, and gateway base URL before writing. `--write` parses TOML before and after a style-preserving edit and verifies that unrelated values stay unchanged. Invalid syntax or an ambiguous target table is refused without changing the config or creating a backup. It serializes cooperating gateway writers across read/merge/backup/replace, uses private atomic writes, preserves a symlinked Codex config path by updating its real target, and creates a private timestamped, byte-exact backup before changing an existing Codex config. TOML Kit supplies parsing/editing on all supported Python versions, including Python 3.10; no `tomllib` availability is assumed. By default it writes only `[model_providers.<provider>]`; add `--activate` to also write top-level `model` and `model_provider`.

Use `setup --repair` when Codex config has drifted and you only want to reconcile the provider block and selected model. It does not run OAuth, install the `$anti` skill, or start/stop the gateway:

```bash
codex-antigravity setup --repair --config ~/.codex/config.toml --model claude-sonnet-4-6
```

For durable startup after reboot, install the gateway as a per-user service:

```bash
codex-antigravity service install --port 51122 --host 127.0.0.1
codex-antigravity service status --json
codex-antigravity service uninstall --port 51122
```

The service command writes a macOS LaunchAgent, Linux systemd user unit, or Windows Scheduled Task depending on the platform. `doctor --codex-ready` and `status --json` report both the lightweight pid-file process state and the durable service state.

Client endpoints require HTTPS for remote hosts. Plain HTTP is allowed for `localhost`, IPv4 loopback and IPv6 loopback; URL username/password fields, invalid ports and control characters are rejected before dispatch. Base URLs also reject query strings and fragments. CLI diagnostics/setup, OAuth requests and standalone Anti refuse all HTTP redirects, including same-origin redirects: configure a non-redirecting endpoint instead. Provider HTTPX clients explicitly disable redirect following as well. Explicit invalid provider endpoint overrides remain blocked instead of falling back to a preset URL; correct or remove the override to restore the preset. Plaintext loopback requests bypass proxies; HTTPS keeps its configured certificate environment.

Gateway request diagnostics are local and sanitized:

Gateway and standalone Anti share credential-redaction rules for structured fields, nested JSON error strings, authorization headers, URL user information, and known token formats. Anti additionally masks provider identifiers; gateway request IDs remain available for telemetry correlation. Redaction bounds diagnostic text to 512 KiB, structured depth to 32, and visited items to 10,000; over-limit content becomes an explicit redacted marker. This policy recognizes credential fields and formats, rather than guaranteeing detection of every arbitrary secret.

```bash
codex-antigravity logs --tail 50
codex-antigravity logs summary --since 24h
codex-antigravity logs --follow
codex-antigravity logs clean
curl http://127.0.0.1:51122/health
```

Request JSONL append and rotation share a cross-process lock. By default, the log retains a current segment capped at `10 MiB` plus one rotated segment. Set `ANTIGRAVITY_REQUEST_LOG_MAX_BYTES` (1 KiB–10 MiB) and `ANTIGRAVITY_REQUEST_LOG_BACKUP_COUNT` (0–5) to change retention. Invalid settings use documented defaults and appear in request-log diagnostics. Individual records exceeding the smaller of the segment cap and 64 KiB are replaced by an explicit omission marker. Readers include all retained segments in chronological segment order and remove exact duplicates only when they have a request ID. Summaries report earliest/latest retained timestamps and flag requested windows that the retained data cannot establish; timestamp coverage does not guarantee that logging was continuous. It records request ids, Anti run correlation, model route/provider/family, stream mode, terminal reason, attempt/rotation counts, cooldown scope/category, cancellation, latency, HTTP status, usage totals, and redacted errors. It does not store raw prompts, request bodies, provider keys, OAuth tokens, account emails, or encrypted stores. `logs summary` aggregates those sanitized records by route/family with terminal, attempt, rotation, cancellation, usage, success-rate, latency, 429, and error-class metrics.

`codex-antigravity doctor --codex-ready --json` includes read-only account/provider store format and migration status, account-state schema version, observed service state, and provider capability mismatches under `diagnostics`. These checks do not migrate stores or rewrite config. See `docs/refactor-migration.md` before upgrading or rolling back a store used by an older package.

OAuth inspection in `setup --check`, `setup --json`, `setup-v2 --check-google`, and `doctor` does not repair credential files, migrate account/provider stores, or create encryption keys. Unsafe POSIX credential permissions produce a warning and prevent use of the file's credentials until repaired; symlinked credential paths are refused. Set the credential file mode to `0600`, or use explicit `setup --write` or `login` to repair permissions. Environment credentials retain precedence. On Windows these checks do not change permissions or assess ACLs.

The package-version check is a separate side effect: setup/readiness and doctor may query PyPI and create or refresh `~/.codex/antigravity-version-check.json` once daily. Set `CODEX_ANTIGRAVITY_NO_UPDATE_CHECK=1` to disable both this lookup and its cache writes when requiring a filesystem read-only check. `setup-v2` does not perform the version check.

OAuth refresh timeouts, connection/DNS failures, server errors, throttling, and malformed responses cool the account down without adding credential strikes. Only a structured `invalid_grant` token rejection adds strikes; session-policy reauthentication and OAuth client configuration errors remain recoverable. Refresh attempts respect persisted account cooldowns and disabled state, including concurrent/background callers. Existing disabled accounts still require explicit recovery. See [Google’s refresh-token and session-policy guidance](https://developers.google.com/identity/protocols/oauth2) for `invalid_grant` versus `invalid_rapt`.

Request-log rows distinguish lifecycle phase (`started` or `terminal`) from semantic terminal outcome. `logs summary` groups by `request_id`, counts the last terminal contribution once, and reports completed, incomplete, failed, cancelled, and requests with no retained terminal record separately. Start rows do not enter latency or failure samples; success rate uses closed requests only and is unknown when none have closed. Duplicate terminals do not double usage or attempts. `upstream_http_status` and `provider_accepted` record observed upstream HTTP acceptance separately from the gateway status and generation outcome. Local validation does not infer provider acceptance; unknown remains unknown. Metrics come only from the selected last terminal, without borrowing from discarded terminals. Disconnect after an observed terminal retains that outcome. Legacy rows remain readable; rows lacking a request ID are counted independently because they cannot safely be correlated.
Google account selection is sticky for sequential requests but load-aware for concurrent ones. `AccountState` owns family/account cooldowns, process-local leases, attempt counters, and persisted schema-version `2` state; request handlers release every lease when non-streaming responses finish or streaming responses end/disconnect.

To expose a local model definition in Codex's model picker, add an overlay entry:

```bash
codex-antigravity models list
codex-antigravity models add claude-experimental \
  --backend-id claude-experimental-backend \
  --display-name "Claude Experimental" \
  --family claude \
  --context-window 200000 \
  --alias claude-exp
codex-antigravity models doctor
```

Overlays are stored in `~/.codex/antigravity-models.toml`. Built-ins are still the source of truth; overlay ids, backend ids, and aliases cannot collide with built-ins unless `--force` is passed. Runtime requests and `/v1/models` fall back to built-ins if the overlay file is malformed; use strict `models list` or `models doctor` to repair it.

`install-skill` installs the bundled Codex `$anti` helper skill into `~/.codex/skills/anti`. Use it after native Claude is working in Codex when you want chat prompts such as `$anti review this diff with opus`, `$anti plan --scope staged`, `$anti panel --mode review --scope staged`, or `$anti smoke` to route through the repo-shipped helper. Existing local `anti` skills are left untouched unless `--force` is passed. Installation first copies into a private sibling staging directory and validates its complete file manifest without executing the staged skill. Publication is serialized; failed swaps or final manifest checks restore the previous installation. Successful forced installs retain a timestamped backup under a sibling `skills-backups` directory so backups do not show up as extra personal skills. If restoration or staging cleanup itself fails, the error reports the installed, backup, or staging paths needed for recovery.

For a no-config-mutation V2 readiness check, run:

```bash
codex-antigravity setup-v2
codex-antigravity install-skill --verify
# If setup-v2 warns that the installed anti skill differs from this package:
codex-antigravity install-skill --force --verify
```

Use `setup-v2 --write` only when you want it to install or refresh the bundled skill. It does not write `~/.codex/config.toml`; use primary `setup --write`, `setup-google`, or `configure-codex --write` to install the provider block. Add `--activate` to those config-writing commands only when you explicitly want to switch the active Codex default. When checking a remote gateway, export the bearer token and pass `--gateway-token-env` (defaults to `ANTIGRAVITY_GATEWAY_TOKEN`) so the `/v1/models` probe can authenticate.
If the existing `anti` skill is locally modified or stale, add `--force` before verification to back it up under `skills-backups` and replace it. BYOK provider checks are opt-in; add `--check-byok` to inspect provider readiness and compare configured provider models with the gateway's `/v1/models` catalog.

For bounded multi-model advice, use the helper-level panel mode. It does not replace Codex's native acting model loop; it returns advisory synthesis and verifiable findings for Codex to check locally:

```bash
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope diff --base origin/main --role correctness --role security --role tests
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode plan --scope working-tree --prompt "Plan this PR"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode ask --model sonnet --model openrouter:deepseek/deepseek-chat --judge opus --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope staged --output findings
python3 ~/.codex/skills/anti/scripts/anti.py moa --mode review --model deepseek-v4-pro --judge opus --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py fusion --mode plan --model opus --model deepseek-v4-pro --judge opus --scope working-tree --prompt "Plan this repository change"
```

Panel mode validates requested judge/fallback models against `/v1/models` before generation and records missing panel lanes as failed metadata when `--min-successes` can still be met. BYOK models only appear there when the gateway process has usable provider credentials or a key-optional local provider setup. Treat panel consensus as a prioritization hint, not proof; verify actionable findings locally before editing.

The panel judge returns a structured findings contract with `id`, `claim`, `severity`, `lanes`, and `verify`. Default prose output renders disagreements first, then findings, unverifiable observations, and caveats. `--output findings` emits just the sanitized findings JSON, while `--json` includes panel results, usage/latency metadata, caveats, findings, and the rendered output. Broad `panel --mode review` scopes reuse the review chunking path to create one bounded summary before fan-out rather than silently truncating full context for every lane.

Default Anti diff inspection disables Git external-diff drivers and textconv converters. Review and planning context use raw diffs and binary-file descriptors; Git inspection failures stop collection instead of becoming empty successful scope. Repository-configured preprocessing is not run implicitly.

Finding confidence is model-reported, not a calibrated probability. Duplicate findings merge deterministically, retain corroborating evidence/checks, and report `findings_merged` separately from `findings_invalid` and `findings_truncated`. `findings_dropped` counts invalid discarded entries only; ordinary merging does not mark judge input partial. Nonfinite numbers, invalid scalar types, and invalid line numbers are rejected with up to 20 bounded `finding_errors` plus an omitted-error count. Generated finding IDs derive from content rather than input order.
Human console output escapes terminal control characters, including ESC, BEL, carriage return, and C1 controls. Ordinary Unicode, Markdown, newlines, and tabs remain readable. JSON output retains original string values through JSON escaping; stored source excerpts are not rewritten by the display layer.

Panel lanes are selected explicitly from the native Sonnet/Opus defaults or from models advertised by the running gateway. BYOK examples include `openrouter:...`, `deepseek:...`, `xai:...`, `kimi:...`, `ollama:...`, and `opencode:...`; they require the corresponding API key or a key-optional local provider. When a BYOK lane receives repository, diff, or file context, the helper prints and records a disclosure naming the provider lane. Virtual picker models such as `panel:*`, `moa:*`, or `fusion:*` remain helper aliases rather than gateway-side fan-out. The current xAI preset is API-key based and uses `XAI_API_KEY`; a catalog entry is not proof that a live generation will succeed.

Repository context leaves the Google Antigravity lane only after explicit selection and the existing BYOK disclosure. Opus remains the default judge; native Codex remains the acting agent and must verify advisory output locally.

V2 workflow presets provide safer defaults for recurring work:

```bash
python3 ~/.codex/skills/anti/scripts/anti.py workflow review-ready --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py workflow plan-deep --scope working-tree --prompt "Plan this PR" --progress
python3 ~/.codex/skills/anti/scripts/anti.py workflow ship-gate --scope diff --base origin/main --json
python3 ~/.codex/skills/anti/scripts/anti.py workflow provider-compare --model sonnet --model openrouter:deepseek/deepseek-chat --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py workflow provider-compare --model deepseek-v4-pro --prompt "Compare this approach with the native review lane"
python3 ~/.codex/skills/anti/scripts/anti.py workflow security-review --scope staged --output findings
python3 ~/.codex/skills/anti/scripts/anti.py workflow debug-consensus --prompt "Intermittent 502s after rotation"
python3 ~/.codex/skills/anti/scripts/anti.py runs list
```

Workflow presets save sanitized summaries under `~/.codex/anti-runs` by default. Primitive commands default to `--save-output never`: only a content-free lifecycle/correlation record is retained, with no findings or reflection history. Opt into `summary` for bounded previews across all saved payloads, or redacted `full` for detailed results and lane files. Summary artifacts are explicitly marked `retention.contentComplete=false`; they are not complete saved answers. See the bundled [recording policy](codex_antigravity_auth/skills/anti/SKILL.md#operational-fallbacks) for bounds. Saved runs include a run id; Anti sends it to the gateway as `metadata.run_id`, and the sanitized request JSONL log records it for correlation without forwarding it to Google or BYOK providers. With `--chunked auto`, Opus/Sonnet plan and review calls use a conservative Claude safety budget and split broad context into bounded chunk calls before synthesis; use `--chunked off` only when you intentionally want one large request, including when `--max-prompt-chars 0` would otherwise mean unlimited. Use `--fallback-model sonnet --fallback-policy on-retryable` for long Opus calls that should degrade after retryable backend failures, and `--progress` to print model/chunk progress to stderr.

Finding checks default to in-memory Python syntax and credential-pattern checks, with structured outcomes and no project writes. Opt into trusted project ESLint with `panel --check-profile eslint` (also forwarded by workflow commands), or skip all checks with `--no-verify`. These checks never execute model-supplied `verify` text or establish a finding's semantic truth. Detailed check records appear in live JSON/full retention; summaries keep counts. See the bundled [check policy](codex_antigravity_auth/skills/anti/SKILL.md#new-flags).

Saved results use immutable revisions referenced by the run index; `anti.py runs show <id>` validates checksums and status consistency before returning `resultPath`. Legacy records are explicitly unverified. See the [artifact contract](codex_antigravity_auth/skills/anti/ARTIFACTS.md).

Each run ID has one writer; use a new ID for a new invocation or when a previous record's ownership is unknown. Corrupt or unreadable reflection files are preserved, with backup/recovery guidance instead of silently replacing history. See the bundled [persistence contract](codex_antigravity_auth/skills/anti/SKILL.md#operational-fallbacks).

Preview retention cleanup with `anti.py runs clean --older-than 30 --dry-run --json`. Only old terminal records are eligible; running, uncertain and temporary state is kept regardless of age. Cleanup retains reflection history and a small permanent ID reservation. Incomplete deletion exits nonzero and lists retained paths; inspect them before retrying with `--resume-cleanup`. See the bundled [cleanup policy](codex_antigravity_auth/skills/anti/SKILL.md#operational-fallbacks).

For the older Google-only OAuth setup, use:

```bash
codex-antigravity setup-google --accounts 2
codex-antigravity start
```

This first verifies that Google OAuth client credentials are configured, then runs the browser OAuth login before writing Codex config so a login startup failure does not leave Codex pointed at an unusable gateway setup. It forces Google's account chooser when adding multiple accounts, stores every successful login in the encrypted rotation pool, clears stale cooldown state on re-authentication, prints the active Gemini/Claude rotation status, writes the Codex provider block, and runs the active-provider doctor only when `--activate` is also passed. To add more accounts later, run `codex-antigravity login --count 2`; to inspect rotation state, run `codex-antigravity accounts`. Use `codex-antigravity accounts reset <email>` to clear persisted cooldown/failure state, `accounts reset --all --yes` for the whole pool, and `accounts remove <email> --yes` to remove a revoked account without hand-editing encrypted storage.

Use `codex-antigravity login --no-browser` to print the authorization URL without launching a browser; `setup --write` and `setup-google` also accept `--no-browser`. Browser-launch failure leaves the URL available for manual opening. Consent denial and Ctrl-C terminate the attempt promptly; timeout uses a monotonic ten-minute deadline. Only a matching-state callback at `/oauth-callback` is accepted, once. The browser reports authorization received; the terminal reports the result after token exchange and account setup.

For remote/headless login, establish `ssh -N -L 127.0.0.1:51121:127.0.0.1:51121 user@remote-host` from your local machine, then run `login --no-browser` on that remote host and open its printed URL locally. Keep both tunnel endpoints on loopback and keep the registered `http://localhost:51121/oauth-callback` redirect unchanged. No token pasting or public callback listener is needed.

For BYOK-only use, replace `codex-antigravity login` with a provider setup command such as:

```bash
codex-antigravity provider set openrouter --api-key-env OPENROUTER_API_KEY --model openrouter/auto
codex-antigravity provider set deepseek --api-key-env DEEPSEEK_API_KEY --model deepseek-v4-pro --model deepseek-v4-flash
codex-antigravity provider set xai --api-key-env XAI_API_KEY --model grok-code-fast-1
codex-antigravity configure-codex --write --model deepseek:deepseek-v4-pro
# Add --activate only if you want DeepSeek to become the active Codex default.
codex-antigravity doctor --byok-only
```

For the unified OpenAI + Antigravity picker (opt-in, one provider for Codex):

```bash
export OPENAI_API_KEY="sk-..."
codex-antigravity setup --write --unified-model-picker --model gpt-5.6 --activate
codex-antigravity start --unified-model-picker
codex-antigravity configure-codex --write --unified-model-picker --model gpt-5.6-codex
codex-antigravity service install --port 51122 --host 127.0.0.1 --unified-model-picker
```

Unified mode advertises `gpt-5.6`, `gpt-5.6-codex`, native Claude/Gemini, and
BYOK `provider:model` ids through `[model_providers.antigravity-unified]` so
Codex keeps a single `model_provider` while the gateway routes per model.
Classic `[model_providers.antigravity]` configs are left untouched. OpenAI auth
prefers explicit `OPENAI_API_KEY` (or `~/.codex/antigravity-openai.json`); set
`ANTIGRAVITY_OPENAI_USE_CODEX_AUTH=1` after `codex login` only if you explicitly
want ChatGPT-subscription reuse via read-only `~/.codex/auth.json` (no refresh
attempted). Extend ids with `ANTIGRAVITY_OPENAI_MODELS="gpt-5.6,my-model"`.

The current presets are API-key based. xAI uses `XAI_API_KEY` and exposes `xai:grok-build-0.1`, `xai:grok-4.3`, and `xai:grok-code-fast-1`; DeepSeek exposes `deepseek-v4-flash`, `deepseek-v4-pro`, `deepseek-chat`, and `deepseek-reasoner`. A model must be advertised by `/v1/models` before Codex can select it, and catalog visibility is not proof that live generation will succeed.

BYOK provider ids may contain only letters, numbers, underscores, and hyphens. Provider model ids may contain `/` or `:`, but not whitespace or control characters. Unknown `provider:model` prefixes are rejected as BYOK routing errors before any Google account selection. Non-preset custom BYOK providers must provide a base URL, and the generic `custom` preset is not auto-enabled until `provider set custom ...` is run. `--api-key-env` is preferred because it avoids persisting keys; `--api-key` stores a key in encrypted provider config. Stored/env BYOK keys and extra provider header values must be printable ASCII without control characters. Model-picker display names must not contain control characters. Provider API-key env var names must contain only letters, numbers, and underscores and must not start with a number. Custom provider and Codex gateway base URLs must be absolute `http` or `https` URLs without embedded credentials, whitespace/control characters, query strings, fragments, invalid ports, or malformed bracketed hosts. Plain `http` base URLs are accepted only for loopback/local hosts; remote providers and remote gateway URLs must use `https`. Extra BYOK provider headers may not override gateway-managed auth, content, host, or transport headers; malformed provider config is rejected before it is written and before streaming begins. Key-optional providers are only keyless on loopback/local hosts; remote custom or cloud URLs need a stored/env API key before they appear in Codex's picker or route requests. BYOK streams surface provider error frames as failed Responses API streams, ignore never-named tool-call deltas, and wait for complete streamed function names before emitting function-call items.
Models configured with `--api-key-env` remain hidden from `/v1/models` until the env var exists in the gateway process environment. `doctor --byok-only` fails when configured BYOK providers have missing or malformed keys, and `doctor --config /path/to/config.toml` can verify non-default Codex config files.

BYOK dispatch supports `kind: openai_chat` only. Providers configured with `openai_responses` or another unsupported kind remain visible in configuration diagnostics, but their models are excluded from `/v1/models`. Doctor and setup preflight report the unsupported kind; direct requests fail with HTTP 400 before contacting a provider. Use an endpoint supporting Chat Completions with `kind: openai_chat`, or remove the unsupported provider. This restriction does not affect the separate unified OpenAI upstream route.

For 1Password-backed BYOK keys, store secret references in a local env file and let the gateway process run under `op run`:

```dotenv
OPENROUTER_API_KEY=op://Private/OpenRouter/sk
```

```bash
codex-antigravity provider set openrouter --api-key-env OPENROUTER_API_KEY --model openrouter/free
codex-antigravity start --background --op-env-file ~/.codex/antigravity.env
codex-antigravity service install --port 51122 --host 127.0.0.1 --op-env-file ~/.codex/antigravity.env
```

Set the env file to private permissions (`chmod 600 ~/.codex/antigravity.env`). The 1Password CLI must be installed on `PATH`; gateway start and service install resolve it to an absolute path and fail before starting the process or writing a manifest if `op` is missing. Durable services still depend on your local 1Password unlock/session behavior after reboot.

If your 1Password CLI includes the Environments beta commands, use `--op-environment <environment-id>` instead of `--op-env-file`.

### Gateway access and reverse proxies

The gateway binds to loopback by default. Local-only mode grants unauthenticated access only to an observed loopback peer with a valid loopback `Host` and no proxy-indicator headers. This applies to `/health` and `/v1/models` as well as generation requests. A request carrying `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host`, `X-Forwarded-Proto`, `X-Forwarded-Port`, or `X-Real-IP` cannot use that exemption, regardless of the header value.

For a reverse proxy, enable authenticated mode **even if the gateway binds to loopback**:

```bash
# Supply a private, randomly generated token in this environment first.
codex-antigravity start --host 127.0.0.1 --allow-remote
```

`--allow-remote` requires `ANTIGRAVITY_GATEWAY_TOKEN` to contain at least 32 visible ASCII characters and enables bearer authentication for **every request**, including direct loopback requests and health checks. Setting `ANTIGRAVITY_ALLOW_REMOTE=1` in the gateway's runtime environment enables the same authentication policy. A non-loopback bind additionally requires the explicit `--allow-remote` flag. Each client must send `Authorization: Bearer <token>`. Readiness/status commands need the token in their own environment too; when using a secret runtime, wrap the invoking command so both the launcher and its child receive the token.

Foreground, background, and managed-service launches disable Uvicorn's forwarded-header interpretation. Forwarded addresses never grant access, and the gateway does not reconstruct browser origins from forwarded scheme or host values. Custom Uvicorn launches must also use `--no-proxy-headers`. Keep the backend private; the built-in server speaks plain HTTP, so remote access needs a protected tunnel or a TLS-terminating proxy with a protected backend connection. The proxy must relay each client's bearer header, rather than inject the gateway token for unauthenticated callers.

A local proxy that strips all proxy indicators and rewrites `Host` looks like any other local process. The gateway cannot detect that proxy automatically; authenticated mode is required before exposing it. JSON content-type, cross-site fetch, and Origin checks remain active after authentication. For browser requests, Origin must match the scheme and Host visible to the backend; forwarding `X-Forwarded-Proto: https` alone does not make an HTTP backend origin match an HTTPS browser origin.

Use live diagnostics sparingly when proving a final install:

```bash
codex-antigravity doctor --codex-ready --live --live-model claude-sonnet-4-6
```

`doctor --live` currently supports Google Antigravity models only. It also performs a once-daily cached package-version check against PyPI and warns when an upgrade is available. Set `CODEX_ANTIGRAVITY_NO_UPDATE_CHECK=1` to disable that external metadata lookup.

Live readiness requires a completed response with usable text in a completed assistant message. HTTP success alone, failed or incomplete responses (including token-cap exhaustion), refusals, empty output, and malformed responses do not pass. The live probe in `doctor --codex-ready --json` separates `transport_ok` from `generation_ok` and reports `terminal_kind`, `terminal_reason`, and a redacted `error`; `ok` reflects generation success. The check sends one request with the existing token budget and does not retry automatically.

Google and Chat Completions responses select provider alternative index `0`, consistently across streaming and non-streaming output. Other alternatives cannot contribute text, tools, or terminal reasons. A single unindexed alternative remains supported; ambiguous multi-answer or mixed unindexed/alternative streams fail explicitly. Usage stays the provider-reported aggregate, since per-alternative token usage cannot be inferred.

Token refresh and project discovery run outside account-selection and storage locks. A concurrent selection can use another eligible account; a busy refresh never makes an expired token eligible. Each account has one refresh owner per gateway process. The credential snapshot is checked before refresh and before writing back, so removal, changed credentials, and newer token state take precedence. Family cooldowns remain independent of token refresh.

The gateway lifespan starts a refresh-ahead check and repeats checks every 60 seconds while idle, refreshing tokens within five minutes of expiry. At most one refresh-ahead worker runs at a time. Shutdown stops the timer, signals the worker to stop before further discovery/merges/accounts, and waits for the current synchronous call to finish using its existing network timeouts. It does not abandon a live worker thread. This is process-local refresh ownership; multiple gateway processes are not coordinated by a distributed refresh lease.

Live readiness requires a completed response with usable text in a completed assistant message. HTTP success alone, failed or incomplete responses (including token-cap exhaustion), refusals, empty output, and malformed responses do not pass. The live probe in `doctor --codex-ready --json` separates `transport_ok` from `generation_ok` and reports `terminal_kind`, `terminal_reason`, and a redacted `error`; `ok` reflects generation success. The check sends one request with the existing token budget and does not retry automatically.

## 1. Supported Models & Aliases
You can use standard, developer-friendly names in your `~/.codex/config.toml` that the gateway automatically translates to the official Google Antigravity backend model definitions:

| OpenAI Codex Model ID | Antigravity Backend Model |
| --- | --- |
| `gemini-3.8-flash` | `gemini-3.8-flash-tiered` (current Flash; low/medium/high via `thinkingLevel`) |
| `gemini-3.7-flash` | `gemini-3.7-flash-tiered` (supported Flash) |
| `gemini-3.1-pro` | `gemini-3.1-pro-low` (Advanced Reasoning Pro) |
| `gemini-3.1-flash-image` | Recognized but not advertised; generation rejects unsupported image output before dispatch |
| `claude-sonnet-4-6` | `claude-sonnet-4-6` (High-Fidelity Anthropic Sonnet) |
| `claude-opus-4-6-thinking` | `claude-opus-4-6-thinking` (Deep Anthropic Opus Reasoning) |

The older `gemini-3.6-flash-*` and `gemini-3.5-flash-*` IDs remain accepted for saved configurations and backward compatibility.

Common setup aliases are accepted anywhere the CLI accepts a Codex model id:

| Alias | Canonical Codex Model ID |
| --- | --- |
| `sonnet` | `claude-sonnet-4-6` |
| `claude-sonnet` | `claude-sonnet-4-6` |
| `opus` | `claude-opus-4-6-thinking` |
| `claude-opus` | `claude-opus-4-6-thinking` |
| `flash` | `gemini-3.8-flash` |
| `flash-low` / `flash-medium` / `flash-high` | `gemini-3.8-flash` with the matching `thinkingLevel` |
| `flash-3.8-low` / `flash-3.8-medium` / `flash-3.8-high` | `gemini-3.8-flash` with the matching `thinkingLevel` |
| `flash-3.7` | `gemini-3.7-flash` |

`codex-antigravity models doctor` also prints the Claude thinking-budget mapping for `low`, `medium`, `high`, and `xhigh` so advertised reasoning metadata can be compared with runtime request transforms.

---

## 2. Advanced Multi-Account Rotation & Rate-Limiting
When multiple Google accounts are registered, the gateway automatically rotates through them:
- **Rate-Limiting Cooldowns**: If a request returns `429 RESOURCE_EXHAUSTED` (such as Anthropic/Claude limiters), the account is marked on an account-level cooldown backoff strategy with exponential delay. Cooldowns persist across restarts so the gateway does not immediately retry a recently limited account.
- **Sticky Active Selection**: The `AccountManager` keeps independent active-account slots for Gemini and Claude families to preserve conversational continuity before rotating on connection timeouts/failures.
- **Claude Diagnostics**: Google request failures include sanitized family-level diagnostics such as selected family, cooldown count, retry-after source, rotation attempt status, and whether all Claude accounts are cooling down. Non-streaming Google failure responses use a structured `detail` object with `message` and `diagnostics`; clients should handle both this shape and older string details. Account identifiers are reserved for authenticated account-list commands.

Use `codex-antigravity accounts explain --model claude-sonnet-4-6` (or add `--json`) for a read-only eligibility explanation. The view uses positional identifiers such as `account-1`, fixed exclusion categories, cooldown seconds, token lifetime categories, and next actions. It includes the account-store, OAuth client configuration, and keyring namespaces without displaying emails, tokens, project IDs, fingerprints, or stored free-text error reasons. Identifiers follow store order and may change when accounts are removed.

Routing eligibility is separate from token readiness: an expired token may require refresh, whose success is unknown until attempted. The command never refreshes, probes providers, takes leases, writes migrations, or repairs files. Unsupported store versions fail with recovery guidance. Runtime selection prefers the lowest lease count, with ties ordered cyclically from the family's preferred account; this is sticky preference, not round-robin. The CLI cannot observe another gateway process's leases, so it reports them as unknown and does not predict the next selected account. Cooldown expiry alone does not reveal whether its cause was throttling, transport failure, or authentication.

The view reports route classification for both classic and unified gateway modes because a separate gateway may run with different settings. Google eligibility applies only to modes classified as `antigravity`; OpenAI and BYOK routes are rejected. Standard namespace paths use `~/.codex/...`; customized paths are represented by stable hashes so private directory names stay out of shared reports.

---

## 3. High-Fidelity Streaming & Reasoning
Native Responses content deltas remain streaming, but the final completed/incomplete/failed outcome is committed only after EOF or a detected failure. `[DONE]` does not publish early success: duplicate terminal markers, trailing output, inconsistent supplied identities/sequences, malformed data and interrupted streams produce one failed terminal. Clean EOF after a terminal works without `[DONE]`. Waiting from a candidate terminal to EOF is bounded by the existing OpenAI upstream timeout, including comment-only keepalives. Supplied sequence numbers may have gaps, and compatible providers may omit identity fields or lifecycle events; contradictory supplied values fail. Identity bookkeeping is limited to 10,000 items and 65,536 item-ID characters. Buffered native SSE collection uses the same outcome validation.

Native output preservation and supported item/field limits are documented in [the native Responses contract](codex_antigravity_auth/NATIVE_RESPONSES.md). Reasoning continuation, web-search calls, citations, custom calls and assistant phase survive native routing without using translated-output heuristics. Unsupported item types fail explicitly.

Streaming readers decode UTF-8 incrementally, ignore one leading BOM, and recognize LF, CRLF, and CR line endings. Native Responses events are dispatched at a blank line, with multiple `data:` fields joined by a newline. Malformed UTF-8 is replaced consistently; unfinished data at EOF fails instead of becoming a complete event. Chat Completions and Google retain an explicit legacy JSON-line mode for endpoints that omit blank separators, including multiline JSON continuations; a physical data line must still terminate. Readers retain at most 8 Mi decoded characters and 10,000 data lines per pending frame. These bounds do not impose whole-response or gateway admission limits.

Explicit provider refusal text is retained as refusal content even alongside an answer prefix or tool call. A refusal-only response can be `completed`, while readiness still reports it as refused. Filtered responses with ordinary text or tools are `incomplete` with reason `content_filter`; token-limit responses use `max_output_tokens`. Known technical or unknown finish reasons fail explicitly while retaining supported partial output. Policy metadata without user-facing refusal text produces a generic refusal notice, and safety ratings without an explicit block do not imply refusal. Streaming and non-streaming normalization use the same outcome rules.

The local server natively isolates explicit thinking blocks and stream envelopes, ensuring standard formatting:
- **Thinking/Reasoning block**: Emits `response.reasoning_text.delta` for explicit backend thinking parts while preserving regular `thoughtSignature` text as visible output.
- **SSE Stream**: Formats candidates, function calls, usage metadata, and completion events into Responses API SSE chunks parsed correctly by both Codex CLI and Codex Desktop.

## Namespace copy

Use `CODEX_HOME=/absolute/client/root` for client config/auth/skills and
`ANTIGRAVITY_STATE_HOME=/absolute/gateway/root` for gateway configuration and
state. Set either to `~/.codex` explicitly when sharing that root is intentional.
`namespace show`, status JSON, service JSON and readiness diagnostics distinguish
their sources and whether the roots are shared, without printing credential or
private directory contents. `--config /absolute/file.toml` and `--skill-dir
/absolute/directory` still override individual client paths.

To copy gateway configuration on the same machine, stop the gateway and any
other configuration writers, then inspect the plan:

```sh
codex-antigravity namespace copy-state --source /absolute/old/root --destination /absolute/new/root
# Explicitly publish the copy after checking the plan:
codex-antigravity namespace copy-state --source /absolute/old/root --destination /absolute/new/root --write
```

The destination must not exist. The command stages all selected files in a
private sibling directory, rechecks the source, and publishes the directory
under a destination lock. Ordinary failures discard only staging; abrupt process
termination may leave a hidden staging directory for manual inspection, while
the source stays untouched. Cooperating store writers are locked; stop external
editors and Anti as well, since they need not honor these locks. The contract
does not defend against the same user replacing every parent directory.

Copied configuration comprises account/provider ciphertext, the local fallback
storage key if present, Google OAuth client settings, OpenAI gateway settings,
and model overlays. File bytes are copied unchanged and a versioned checksum
manifest is included. No keyring material is exported: retain access to the same
OS keyring or explicitly supplied storage-key environment. This is not a portable
credential backup or an encryption-key migration. POSIX copies use owner-only
permissions; Windows privacy retains the platform's existing protection model.

Client `auth.json`/`config.toml`, process PID/log files, and historical Anti runs
are excluded. Existing histories remain at the old root; selecting a new root
starts a separate history. Log in through Codex with the selected `CODEX_HOME`
when a new client identity is needed. Finally set `ANTIGRAVITY_STATE_HOME` to the
new root and reinstall any service to capture the selection. Neither the copy
command nor diagnostics changes the current environment or service automatically.
## Request shape and schema diagnostics

Malformed message/content/tool shapes and orphan outputs return field-specific HTTP400 errors before account work. Translated routes reject unsupported built-in tools and explicit schema weakening; Google cannot honor `strict: true`. Native Responses keeps provider-specific items/tools and continuation intact. See [request validation and translation-loss behavior](codex_antigravity_auth/design/request-shapes.md) for compatibility changes and limits.
### Gateway process-log privacy and retention

`start`, `start --background` and newly installed services write bounded,
sanitized process logs separately from structured request telemetry. Per port,
retain at most 2 MiB active plus two 2 MiB backups. `status --json` and
`service status --json` report both log kinds without including their contents.
Legacy gateway/service log files are left untouched; reinstall an existing
service to stop its old append-only output routing. Account references in runtime
messages are opaque and change on restart; explicit account-management commands
still show local account identity. See [the process-log contract](codex_antigravity_auth/PROCESS_LOGS.md).

## Model discovery and recent readiness

`provider discover NAME` reads cached evidence; `--network` explicitly fetches a
bounded optional catalog. `models explain ID --json` stays offline, while
`models probe ID --network` records one expiring text-generation check. Imports
are preview-first and require `--write --accept-digest` to save. See the
[discovery and readiness contract](codex_antigravity_auth/design/model-discovery.md)
for supported pagination, limits, cache semantics and configuration diagnostics.

## Local finding verdicts and report export

```sh
python3 ~/.codex/skills/anti/scripts/anti.py runs export --repo . --run-id RUN_ID --format json
python3 ~/.codex/skills/anti/scripts/anti.py runs finding --repo . --run-id RUN_ID --finding FINDING_KEY --verdict rejected --author reviewer --source-file src/example.py --evidence-file local-evidence.txt
python3 ~/.codex/skills/anti/scripts/anti.py runs export --repo . --run-id RUN_ID --format sarif --output review.sarif
python3 ~/.codex/skills/anti/scripts/anti.py runs export --repo . --format markdown --output reviews.md
```

Use `findingKey` from the first export. Local verdicts require explicit evidence
and record the inspected file hash; model claims and passing file checks remain
unverified. Rejected and unresolved findings remain visible. Existing output
files are never overwritten, and these commands never publish to GitHub. Retained
content and provenance limits follow the
[review export contract](codex_antigravity_auth/skills/anti/ARTIFACTS.md#finding-adjudication-and-review-exports).

### Anti repository submission policies

For explicit repository audits, use `review --scope repository` with optional
literal `--review-root` / `--exclude-path` selections. Working-tree reviews report
excluded untracked files; `--include-untracked` opts in to their content. Staged
scope stays unchanged. See [review inventory and coverage limits](codex_antigravity_auth/skills/anti/SCOPES.md).

Diff findings retain captured old/new line and blob provenance. Locations outside
submitted hunks stay unknown; mapping never verifies the finding. See
[immutable diff locations](codex_antigravity_auth/skills/anti/DIFF_PROVENANCE.md).

`consult`, `review`, `plan`, `compare`, `panel` and `workflow` accept opt-in
`--data-policy PATH`. Exact gateway/model/stage allowlists and forbidden source
paths restrict the chosen run. Bounded secret-pattern checks stop a submission
until resolved or explicitly acknowledged for its exact prompt hash. Policy
dry runs write nothing and report hashes instead of source. See the bundled
[policy contract](codex_antigravity_auth/skills/anti/DATA_POLICY.md).

## Request time budgets

Google, BYOK and native OpenAI requests now share a monotonic 60-second preparation/nonstream deadline. Streaming has separate 60-second event-idle and 30-minute total defaults, including preparation, with validated metadata overrides. Downstream backpressure and resource cleanup are bounded; timeouts never trigger replay after visible output. See [request deadlines and cleanup](codex_antigravity_auth/REQUEST_DEADLINES.md) for overrides, failure outcomes, cleanup grace and cancellation limits.

## Completed function-call validation

Completed tool arguments must encode JSON objects and satisfy the available declared identity and supported schema checks. Invalid calls cannot become executable completion events; usable sibling output is retained with an explicit failed/incomplete result. Google’s internal `_placeholder` is removed only with per-tool injection provenance. See [final-call validation and limits](codex_antigravity_auth/design/tool-calls.md).

Google generated-media parts produce an explicit failure while retaining supported sibling output. The image-generation backend is recognized but excluded from advertised models and rejected before account selection; image input on supported text models remains available. See [Google output support](codex_antigravity_auth/design/google-output.md).

## Anti whole-run deadlines

Anti generation commands accept `--run-timeout` (default 1800 seconds). This budget
is shared by chunks, retries, fallback and judge calls; each actual destination
acquires a process-local permit for each attempt. Deadline-deferred work is saved
as partial coverage under the selected retention mode. See [run controls and
cooperative timeout limits](codex_antigravity_auth/skills/anti/RUN_CONTROL.md).


## Anti admission and cost units

`--budget` remains a heuristic-unit limit. Independent `--max-calls`,
`--max-total-input-tokens` (estimated) and `--max-total-output-tokens` allowances
apply to retries, fallback and judge calls. Optional `--currency-budget` requires
an explicit dated `--pricing-file` with complete-attempt charge bounds; unknown
or stale prices refuse admission. Local usage is not billing. See the packaged
[spend-control contract](codex_antigravity_auth/skills/anti/SPEND_CONTROL.md).

## Model discovery and recent readiness

`provider discover NAME` reads cached evidence; `--network` explicitly fetches a
bounded optional catalog. `models explain ID --json` stays offline, while
`models probe ID --network` records one expiring text-generation check. Imports
are preview-first and require `--write --accept-digest` to save. See the
[discovery and readiness contract](codex_antigravity_auth/design/model-discovery.md)
for supported pagination, limits, cache semantics and configuration diagnostics.

## Context preflight

`POST /v1/context/preflight` accepts the intended Responses request body and
returns a count-only `fit`/`unknown`/`reject` assessment without generation. The
current catalog limits are declarations, so ordinary routes return `unknown`
with labeled whole-request estimates. See [context preflight](codex_antigravity_auth/skills/anti/CONTEXT_PREFLIGHT.md)
for component accounting, evidence requirements, generation behavior and Anti
usage calibration. Character limits are not tokenizer or context guarantees.

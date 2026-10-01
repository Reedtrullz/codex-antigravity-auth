# Google Antigravity Auth for OpenAI Codex Usage Guide

This guide describes real-world examples, advanced configurations, and diagnostics routines to run Google Antigravity models inside OpenAI Codex efficiently.

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

```bash
codex-antigravity logs --tail 50
codex-antigravity logs summary --since 24h
codex-antigravity logs --follow
codex-antigravity logs clean
curl http://127.0.0.1:51122/health
```

The request JSONL log is capped and rotated at `10 MiB`. It records request ids, Anti run correlation, model route/provider/family, stream mode, terminal reason, attempt/rotation counts, cooldown scope/category, cancellation, latency, HTTP status, usage totals, and redacted errors. It does not store raw prompts, request bodies, provider keys, OAuth tokens, account emails, or encrypted stores. `logs summary` aggregates those sanitized records by route/family with terminal, attempt, rotation, cancellation, usage, success-rate, latency, 429, and error-class metrics.

`codex-antigravity doctor --codex-ready --json` includes read-only account/provider store format and migration status, account-state schema version, observed service state, and provider capability mismatches under `diagnostics`. These checks do not migrate stores or rewrite config. See `docs/refactor-migration.md` before upgrading or rolling back a store used by an older package.

OAuth inspection in `setup --check`, `setup --json`, `setup-v2 --check-google`, and `doctor` does not repair credential files, migrate account/provider stores, or create encryption keys. Unsafe POSIX credential permissions produce a warning and prevent use of the file's credentials until repaired; symlinked credential paths are refused. Set the credential file mode to `0600`, or use explicit `setup --write` or `login` to repair permissions. Environment credentials retain precedence. On Windows these checks do not change permissions or assess ACLs.

The package-version check is a separate side effect: setup/readiness and doctor may query PyPI and create or refresh `~/.codex/antigravity-version-check.json` once daily. Set `CODEX_ANTIGRAVITY_NO_UPDATE_CHECK=1` to disable both this lookup and its cache writes when requiring a filesystem read-only check. `setup-v2` does not perform the version check.

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

Each run ID has one writer; use a new ID for a new invocation or when a previous record's ownership is unknown. Corrupt or unreadable reflection files are preserved, with backup/recovery guidance instead of silently replacing history. See the bundled [persistence contract](codex_antigravity_auth/skills/anti/SKILL.md#operational-fallbacks).

For the older Google-only OAuth setup, use:

```bash
codex-antigravity setup-google --accounts 2
codex-antigravity start
```

This first verifies that Google OAuth client credentials are configured, then runs the browser OAuth login before writing Codex config so a login startup failure does not leave Codex pointed at an unusable gateway setup. It forces Google's account chooser when adding multiple accounts, stores every successful login in the encrypted rotation pool, clears stale cooldown state on re-authentication, prints the active Gemini/Claude rotation status, writes the Codex provider block, and runs the active-provider doctor only when `--activate` is also passed. To add more accounts later, run `codex-antigravity login --count 2`; to inspect rotation state, run `codex-antigravity accounts`. Use `codex-antigravity accounts reset <email>` to clear persisted cooldown/failure state, `accounts reset --all --yes` for the whole pool, and `accounts remove <email> --yes` to remove a revoked account without hand-editing encrypted storage.

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
The gateway binds to loopback by default. Non-loopback binds require `--allow-remote` plus `ANTIGRAVITY_GATEWAY_TOKEN` set to at least 32 visible ASCII characters; remote clients must send it as a bearer token. The built-in server is still plain HTTP, so remote use should go through a trusted tunnel, local network boundary, or TLS-terminating proxy.

Use live diagnostics sparingly when proving a final install:

```bash
codex-antigravity doctor --codex-ready --live --live-model claude-sonnet-4-6
```

`doctor --live` currently supports Google Antigravity models only. It also performs a once-daily cached package-version check against PyPI and warns when an upgrade is available. Set `CODEX_ANTIGRAVITY_NO_UPDATE_CHECK=1` to disable that external metadata lookup.

Live readiness requires a completed response with usable text in a completed assistant message. HTTP success alone, failed or incomplete responses (including token-cap exhaustion), refusals, empty output, and malformed responses do not pass. The live probe in `doctor --codex-ready --json` separates `transport_ok` from `generation_ok` and reports `terminal_kind`, `terminal_reason`, and a redacted `error`; `ok` reflects generation success. The check sends one request with the existing token budget and does not retry automatically.

## 1. Supported Models & Aliases
You can use standard, developer-friendly names in your `~/.codex/config.toml` that the gateway automatically translates to the official Google Antigravity backend model definitions:

| OpenAI Codex Model ID | Antigravity Backend Model |
| --- | --- |
| `gemini-3.8-flash` | `gemini-3.8-flash-tiered` (current Flash; low/medium/high via `thinkingLevel`) |
| `gemini-3.7-flash` | `gemini-3.7-flash-tiered` (supported Flash) |
| `gemini-3.1-pro` | `gemini-3.1-pro-low` (Advanced Reasoning Pro) |
| `gemini-3.1-flash-image` | `gemini-3.1-flash-image` (image generation) |
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

---

## 3. High-Fidelity Streaming & Reasoning
The local server natively isolates explicit thinking blocks and stream envelopes, ensuring standard formatting:
- **Thinking/Reasoning block**: Emits `response.reasoning_text.delta` for explicit backend thinking parts while preserving regular `thoughtSignature` text as visible output.
- **SSE Stream**: Formats candidates, function calls, usage metadata, and completion events into Responses API SSE chunks parsed correctly by both Codex CLI and Codex Desktop.

## Private storage and lock files

Gateway secure stores and packaged/standalone Anti persistence share one checked
process-lock implementation. Lock files must be regular, singly linked files
owned by the current user. The opened descriptor is compared with the directory
entry (native volume plus 128-bit file identity on Windows) before permissions change or the Windows lock byte is written. Symlinks,
reparse points, hardlinks, FIFOs and unexpected path types are refused. If neither
POSIX flock nor Windows byte-range locking is available, the operation fails;
there is no thread-only success path.

Managed leaf directories and newly created parents are protected before files
are opened; unrelated pre-existing ancestors are not chmodded. POSIX directories
use 0700 and files 0600, with descriptor-based permission updates. Managed leaf
directory symlinks are refused; use the canonical directory when configuring a
protected store. These checks do not claim protection against the same user or
an administrator replacing every ancestor directory.

Windows uses handle-based ownership and DACL checks rather than treating chmod
as an ACL guarantee. Objects must initially belong to the current user or its
process-default owner (for example an elevated token's default owner group).
Protection sets the current user as owner, applies a protected current-user-only
full-control DACL, then verifies owner, ACE type/count/access mask and inheritance
on the opened object. Files are protected before secret bytes are written;
private directories use an exclusive handle to avoid rewriting unrelated child
ACLs. Each managed child is protected independently before its content is written. If required ACL,
handle or filesystem facilities are unavailable, access fails explicitly.
Administrators' backup/ownership privileges remain outside this boundary.

The Windows implementation follows Microsoft's
[ReOpenFile](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-reopenfile),
[GetSecurityInfo](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo),
[FILE_ID_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info)
and [SetSecurityInfo](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo)
contracts. Native Windows tests inspect temporary-file ACLs independently through
PowerShell; non-Windows runs skip that check and exercise synthetic refusal paths.
No Windows ACL success is inferred from POSIX mode bits or mocked tests.

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

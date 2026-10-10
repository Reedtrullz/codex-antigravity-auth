# Local-only workflows

Local-only mode restricts this gateway and Anti's HTTP destinations to declared
loopback endpoints. It does not download models or bundle an inference engine.
Configure a local OpenAI Chat Completions server and its model IDs first, then
start a separate gateway process in the explicit mode:

```bash
codex-antigravity provider set ollama --base-url http://127.0.0.1:11434/v1 --model qwen3:8b --model gpt-oss:20b
codex-antigravity start --host 127.0.0.1 --port 51122 --local-only
```

These are example IDs: install and verify the models separately. A provider named
`ollama` is not proof of locality; the configured endpoint controls eligibility.
The supported adapter must be configured and usable. Missing stages fail with a
stage-specific error; Anti does not replace them with remote defaults.

`start --background --local-only` passes the mode to its child process. For a
manually managed process, set `ANTIGRAVITY_LOCAL_ONLY=1` in that process's
environment before startup. Mode is captured for its lifespan. An already
running normal gateway is not converted by an Anti flag; restart it explicitly.
The CLI requires a loopback bind, refuses `--allow-remote`, and does not launch
1Password wrappers in this mode. Service-install commands do not configure it.

## Select every stage

Use the packaged helper path or the corresponding installed Anti script:

```bash
python codex_antigravity_auth/skills/anti/scripts/anti.py panel --mode ask --local-only --model ollama:qwen3:8b --model ollama:gpt-oss:20b --judge ollama:qwen3:8b --fallback-policy never --prompt "Compare two designs" --json
```

Consult, review, plan, panel, compare and workflow commands accept `--local-only`.
Every generation call, including chunk summaries, synthesis, retries, fallback
and judge attempts, uses the same policy. Select a loopback fallback explicitly
if enabling one. Remote Sonnet/Opus defaults will fail; they are not local aliases.
`smoke --local-only --mode sidecar --model ollama:qwen3:8b` checks the local
catalog without generation. Full/Codex-backend smoke modes are outside this
workflow and are refused.

Two different models behind one provider remain `same_provider_multi_model`.
A single-model panel remains `degraded_single_model`, partial, and nonzero exit.
Minimum provider/model requirements still apply; local mode cannot manufacture
diversity. Existing repository [data policy](DATA_POLICY.md), coverage,
[deadlines](RUN_CONTROL.md), [admission allowances](SPEND_CONTROL.md) and retention
settings remain in force. Local mode does not assert inference is free or tested.

## Export settings without credentials

Export and explicitly load a versioned profile. Export performs no network lookup
and does not confirm availability:

```bash
python codex_antigravity_auth/skills/anti/scripts/anti.py local-profile --model ollama:qwen3:8b --model ollama:gpt-oss:20b --judge ollama:qwen3:8b > local-settings.json
python codex_antigravity_auth/skills/anti/scripts/anti.py workflow provider-compare --local-profile local-settings.json --prompt "Compare two designs" --json
```

The profile contains exactly `version: 1`, `local_only: true`, `gateway`, `models`,
`judge`, `fallback_model` and `fallback_policy`. It has no credential/header fields
or provider configuration, and import does not change gateway configuration.
Unknown keys, duplicate keys, non-loopback gateways and files above 64 KiB fail.
Use a one-reviewer profile for consult/review/plan and `workflow plan-deep`.
Do not combine a profile with explicit gateway/model/judge/fallback flags. Keep
credentials out of model IDs and endpoint paths; settings describe user-selected
names and are not an automatic redaction/export of an existing secret store.

## Enforcement and limits

The gateway advertises `local_only_policy: {"version": 1, "enabled": true}` in
health/catalog responses, filters its catalog to dispatchable loopback BYOK
routes, and labels each capability's `destination_scope`. Native Google/OpenAI
routes and remote BYOK endpoints are refused before dispatch. Google background
refresh and package update checks do not run. Normal configuration and encrypted
local provider stores can still be read; local-only is not a credential-store
migration or isolation feature.

Anti requires that contract plus declared loopback capabilities, and submits to
`/v1/local/responses`, which refuses operation without gateway local-only mode.
The gateway rechecks actual provider configuration on each request. A normal
`/v1/responses` request can additionally require local routing with boolean
`metadata.antigravity_local_only: true`; `false` cannot opt out of global mode.
Older gateways lacking the dedicated endpoint cannot silently accept these runs.

Both HTTP and HTTPS loopback requests bypass proxy environment settings in local
mode. Redirects are refused, even to another loopback URL. HTTPX local mode also
ignores environment-provided certificate configuration; use a trusted endpoint.
Loopback means `localhost` or a loopback IP literal, not a LAN hostname or remote
inference service. Numeric loopback avoids reliance on local hostname resolution.

This is an application destination policy, not an OS network sandbox. It does
not attest that a third-party local model server, keyring implementation, plugin,
repository check or tool never contacts a remote service. Provision those tools
before going offline; use OS-level network restrictions when that stronger
boundary is required. No automatic model download, hosted catalog lookup, remote
fallback or hosted update lookup is part of the local workflow. Retained run
metadata records the policy and optional profile hash without gateway/model
labels, including under never-mode retention.

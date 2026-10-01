# Setup plans, profiles and local restoration

`codex-antigravity setup --plan` prints a versioned JSON plan without writing,
resolving credentials, opening a browser or contacting a gateway. It describes
provider settings, the config target/hash, optional skill/start stages and
prerequisites that still need checking. Credential/login stages are conditional
on the selected route. `--activate` remains an explicit opt-in to changing the
active model/provider; a plan never activates anything.

`setup --write` and `setup --repair` now save a receipt under
`ANTIGRAVITY_STATE_HOME/setup-receipts/<id>/receipt.json` (the state root defaults
to `~/.codex`). Existing setup/check/repair flags and legacy `setup-google`,
`setup-v2`, `configure-codex` and `install-skill` commands remain available.
Those separate legacy commands retain their existing workflows and backups;
the new receipts cover the coordinated `setup` command and profile application.

## Named non-secret profiles

All `profiles` and `setup-history` commands emit JSON with `schemaVersion: 1`.
Creation and application are dry runs unless `--write` is supplied:

```sh
codex-antigravity profiles create google --model claude-sonnet-4-6
codex-antigravity profiles create google --model claude-sonnet-4-6 --write
codex-antigravity profiles create unified --model gpt-5.6 --unified-model-picker --write
codex-antigravity profiles create local --model ollama:gpt-oss:20b --write
codex-antigravity profiles list
codex-antigravity profiles show google
codex-antigravity profiles apply google
codex-antigravity profiles apply google --write --activate
```

Profiles live under the gateway state root in `setup-profiles/<name>.json`.
A profile contains only its name, model/provider/name/base URL, unified-picker
preference and a `secretReferences.gatewayTokenEnv` environment-variable name.
Use `profiles create ... --gateway-token-env ENV_NAME` to configure that reference.
The command never reads the referenced value. Literal API keys, arbitrary fields
and unknown profile versions are refused. Choose a new name instead of silently
overwriting an existing profile.

Application semantically updates the selected Codex provider table while keeping
unrelated TOML and comments. The optional token-variable name becomes that table's
`env_key`; a profile without a reference removes `env_key` from that selected
table. Root model/provider selectors change only with `--activate`. Explicit
`--config` and `--skill-dir` targets are supported; otherwise `CODEX_HOME` owns
client config and skills independently of the gateway state root.

Profiles apply local config and optionally the bundled skill (`--install-skill`,
with `--force` for a different existing skill). They do not log in, fetch secrets,
start/stop a gateway, or change a running service. A unified profile reports the
corresponding manual start follow-up. Provider account/key setup remains separate;
a saved profile does not claim its model or credentials are ready.

## Receipts and partial setup

Receipts record requested, pending, running, completed, skipped and failed stages.
They retain error **classes**, not raw callback errors or credential results.
For config/skill changes, they record before/after hashes and owner-protected
backups before the mutation. Plans, receipts and profile operations are local;
there is no upload or automatic restoration.

```sh
codex-antigravity setup-history list
codex-antigravity setup-history show <receipt-id>
codex-antigravity setup-history restore <receipt-id> --stage config
codex-antigravity setup-history restore <receipt-id> --stage config --write
codex-antigravity setup-history restore <receipt-id> --stage skill --write
```

Restoration always requires an explicit config and/or skill selection. A dry run
compares the current target with the saved post-setup hash and validates the
original backup. Changed, missing, symlinked, unsupported or future-version state
is refused. Even a repeated restore checks for subsequent user drift. Unstarted
stages are reported as not applied. No force flag bypasses these ownership checks.

Before a restore changes files, it records `restoring` plus all recovery paths.
It preserves the displaced config as `config-before-restore`; displaced skill
trees remain beside the target as `.anti.setup-restore-<id>`. Original backups
are retained too. A failure or process interruption with uncertain state requires
manual inspection of these paths. The tool does not guess which copy to delete
or automatically repeat an uncertain restore. A restored newly created config
is removed only after preserving its bytes and verifying the owned hash.

Credential stores, OAuth grants and services are never restored by this mechanism.
A gateway started by setup is not automatically stopped on a later-stage failure;
inspect the recorded status follow-up. Interrupted receipts still marked running
are treated as uncertain, not assumed abandoned.

Backups may contain private client TOML or custom skill content. Keep the receipt
directory private; do not include its raw backups in support bundles. Files and
trees are bounded to 32 MiB and 10,000 entries per snapshot, and unsupported
symlinks/hardlinks are refused. Cooperating writers share the target locks; avoid
editing targets while applying/restoring. Native Windows protection uses the
shared file-protection implementation; local POSIX tests do not establish native
Windows ACL success.

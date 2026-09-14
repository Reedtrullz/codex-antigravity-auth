# Anti gateway runtime rollback reference

Captured read-only before any candidate runtime update on 2026-09-14.

## Code and service references

- Primary checkout: `/Users/reidar/Projectos/codex-antigravity-auth`, SHA `117b496db568a7c222dd1698912224c36f8264da`.
- Candidate worktree: `/Users/reidar/.codex/worktrees/dfb9/codex-antigravity-auth`, SHA `6f717279de5ce1de8c1235bb4d51cc4e4a92b834` before the liveness remediation commit.
- Launchd job: `com.codex-antigravity.gateway.51122`.
- Launchd plist: `/Users/reidar/Library/LaunchAgents/com.codex-antigravity.gateway.51122.plist`, size `939`, mtime `2026-07-27T19:29:37+0200`, SHA256 `391297d7fa13990a69e2ff29453957779956e6da11e9b1af29a074cfc9b7db09`.
- Installed Anti helper: `/Users/reidar/.codex/skills/anti/scripts/anti.py`, size `324249`, mtime `2026-09-14T12:43:56+0200`, SHA256 `33bcf6ff076e48d237af6ef7151f80b6be0794a5d816739f393aebbf456e73f7`.
- Existing service process: PID `16078`, `127.0.0.1:51122`, importing the primary checkout.

## Encrypted state references

- Accounts file: `~/.codex/antigravity-accounts.json`, size `11896`, mtime `2026-09-14T18:14:11+0200`, SHA256 `13f66995869bcf91facca5f91dac718b15ab67554c6c44ab8ddfd5390bb1d41b`.
- Providers file: `~/.codex/antigravity-providers.json`, size `804`, mtime `2026-07-27T19:34:22+0200`, SHA256 `1959f85e8a68235ab04bb885ccb904f1eaa41c116cf81d2b193214873e416b94`.
- These hashes identify encrypted state only; no plaintext credentials or tokens are recorded here.

Rollback means restoring the prior package/helper/plist and restarting the
single owned gateway after stopping the candidate. Do not blindly restore or
replace encrypted account/provider files; preserve their state and use their
hashes only to detect unintended writes. No runtime mutation was performed
when this reference was captured.

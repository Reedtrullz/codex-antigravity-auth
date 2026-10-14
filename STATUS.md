# Current source contracts

This is the authoritative status entrypoint for the checked-out source tree.
It describes source behavior, not the latest public release, deployed gateway or
live provider availability. Package version comes from `pyproject.toml`; the
[verification guide](VERIFICATION.md) generates evidence tied to an exact source
revision, actual runtime and optional distribution artifacts. Unmerged PRs are
not released features. [Historical snapshots](docs/history/2026-10-01/README.md)
preserve earlier dates, SHAs, test counts and credentialed observations.

## Route boundaries

| Route | Credential / request transport | Source contract |
| --- | --- | --- |
| Google Antigravity | Google OAuth account selection and rotation; Google content translation | `google_transport.py`, `transform.py`, `accounts.py` |
| Native OpenAI | OpenAI auth state; Responses passthrough in unified mode | `openai_native.py`, `unified.py` |
| BYOK Chat | Provider API key or explicitly local key-optional endpoint; Chat translation | `byok.py`, `openai_transport.py` |
| BYOK Responses | Explicit provider Responses wire API | `openai_transport.py`, capability declarations |

`server.py` owns dispatch/retry/lease lifetime. Provider adapters classify response
terminals through `response_protocol.py`; HTTP 200 alone is not semantic success.
Only `completed` with usable output is generation readiness. Failed/incomplete
results and refusal outcomes remain distinct; do not report catalog reachability
as generation proof. Anti is an optional advisory CLI; Codex remains the actor.

## Advertisement, transport and live evidence

1. **Advertised**: `/v1/models` lists configured identities plus catalog coverage
   diagnostics. A partial catalog does not establish that missing models are
   unavailable. Aliases and context metadata come from the
   [capability owner](codex_antigravity_auth/design/capabilities.md).
2. **Transport-supported**: the adapter can represent a request/output shape.
   Provider declarations can narrow that set. Unknown capability remains unknown;
   context declarations are not measurements of current backend limits.
3. **Live-tested**: an explicit bounded generation observed usable completion for
   one model/route/configuration at one time. Evidence expires and is not an
   entitlement or future availability guarantee. See
   [discovery and readiness](codex_antigravity_auth/design/model-discovery.md).

Google OAuth and BYOK API keys are current authentication paths. Dedicated xAI
OAuth and BluesMinds integrations were removed; configure xAI through its BYOK
API-key preset. Old migration/release notes do not re-enable removed auth flows.

## Working on this source

- [README](README.md): installation and product map.
- [USAGE](USAGE.md): command operations and explicit mutation/network options.
- [Verification](VERIFICATION.md): synthetic checks and source/artifact evidence.
- [Migration](docs/refactor-migration.md): backup and compatibility boundaries.
- [Roadmap dispositions](ANTI_IMPROVEMENTS_PLAN.md): implemented source versus
  pending portfolio work, including unmerged follow-up PRs.
- [Agent conventions](AGENTS.md): contributor rules; generated model metadata is
  checked against its owner, rather than copied into a second status table.

No new live, native service-manager, hosted CI or PyPI publication success is
claimed by this documentation change. Consult exact revision evidence for each.

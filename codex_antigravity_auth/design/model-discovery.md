# Model discovery and readiness observations

Declared capability, catalog visibility and successful text generation are
separate evidence. Existing local declarations remain usable without discovery
or internet access. No command automatically probes all models or promotes
features reported by an upstream catalog.

## Explicit discovery

```sh
codex-antigravity provider discover openrouter --json
codex-antigravity provider discover openrouter --network --timeout 10 --json
```

The first command reads a local observation only. `--network` permits a bounded
GET of the configured OpenAI-compatible provider's optional `/models` endpoint.
The adapter accepts an object with `data` entries containing model IDs. Supported
pagination conventions are `next_page_token`, `next_cursor`, or boolean
`has_more` with `last_id`; conflicting tokens, cycles and URL-based next links
are refused. Every page uses the same configured endpoint. Catalog endpoint
support is optional: missing endpoints, malformed records, timeout and partial
results have explicit statuses and fixed diagnostics. A partial catalog keeps
valid IDs already observed. Failed refreshes record that failure; they do not
remove configured models.

Limits are four pages, 2,000 records per page and distinct IDs per observation,
512 KiB per response, and a total network timeout up to 30 seconds. Redirects and
encoded/compressed responses are refused. Only sanitized bounded IDs are cached;
unknown fields, feature advertisements, body text and headers are discarded.
Common secret-like IDs and IDs echoing known transport credentials are rejected.
Discovery does not modify the model registry.

## Review before saving

```sh
codex-antigravity provider import-discovery openrouter --model vendor/model --json
codex-antigravity provider import-discovery openrouter --model vendor/model --write --accept-digest PREVIEW_DIGEST --json
codex-antigravity models import ./reviewed-models.toml --json
codex-antigravity models import ./reviewed-models.toml --write --accept-digest PREVIEW_DIGEST --json
```

Provider imports require a fresh matching discovery observation and explicitly
selected IDs. The preview lists additions and inherited provider declarations;
upstream feature advertisements are never imported. Existing per-model settings
remain intact. Local TOML imports show full validated definitions, reject
identifier collisions and append to the managed native overlay. Native family,
context and capability choices come from the supplied file, not discovery.

Saving requires the preview digest. Source or relevant current-state changes
invalidate it; checks repeat under the store lock. Overlay digests also cover
existing file bytes, including comments. A successful overlay save uses the
existing managed TOML renderer and atomic store writer. Preview alone writes no
configuration, key or observation file.

## Explanation and one-model probes

```sh
codex-antigravity models explain openrouter:vendor/model --json
codex-antigravity models probe sonnet --network --base-url http://127.0.0.1:51122/v1 --json
```

Explanation reads declarations and observations without network access. It uses
the central route/capability policy, distinguishes unsupported/missing model
configuration, and reports discovery as fresh, stale, mismatched, invalid,
missing or unsupported for routes without an adapter. Native Google and unified
OpenAI registries remain local declarations; this discovery adapter is for BYOK
OpenAI-compatible catalogs.

`models probe --network` sends one fixed, nonstreaming text-generation request
with a 16-token output cap. It records the requested canonical route/model,
capability (`text_generation`), a digest of declarations and gateway URL,
observation/expiry times, HTTP status and fixed terminal outcome. The existing
generation-readiness classifier requires completed usable assistant output;
HTTP 200 alone is insufficient. Generated text, error bodies and credential
values are not retained.

`recent_generation_ok` is true only for a fresh matching successful observation
without current configuration diagnostics. Discovery and registry membership
cannot make it true. Route identity is the declared mapping for the requested
model; the terminal outcome is measured from the gateway response. It is a
point-in-time text sample, not proof of current credentials, every capability,
upstream model identity beyond the gateway contract, or present availability.
General `availability` remains unknown. Existing doctor probes remain immediate
checks and do not automatically create these observation records.

Version-1 observation files live beside the model overlay under
`antigravity-model-observations`, with atomic private writes. Discovery expires
after one hour; text probes expire after ten minutes. Reads do not repair,
migrate or overwrite malformed/future records. Model-explanation and import
previews do not normalize secret stores on disk or print their warning labels.
JSON output can be saved for local comparison; it contains fixed diagnostics,
IDs and digests, never raw provider errors or request credentials.

`/v1/models` retains its existing picker lists and adds
`provider_catalog_diagnostics`: complete/partial/timeout/error and per-provider
skip reasons and omitted counts. A provider lookup failure does not erase native
declarations or imply a generation failure. No upstream discovery is run by the
picker endpoint.

Tests use synthetic credentials, guarded owned HTTP listeners and temporary
stores. Live provider acceptance, platform credential-store integration and
current model availability are not claimed by those tests.

# Request shapes and translation-loss contract

The boundary validates common input/message/content and function-definition
shapes before account work. Translated routes then validate their supported
item/tool types and tool-result linkage before reading provider configuration or
acquiring a Google account. Direct Google/Chat translation helpers use the same
pure checks. Errors are HTTP400 with a field path; they do not echo argument,
output or credential values. An invalid request is rejected in full.

Text input and explicit empty strings/lists remain supported. Missing input retains
the legacy empty-input default, while explicitly null input is refused on translated
routes. Message content must be explicit, text parts must contain string `text`,
and roles are user/assistant/system/developer (omitted role retains the user
shorthand). Existing attachment validation still owns MIME/bytes/role/model
capabilities; this change does not add a modality or reasoning-effort mapping.

Translated function calls need a valid name, call identity and object arguments
(or a JSON string encoding an object). Invalid JSON, duplicate argument keys,
non-object arguments and ambiguous legacy aliases are rejected. Google legacy
`tool_use`/`tool_result` content remains supported with assistant/user roles;
Chat translation requires calls as top-level `function_call` items. Tool outputs
must explicitly identify a preceding call; duplicate results, duplicate call IDs
and mismatched explicit names fail. Calls from earlier turns do not need to be
re-advertised as current tools. Valid adjacent multi-call histories retain order,
call names and correlation IDs.

`tools` must be an array. Flat Responses and nested Chat-style function definitions
remain supported, with no conflicting spellings. Descriptions, strict flags,
parameter/schema containers and common schema child shapes are checked. Translated
routes accept only the function tool/choice forms they implement; built-in and
custom tools cannot disappear in conversion.

## Schema behavior

Google's existing cleaner removes constructs including `$ref`/definitions,
`const`, `additionalProperties`, string/array constraints and `format`. Requests
using those constructs now fail at the relevant schema field with
`translation_loss`, before account work. Boolean schemas are also refused there.
`strict: true` is rejected on Google because that guarantee is not implemented.
There is no implicit constraint-dropping compatibility mode.

Purely descriptive schema annotations (`title`, `description`, `$schema`, `$id`,
`$comment`, `default`, `examples`) may still be omitted by the existing Google
adapter. Their presence is not an enforced validation constraint. The existing
internal no-required-property placeholder mechanism remains unchanged. Passing
these shape checks does not validate generated arguments or certify the upstream
backend's interpretation of every retained schema keyword.

BYOK Chat preserves parameter schemas and its explicit `strict` flag on the wire;
actual strict enforcement remains the configured provider's responsibility.
Native Responses similarly keeps schemas unchanged, including nested boolean
schemas. The common checks are not a second full JSON Schema implementation.

## Native Responses

Native provider-specific input items, built-in/custom tools and built-in tool
choices pass through unchanged for provider validation. They are not advertised
as supported by translated routes. Ordinary message media is still checked by
[the input fidelity contract](capabilities.md). Native `previous_response_id` and
its provider-owned tool-result linkage pass through; translated routes require
full history. Provider errors remain provider errors rather than silent local
normalization. Nullable optional native function fields are retained.

Structural checking is bounded to48 nesting levels and100,000 JSON nodes;
translated argument strings are limited to8Mi characters before their second
JSON parse. These are validation-work limits, not a replacement for transport body
or stream limits. Synthetic tests cover pre-account rejection, retained multi-tool
histories, real loopback wire payloads for native/Chat and unchanged media policy.
No live provider strict-schema or continuation acceptance is claimed.

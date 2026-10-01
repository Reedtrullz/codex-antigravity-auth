# Finalized function calls and internal-marker provenance

This contract validates function-call data; the gateway never executes a tool,
guesses missing arguments, repairs invalid JSON, or retries a model to repair a
call. Incoming request shape and Google schema-loss checks remain owned by
[request-shapes.md](request-shapes.md).

## Completed arguments

Chat and native Responses completed `arguments` must be a JSON string encoding an
object. Google `functionCall.args` must be an object; an omitted Google args field
retains the API's empty-object default. Arrays, scalars, empty/malformed strings,
duplicate JSON keys, nonfinite literals and unsupported values fail explicitly.
The original argument string is retained when no internal marker needs removal.

A supplied request provides function declarations keyed by namespace/name. The
validator checks that a call is declared and honors explicit `tool_choice: none`
and forced function choices. Namespace groups and completed native
`tool_search_output` / developer `additional_tools` history declarations are
recognized. Identical replay declarations deduplicate; conflicting definitions at
the same source level are refused. Explicit current tools take precedence over
historical declarations. The function-name namespace shown for unscoped loaded
functions in the official tool-search example is accepted without inserting a
namespace into the returned payload.

Opaque native `previous_response_id`/conversation state can hold declarations not
available locally. Known declarations are checked, but unknown prior function
names receive syntax validation only in that case. A standalone response helper
without any original request similarly cannot claim identity or schema validation.
A supplied empty static catalog does not authorize a generated function.

Local schema checking is bounded and deliberately limited: types (including
number/boolean distinction), required properties, properties, enums/const,
additional properties when no regex properties exist, items/prefix items,
string/array/object size bounds, numeric minimum/maximum, local references and
allOf/anyOf/oneOf. Numeric bounds compare JSON decimal values without introducing
binary-float boundary errors; integer values retain their exact precision.
Google OpenAPI-style nullable fields are recognized. Regex,
format, conditional/dependency and remote-reference semantics are not evaluated
locally. No remote schema is fetched and no regex is executed. Other constraints
remain the provider's responsibility; this is not a local strict-schema
certification or authorization to execute a call.

## Failure and streaming behavior

Duplicate provider call IDs invalidate every call sharing that identity.
Invalid calls are removed from executable output while validated sibling text,
reasoning and valid calls remain. A completed response with invalid calls becomes
`failed` with a fixed code/message that contains no argument values. A provider's
`incomplete` outcome remains incomplete, retains its reason, and also reports the
call-validation error. Usage remains available. Native item execution status and
custom free-form tool input are preserved; argument syntax is checked independently
of whether a tool's execution is still in progress.

Translated stream calls are assembled and checked before their completion events
are emitted. Native streams continue forwarding a safe nonfunction prefix, then
hold events from the first function (or an unresolved earlier output slot) until
terminal/EOF validation. Valid held events retain their original ordering. On a
call-validation failure, their completion events are withheld and the terminal
snapshot carries the usable sibling output. This may delay native tool progress,
including later text, until EOF; it prevents an invalid completed call from being
published before its validation result. Conflicting finalized argument snapshots
fail rather than choosing one. Ordinary in-progress argument strings need not yet
be valid JSON.

Google `partialArgs` is unsupported whenever present, including an empty list.
`willContinue: true` is likewise unsupported; `willContinue: false` without
`partialArgs` permits normal finalized-call validation. Unsupported partial forms
never become fabricated complete calls, and a later fragment cannot erase that failure.
This does not claim live Antigravity support for those newer Google fields.

Validation limits: 8 Mi characters of argument text, 100,000 decoded argument or
schema nodes, 48 nesting levels, 1,024 characters per numeric literal and 3,400
bits for integer values. Native pending-event retention additionally uses the
native contract's cumulative 8 MiB decoded string/key and 100,000-node budget.
Limits fail explicitly, never clip data into a successful call. These are logical
validation limits, not physical process-memory or live-provider latency claims.

## `_placeholder`

Generic argument helpers preserve `_placeholder`. Only the specific Google tool
whose original object schema had no required properties and no existing marker
property can have an injected marker removed. A supplied marker must then be a
boolean; other user arguments retain their values, including decimal precision.
If injecting the marker would overwrite a declared property, request validation
refuses the collision before account work. A legitimate marker field that needs
no injection remains intact. Chat/native routes never apply Google marker removal.

## Sources and validation

Checked 2026-10-01: OpenAI's [function-calling guide](https://developers.openai.com/api/docs/guides/function-calling?api-mode=responses)
describes JSON function arguments separately from custom-tool strings; the
[streaming reference](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.function_call_arguments.done)
distinguishes deltas from finalized arguments. [Tool search](https://developers.openai.com/api/docs/guides/tools-tool-search)
documents loaded and additional tool declarations. Google's [FunctionCall schema](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/v1/Content#FunctionCall)
uses structured object args and exposes explicit partial-call fields.

Synthetic tests cover all four routes, streaming and nonstreaming, matching
request declarations, bad/partial/valid argument objects, retained siblings,
native namespaces, custom text tools, marker collisions/provenance, exact decimal
values and failure without retry. Provider calls, actual tool execution and live
strict-schema/call acceptance are not part of this verification.

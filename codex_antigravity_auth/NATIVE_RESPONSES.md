# Native Responses output contract, version 1

Native OpenAI Responses routes preserve supported output items in provider order,
including item/call IDs, opaque continuation and assistant phase. Google and Chat
Completions translation keep their own meaningful-output rules. Native output
must not pass through that translation filter: a reasoning item without prose
can carry continuation needed by the next request.

## Supported output

| Item | Preserved fields beyond type/ID |
| --- | --- |
| `message` | assistant role, status, content, optional phase (`commentary`, `final_answer`, null) |
| `function_call` | call ID, name, argument string, optional status/namespace/async/caller |
| `custom_tool_call` | call ID, name, input string, optional status/namespace/async/caller/created_by |
| `reasoning` | summary, optional reasoning content, status, encrypted_content |
| `web_search_call` | status and search/open_page/find_in_page action |

Message content supports output text (including citations and optional logprobs)
and refusal. Citation variants are URL, file, container file and file path.
Search actions retain queries, sources, URLs and patterns. URLs in these data
fields must be HTTP(S), without embedded user credentials; the gateway does not
fetch them. Function arguments and custom input remain strings; this layer does
not execute them or validate tool schemas. Native input replay is unchanged.

Unknown item/content/action types and unknown fields within these typed objects
produce explicit `unsupported_native_*` failures. Invalid known shapes produce
`invalid_native_output`. No supported item is silently discarded to make a
response succeed. Other Responses item families, including computer, shell,
image-generation and MCP calls, are outside this initial contract. Supporting a
new family requires schema and replay tests before expanding the contract.

Compatible endpoints may omit legacy message ID/role/status, but supplied facts
must be valid. Empty anonymous message shells are rejected. Reasoning and search
items require their IDs and documented final structure. No IDs are invented for
provider items. A literal completed response with no output fails as
`empty_response`; a valid typed continuation item can be preserved without
human-readable text. That is a transport guarantee, not evidence that every
model/client supports opaque-only answers.

## Streaming and continuation

The same validation applies to JSON, buffered native SSE and streaming SSE.
Supported content, reasoning, custom/function argument and web-search lifecycle
events are forwarded, including nullable annotation-added events; unsupported
event families fail explicitly. Final outcome
still waits for EOF under the native terminal-authority rules.

Complete `response.output_item.done` snapshots are retained within bounded memory.
They may supply missing terminal items/fields, including omitted nested object
fields at matching list positions. Supplied list lengths must agree. Supplied terminal identity, type
and non-null fields must agree with the complete snapshot; conflicts fail rather
than selecting one possible continuation. In particular, encrypted content from
an `output_item.added` event is never treated as the complete reasoning item.
The gateway neither decrypts opaque content nor puts it in diagnostic messages.
Failure diagnostics identify the contract version without echoing rejected data.
Provider failure messages remain generic.

Each decoded envelope/output and the aggregate retained complete items are
limited to 8 MiB of UTF-8 string/key bytes, 100,000 JSON nodes, nesting depth 32,
and arrays of 10,000 elements. IDs and stream identity bookkeeping retain the
existing limits. Non-finite numbers, invalid Unicode and non-JSON values fail.
These are output validation limits, not whole-request admission or response-body
allocation limits. Incremental framing has its own pending-frame bounds.

## Evidence and compatibility boundary

The public OpenAI [reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)
and [input-item schema](https://developers.openai.com/api/reference/java/resources/responses/subresources/input_items/methods/list)
describe retaining complete reasoning items and assistant phase for replay.
The [web-search guide](https://developers.openai.com/api/docs/guides/tools-web-search),
[response output schema](https://developers.openai.com/api/reference/java/resources/responses/methods/create)
and [streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events)
provide the typed shapes used here (inspected 2026-10-01).

Regression tests use synthetic ciphertext, responses and transports only. They
cover byte-preserving field values, item order, request replay, complete-item
reconciliation and explicit rejection. No live credentialed provider or Codex
client acceptance test is claimed. Protocol changes can require a contract
update; arbitrary future fields do not acquire support by passing JSON parsing.

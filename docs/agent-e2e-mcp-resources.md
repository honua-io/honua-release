# Native MCP resource transport component

This source component adds `mcp_list_resources {}`, `mcp_list_resource_templates {}`,
and `mcp_read_resource {uri}` to the existing controller/worker transport. It is a
prerequisite for observing the actual `honua://proposals/{proposalId}` resource;
it does not implement a P16 producer or qualify its lifecycle assertions.

The default model tool list, installed runtime action policies, task credentials,
frozen packs, action budgets, campaigns and component pins are unchanged. A trusted
caller may pass an already reviewed runtime operation policy to `BedrockModel` and
`McpActions`. Only resource actions explicitly present in its `allowedActions` are
advertised and dispatched. An absent policy admits none. No installed profile is
changed or admitted by this patch. Actual profile approval and authority for P16
issuance, private consumption and separately approved revocation remain unresolved
under release issue #1000; this component grants none of those permissions.

The URI is a bounded native Honua identifier, never a destination for HTTP, file or
other fetching. All requests use the existing pinned MCP endpoint, original worker
credential, session and native agent/execution context. Server resource authorization
still decides access. Resource discovery uses native opaque cursors with at most
32 pages, 1,024 descriptors and a 512 KiB merged catalog. Reads require a complete
bounded result, matching content URIs and text or base64 blob content; a read cursor
is refused. The existing deadline, no-redirect transport, per-response and session
byte bounds apply. Resource bytes receive literal/JSON-escaped, percent-decoded and
base64-blob credential screening before raw response retention, including JSON
property names and complete SSE comments. A nonempty incomplete SSE tail is
refused before retention; accepted native response bytes are never rewritten.

Native resource contents and RPC errors are preserved. A pending proposal remains
pending; no operation completion, approval, key identity or effect is synthesized.
The original canonical operation instance must eventually be observed from actual
separately approved replay. `ExecutionOperationId` is not treated as an instance
handle. No controller approval or model observation message injection is added.

Resource actions use the existing durable intent, fenced worker launch, chunked
reply evidence and action identity. Changed requests fail before dispatch. Retained
outcomes cannot be redispatched. A worker lacking resource support refuses the new
action; the controller preserves that error without fallback. New resource-capable
worker bytes still need a reviewed candidate/image before use in any real campaign.

`tools/test_agent_e2e_mcp_resources.py` exercises loopback JSON/SSE, paginated native
catalogs, actual worker queue/chunk/evidence roundtrips, pending content, resource
errors, bounded/foreign/partial results, credential echoes, policy refusal, legacy
worker refusal and durable replay rejection. These local fixtures are transport
regressions, not live qualification receipts.

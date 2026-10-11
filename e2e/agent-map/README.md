# Honua agent map (real-world harness)

Reference harness for the 2026.1 promise journey (honua-release#376, #378): a genuine model composes a map
through the published Honua SDK and MCP server. It is a developer tool, not a certification gate.

A prompt-to-map loop built only on published packages and public endpoints:

- `app/` — Vite app scaffolded with `npm create honua-app@latest`, upgraded to `@honua/sdk-js@latest`.
  It connects to the public Maui layers on `https://demo.honua.io` (discovered with `@honua/mcp-server`'s
  `honua_list_sources`), mounts them with `mountSource` from `@honua/sdk-js/map`, styles with
  `@honua/sdk-js/style` renderers, and exposes the SDK's AI map kit (`createHonuaAiMapKit` from
  `@honua/sdk-js/agent-tools`) over a small runtime adapter. The page has a prompt box.
- `agent-drive.mjs` — boots Vite plus a loopback control channel (`127.0.0.1:47811`). The page long-polls it
  for jobs, so the map can run in any browser (Windows Chrome from WSL works).
- `agent-loop.mjs` — the model turn: a manual Claude tool-use loop over `kit.mcpTools`, executed through the
  page, using the Anthropic Bedrock SDK (`AnthropicBedrock`, model from `HONUA_AGENT_MODEL`).
- `mcp-call.mjs` — a stdio MCP client for `npx -y -p @honua/mcp-server honua-mcp`
  (`node mcp-call.mjs <baseUrl> <tool> '<json>'`).

```bash
npm install            # also installs app/
npm start              # then open http://localhost:5199 and type a prompt
```

Playwright is optional (`CDP_URL` / `POST /attach`); screenshots otherwise come from the map canvas
(`POST /shot`). Control API: `GET /tools /prompt /context /viewport /status`, `POST /call /chat /shot /stop`.

## Credentials and safety

- The model is called through AWS Bedrock with your ambient AWS credentials (`AWS_REGION`, profile or SSO).
  Nothing is stored in this directory. `HONUA_AGENT_MODEL` selects the model ID.
- The control channel listens on `127.0.0.1` only and accepts browser requests only from the Vite page it
  started, so another website open in the same browser cannot drive the agent.
- Tool results and layer titles are rendered as text, never as HTML.

## M1 dogfood recording

Follow [the developer-preview dogfood runbook](../../docs/DEVELOPER-PREVIEW-DOGFOOD.md)
for prerequisite verification, the $25 per-run ceiling, exact operator prompts,
receipts, rollback and tagged teardown. Claims are **developer preview;
certification in progress**. The Console is display-and-approve; no dashboards
are claimed. Studio and mixed topology are Preview, EKS is a GA target, qualification pending ([release#203](https://github.com/honua-io/honua-release/issues/203)), and
`customer-install-manifest.json` stays `pre-cut-rehearsal`.

This harness calls Bedrock directly and renders hardcoded public-demo sources.
It does not install ECS, import/publish a new layer, persist/publish a map, roll
back or tear down. It does not prove the candidate server's StudioAi Bedrock
adapter. Record it separately; use the runbook's discovered CLI/SDK/MCP operations
for the AWS cell. If an operation is unavailable, mark it blocked rather than
claiming that the public demo completed it.

For credential-free checks, run `npm --prefix app run typecheck` and
`npm --prefix app run build` after installation; use the runbook's local Docker
rehearsal for server observations. Starting the harness without submitting a
prompt needs no AWS credentials, but rendering needs a browser plus access to
the public demo and basemap. A genuine-model turn requires scoped Bedrock access
and an explicitly approved `HONUA_AGENT_MODEL` (do not assume the default matches
that grant). Review transcripts and screenshots before attaching: no secrets,
DSNs, presigned URLs or customer-identifying data. Record the final ordinary map
URL before teardown, and distinguish a local browser URL from a published URL.

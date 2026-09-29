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

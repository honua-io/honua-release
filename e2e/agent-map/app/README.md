# Agent-composed Maui map (Vite + TypeScript)

The browser half of the agent-map harness. It builds a map only from the published
[Honua JavaScript SDK](https://github.com/honua-io/honua-sdk-js):

- `connect()` opens each public Maui FeatureServer layer on `https://demo.honua.io/rest/services`. The catalog in
  `src/main.ts` is what `@honua/mcp-server`'s `honua_list_sources` returned for the demo.
- `mountSource()` (`@honua/sdk-js/map`) mounts layers onto a MapLibre map over the OpenFreeMap "liberty" basemap.
- `@honua/sdk-js/style` renderers and `createHonuaAiMapKit` (`@honua/sdk-js/agent-tools`) expose the bounded agent
  tool plane. Every tool call is audited and shown in the activity panel.

The page polls the driver's loopback control channel (`VITE_CONTROL_URL`, default `http://127.0.0.1:47811`) for
jobs. Start it through the harness root (`npm start` one level up), not on its own.

## Network

The app needs network access to `demo.honua.io` and to the OpenFreeMap basemap. There is no offline mode. The
`fixtures/` directory and the fixture middleware in `vite.config.ts` are left over from the `create-honua-app`
scaffold and aren't used by this app.

## Dependencies

`@honua/sdk-js` and `maplibre-gl` are the two packages the app calls directly. `@bufbuild/protobuf` and the two
`@connectrpc` packages are the SDK's optional transport peers. MapLibre GL JS is pinned to 6.11.2; 6.1.0 carried the
GHSA-jrc7-96c5-q579 XSS advisory. MapLibre 6 is ESM-only, so `src/maplibre-worker.ts` sets the worker URL before the
first map is created.

## Checks

```bash
npm run typecheck
npm run build
```

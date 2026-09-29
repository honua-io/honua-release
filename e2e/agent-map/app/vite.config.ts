import { readFileSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import { fileURLToPath } from "node:url";

import { type Plugin, defineConfig } from "vite";

import { FIXTURE_LAYER_PATH } from "./src/fixture-endpoint.js";

function fixtureDocument(name: string): string {
  return readFileSync(fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url)), "utf8");
}

/**
 * Deterministic, offline GeoServices FeatureServer fixture.
 *
 * The starter's green path must never depend on a third-party endpoint, so the
 * dev and preview servers answer the two routes the SDK's GeoServices adapter
 * needs: the layer description and its bounded query. Point
 * `VITE_HONUA_ENDPOINT` at a real service to run the same code against live data.
 */
function honuaFixtureService(): Plugin {
  const layer = fixtureDocument("layer.json");
  const features = fixtureDocument("features.json");
  const middleware = (request: IncomingMessage, response: ServerResponse, next: () => void): void => {
    const pathname = new URL(request.url ?? "/", "http://localhost").pathname;
    let body: string | undefined;
    if (pathname === FIXTURE_LAYER_PATH) body = layer;
    else if (pathname === `${FIXTURE_LAYER_PATH}/query`) body = features;
    if (body === undefined) {
      next();
      return;
    }
    response.setHeader("content-type", "application/json; charset=utf-8");
    response.setHeader("cache-control", "no-store");
    response.end(body);
  };
  return {
    name: "honua-fixture-service",
    configureServer(server) {
      server.middlewares.use(middleware);
    },
    configurePreviewServer(server) {
      server.middlewares.use(middleware);
    },
  };
}

export default defineConfig({
  plugins: [honuaFixtureService()],
});

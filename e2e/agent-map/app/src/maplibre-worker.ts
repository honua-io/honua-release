// MapLibre GL JS 6 is ESM-only and loads its worker as a separate module,
// resolving `./maplibre-gl-worker.mjs` against its own `import.meta.url`. That
// URL survives a dev server but not a production bundle, where MapLibre's own
// module has been rewritten into an app chunk that sits nowhere near the
// installed package and its sibling worker file. Vite's
// `?worker&url` pipeline emits the worker as a real asset and hands back the
// hashed URL it was emitted to, which is what `setWorkerUrl` needs.
//
// Import this module before the first map is created; `src/main.ts` does.

import { setWorkerUrl } from "maplibre-gl";
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

setWorkerUrl(maplibreWorkerUrl);

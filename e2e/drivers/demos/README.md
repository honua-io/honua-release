# S9 — honua-site demos, driven against the seeded candidate

`run.sh` + `drive.mjs` drive the five backend-facing honua-site demos headlessly against the candidate
server booted by `e2e/harness/run_all.sh`:

| scenario | what it asserts |
| --- | --- |
| `S9-demos-shim-security` | the honua-site backend-override shim rejects a non-allow-listed `?apiBase=`, does **not** widen the CSP for it, the re-emitted CSP really blocks that connect, and the allow-listed origin **is** accepted and added to `connect-src` |
| `S9-demos-two-protocols` | GeoServices, OGC API Features and OData lanes each return the same non-zero feature count from the candidate, and the page's own "identical results from both protocols" line agrees |
| `S9-demos-esri-leaflet` | unmodified esri-leaflet renders real features from the candidate's GeoServices surface, and `L.esri.query().intersects()` on a map click returns live parcel attributes into the popup + code strip |
| `S9-demos-geoprocessing` | the live OGC API Processes catalog loads anonymously, and `generalization.simplify-layer` submits → polls → returns a successful job with a real result document |
| `S9-demos-editing` | `maui-inspections` is read live over OData, an edit is applied and survives reopening, the literal `PATCH` is emitted against the candidate, and the page's write-lane claim matches the candidate's own `edit.features` capability (when writes are advertised live, the server row must echo the change) |
| `S9-demos-analyst-workbench` | the workbench's default GeoServices remote-pushdown policy executes explain → accept → execute against the candidate and returns aggregate rows |

`maui-3d` and `sdk-controls` are out of scope by design: they demonstrate SDK-native rendering, not a
backend contract.

## How it works

The demos are **not** modified, stubbed, or intercepted. A local static copy of honua-site is served on
its own origin — a *different* origin from the candidate, exactly as `honua.io` → `demo.honua.io` is in
production — and each page is opened with honua-site's backend-override shim engaged:

```
http://127.0.0.1:$E2E_SITE_PORT/demo-two-protocols.html?apiBase=$E2E_BASE
```

`assets/demos/backend-override.js` in honua-site validates that origin against a tight allow-list
(loopback + `*.honua.io`), re-emits the page CSP widened by exactly that origin, and repoints the demo
config. Everything after that is the demo's own code talking to the candidate.

## Prerequisites

* a honua-site checkout carrying the shim — `E2E_SITE_DIR`, else a sibling `../honua-site`, else a
  shallow clone of `E2E_SITE_REF` (default `trunk`)
* Node + Playwright — installed on demand into `E2E_PW_HOME`
  (default `~/.cache/honua-e2e-playwright`), pinned by `E2E_PLAYWRIGHT_VERSION`
* the candidate must allow the demo origin: `run_all.sh` picks `E2E_SITE_PORT` **before boot** and
  passes `E2E_SITE_CORS_ORIGINS` into the compose profile

A missing prerequisite is `blocked`; `E2E_REQUIRE_REAL` promotes `blocked` to `fail`, so S9 is a real
gate on a real cut. There is no opt-in switch and no way to make S9 unfailable.

## Known server-side gap (2026.1-rc.2)

`S9-demos-analyst-workbench` fails against `nightly-aot-84ee2a5`. The published
`spatial-analytics-workbench` SDK sample that `demo-analyst-workbench.html` loads sends

```
outStatistics=[{ "statisticType":"count","onStatisticField":"OBJECTID","outStatisticFieldName":"feature_count" }, …]
groupByFieldsForStatistics=risk
orderByFields=feature_count DESC
```

and honua-server's GeoServices adapter validates `orderByFields` against the layer's real fields plus a
small core allow-list, so ordering by an `outStatistics` **alias** is rejected with a 400 — something
ArcGIS accepts. The driver proves this is server-side, not a seeding or shim artefact, by re-running the
same demo on its `bounded-local` execution policy (which omits `orderByFields`): that lane executes live
against the same seeded layer and produces real aggregate rows. The failure is left standing on purpose.

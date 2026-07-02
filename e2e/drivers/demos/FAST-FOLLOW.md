# S9 demos — fast-follow (Slice 1 deferral)

The top-5 driveable honua-site demos (`two-protocols`, `geoprocessing`, `editing`,
`analyst-workbench`, `esri-leaflet`) are NOT driven in Slice 1. Native demos (`maui-3d`,
`sdk-controls`) are out of scope by design.

## Why deferred
1. **Hardcoded backend + CSP.** Every demo points at `https://demo.honua.io` and the demo HTML sets a
   `Content-Security-Policy: connect-src` that only allows that origin (plus tile hosts). A naive base-URL
   swap is blocked by the CSP, so the browser must be told to redirect at the network layer.
2. **No override shim exists yet.** There is no `window.HONUA_DEMO_BASE_URL` / `?apiBase=` hook in
   honua-site today — adding one is a cross-repo change (honua-site), outside this honua-release slice.
3. **Larger data contract.** The demos assert `maui-parcels`, a 6-layer zoning service, `maui-inspections`,
   and imagery — well beyond the Slice-1 seed (`maui_zoning` + `e2e_src_fs`).

## The plan (fast-follow)
- **Option A (preferred): Playwright `page.route`** to redirect `**demo.honua.io/**` →
  `E2E_BASE`, bypassing the CSP without touching honua-site. Add per-demo assertions
  (rendered features / GP result / edit round-trip visible in the DOM).
- **Option B: honua-site shim** — read `window.HONUA_DEMO_BASE_URL` (settable via `?apiBase=`) and
  widen the CSP `connect-src` when it is set. This also unblocks manual demoing against any server.
- **Seed extension** — add `maui-parcels`, the 6-layer zoning service, and `maui-inspections` to
  `e2e/harness/seed/seed.sh` (source values from honua-site `assets/demos/*/config.json`).

Tracking: file as `honua-release` issue "e2e S9: drive top-5 demos against seeded server (page.route + seed extension)".

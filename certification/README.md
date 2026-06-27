# certification/

Cross-repo certification gates run by the release train against a candidate manifest:
- **Conformance** — Esri GeoServices / OGC (CITE) / STAC suites with *real* assertions (no false-pass),
  pointed at the candidate server.
- **Contract** — proto/REST/SDK breaking-change detection; `version-contract-drift`.
- **Artifact-consumption** — install/consume every published artifact (npm/nuget/pip/terraform/helm/docker/codegen).

These produce entries in the release train's `gate-report.json`. To build: promote each component repo's
existing checks into reusable `workflow_call` gates and invoke them here. (Stub — Phase 1/2.)

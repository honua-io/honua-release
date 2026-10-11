certification: prove every gated capability both ways in the entitlement lane (release#64)
## Summary

release#64 proposed an entitlement sweep that treats any non-402 answer at or above the required
edition as "allowed". That accepts a 404 or a 500 as proof of entitlement. This adds the
`conformance-entitlements` job to the existing candidate certification gate (`certification.yml`,
gate (c)) with exact assertions in both directions for every gated capability:

- **Positive.** A signed license carrying the real entitlement key gets the capability's documented
  success: status, media type and body shape (SCIM `ListResponse`, SAML `EntityDescriptor`, PNG
  signature, `ApplyStarted` SSE event, `FeatureCollection` with features, `202 queued` scan job,
  `201` scene ingest, and so on). A 404, a 500, a validation error or a GeoServices error inside a
  200 fails.
- **Negative.** A license without the key gets exactly the documented denial
  (honua-server `docs/concepts/editions-and-licensing.md#how-gating-responds`): HTTP 402
  `application/problem+json` naming `entitlement: <key>` and `requires an active <edition>
  entitlement`; on GeoServices routes, the Esri 200 envelope with `error.code: 402` and the same key
  and edition in `error.details`.

Cloud-matrix expansion is out of scope; the lane runs on the existing local candidate harness
(`e2e/harness/compose.candidate.yml` + `seed.sh`) with a licensing overlay.

## Fixtures (fingerprints only)

`fixtures.py` mints three envelopes from honua-server's own licensing test corpus
(`tests/dotnet/Honua.TestKit/Helpers/LicenseTestSupport.cs`, test-only key `test-key`), fetched at the
candidate's exact commit. The minter refuses the corpus unless its public key matches the committed
trust-anchor fingerprint. Envelopes stay in a temp dir; the report carries `sha256:` fingerprints
only. No license material or signing seed is committed (a test enforces this).

| fixture | label | grants | negative role |
|---|---|---|---|
| `entitled` | Enterprise | every catalog key | — (positives) |
| `pro-tier` | Pro | Pro + Community keys | each Enterprise key denies with its own key while its Pro prerequisite is active |
| `keyless` | Enterprise | Community keys + declared prerequisites | each Pro key denies; the label alone activates nothing |

Each stack must report licensing `enabled`, `Valid`, the fixture's license id and edition, and
exactly the fixture's grant before it is probed. The 2026.1 deployment posture
(`Licensing__Mode=Disabled`) and the train inputs are unchanged.

## Entitlements covered

The probe table is a snapshot of `FeatureCatalog` at the pinned server commit (62 gated keys). Every
gated key has exactly one probe or a documented no-HTTP-surface row; a key without a row, or any
difference between the live candidate catalog and the snapshot, fails the lane.

Probed both ways (45), geocoding included:
`admin.multi-tenancy`, `ai.grounding`, `ai.spec-apply`, `alerts.dwell`, `alerts.enter-exit`,
`alerts.evaluation`, `alerts.threshold`, `analytics.buffer-aggregate`, `analytics.clustering`,
`analytics.density`, `analytics.h3`*, `analytics.line-of-sight`*, `analytics.slice`*,
`analytics.spatial-join`, `analytics.sun-shadow`*, `analytics.viewshed`*, `channels.aws-sns`,
`channels.azure-eventgrid`, `channels.digest`, `channels.email`, `channels.slack`, `channels.teams`,
`channels.webhook`, `editing.branch-versioning`, `editing.featureserver-edits`,
`fieldops.offline-sync`, `geocoding.batch`, `identity.mtls-client-certificate`, `identity.oidc`,
`identity.oidc-multi-provider`, `identity.saml`, `identity.scim`, `import.geoserver`*,
`import.geoservices`*, `printing.layout-templates`, `printing.pdf-output`,
`raster.multidim-coverage`, `scene.bim-ingest`, `scene.pointcloud-ingest`, `staticmap.high-dpi`,
`staticmap.large-dimensions`, `staticmap.rich-overlays`, `streaming.feature-subscriptions`,
`temporal.histogram`, `temporal.time-series-tiles`.

\* Negative asserted; positive reports **blocked** (never pass) because the lane does not provision
the infrastructure: elevation raster (four elevation analytics), the h3 extension (`analytics.h3`),
an HTTPS import source (`import.*`).

No HTTP 402 surface (17, reasons in `probes.v1.json`, mirroring honua-server's
`EntitlementProbeRegistry`): `ai.agent-operations`, `ai.approval-workflows`, `caching.output-cache`,
`caching.redis`, `dr.backup-automation`, `dr.cache-backup`, `dr.failover`, `dr.rto-rpo-reporting`,
`geocoding.failover`, `identity.claims-mapping`, `plugin.sdk`, `raster.cloud-cog-serving`,
`raster.cloud-storage-config`, `raster.temporal-mosaic`, `routing.solve`, `styling.auto-suggest`,
`temporal.animation-api`.

## Live result against the pinned candidate

Run locally (Docker) against `ghcr.io/honua-io/honua-server:nightly-93cad86@sha256:785de2ae…`
(and earlier against `798d517`, same result):

- 38/38 provisionable positives pass; 7 blocked as above.
- 35/45 negatives pass.
- **10 negatives fail — a candidate defect, not a lane defect.** Creating an alert rule without
  the trigger/channel entitlement returns 402 naming the key but not the required edition
  (`"The active license does not include the entitlements this alert rule requires. entitlement:
  alerts.dwell"`), unlike every other gate and unlike the documented denial. Affected:
  `alerts.dwell`, `alerts.enter-exit`, `alerts.threshold`, `channels.aws-sns`,
  `channels.azure-eventgrid`, `channels.digest`, `channels.email`, `channels.slack`,
  `channels.teams`, `channels.webhook`. Expect the gate fragment to read `fail` until the server
  message names the edition (honua-server `AlertAdminEndpoints`).

## Follow-ups

- honua-server: alert-rule 402 detail should name the required edition (the 10 failures above).
- honua-server image: `/app/scenes-generated` is not writable by the non-root user, so scene ingest
  fails with 500 in a default container; the overlay mounts a tmpfs.
- Harness: on first boot the server can start during the PostGIS image's init restart and never
  reconnects (Development mode keeps it unready); `lane.sh` restarts it once after two minutes.
- `geocoding.batch` positive uses the default public Nominatim provider; a local stub would remove
  the external dependency.
- Fixtures for the seven blocked positives (DEM raster publish, h3-enabled PostGIS, HTTPS import
  source).

## Verification

```bash
python -m pytest certification/entitlements/test_entitlements.py -q          # 37 passed
PYTHONUTF8=1 python -m pytest tools/test_workflow_contracts.py tools/test_action_pins.py \
  e2e/test_licensing.py certification/test_conformance.py -q
bash certification/entitlements/lane.sh <LicenseTestSupport.cs at the pinned sha> <out-dir>
```

# Entitlement certification (release#64)

The `conformance-entitlements` job of the certification gate proves the edition model the candidate
actually enforces. It replaces the "non-402 means allowed" sweep proposed in release#64, which would
have accepted a 404 or a 500 as an entitled answer. Every gated capability is asserted both ways:

- **Positive.** A license that carries the real entitlement key gets the capability's documented
  success: the exact status, media type and body shape (for example a SCIM `ListResponse`, a PNG
  signature, an `ApplyStarted` event, a `FeatureCollection` with features). Anything else fails,
  including a 404, a 500, a validation error or a GeoServices error inside a 200.
- **Negative.** A license without the key gets exactly the documented denial
  (`docs/concepts/editions-and-licensing.md#how-gating-responds` in honua-server), naming the probed
  key and its required edition:
  - REST surfaces: HTTP 402, `application/problem+json`, `status: 402`, `title: Payment Required`,
    `detail` containing `entitlement: <key>` and `requires an active <edition> entitlement`.
  - GeoServices surfaces (`/rest/services/...`, `/tiles/...`): the Esri wire convention, HTTP 200
    with `error.code: 402`, `error.message: Payment Required` and the same key and edition in
    `error.details`.

## Fixtures

`fixtures.py` mints three signed license envelopes from the server's own licensing test corpus
(`tests/dotnet/Honua.TestKit/Helpers/LicenseTestSupport.cs`, key id `test-key`), fetched at the
candidate's exact honua-server commit. That key is public and test-only; the stacks this lane boots
trust it and nothing else. The minter refuses the corpus unless its public key matches the committed
trust-anchor fingerprint in `fixtures.v1.json`. Envelopes live in a temporary directory for the
duration of the job and are never committed or uploaded: the report records `sha256:` fingerprints only.

| fixture | label | grants | proves |
|---|---|---|---|
| `entitled` | Enterprise | every catalog key | each capability's documented success |
| `pro-tier` | Pro | Pro + Community keys | each Enterprise capability denies with its own key, even where its Pro prerequisite (for example `identity.oidc` under `identity.oidc-multi-provider`) is active |
| `keyless` | Enterprise | Community keys only | each Pro capability denies; the Enterprise label alone activates nothing |

Each stack is checked before probing: licensing `enabled`, `validationState: Valid`, the fixture's
license id and edition, and exactly the fixture's grant active.

## Coverage

`probes.v1.json` holds a catalog snapshot (`FeatureCatalog` at the pinned server commit) and one row
per gated key: either a probe or a documented no-HTTP-surface reason (17 keys, mirroring honua-server's
`EntitlementProbeRegistry`). The verdict fails when a gated key has no row, when a row names a
non-catalog key, or when the live candidate catalog (`GET /api/v1/admin/license/entitlements`)
differs from the snapshot, so a new entitlement cannot ship unproven.

45 capabilities are probed, geocoding included (`geocoding.batch` through
`GeocodeServer/geocodeAddresses`; `geocoding.failover` has no 402 surface by design). Seven
positives need infrastructure the lane does not provision and report **blocked**, never pass; their
negatives still run:

| key | why the positive is blocked |
|---|---|
| `analytics.line-of-sight`, `analytics.viewshed`, `analytics.sun-shadow`, `analytics.slice` | need a published elevation raster; the harness publish API is vector-only |
| `analytics.h3` | needs the h3 PostgreSQL extension, absent from the harness PostGIS image |
| `import.geoservices`, `import.geoserver` | need a reachable HTTPS source server |

The `geocoding.batch` positive uses the server's default geocoding provider (public Nominatim).

## Running it

```bash
python -m pytest certification/entitlements/                      # verdict self-test, no Docker
bash certification/entitlements/lane.sh <LicenseTestSupport.cs> <out-dir>   # live, needs Docker
```

`lane.sh` boots `e2e/harness/compose.candidate.yml` with `compose.licensed.yml` (licensing enabled,
the fixture, and the opt-ins and delivery-channel settings that put each gated route in reach), seeds
it with `e2e/harness/seed/seed.sh`, runs `run.py observe`, tears it down, and finally runs
`run.py verdict`. Set `E2E_SERVER_PORT` and `HONUA_SERVER_IMAGE` to run beside other stacks.

The 2026.1 deployment posture stays `Licensing__Mode=Disabled`; this lane certifies the candidate
image's gating on isolated stacks and adds no license input to the train or any deployment.

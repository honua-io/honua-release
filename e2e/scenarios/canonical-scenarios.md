# Canonical platform scenarios

Each scenario is seeded from a real audit finding so it becomes a permanent regression test, and is the
*executable* form of a compatibility-matrix row. Run across: tier {local-docker, cloud} × deploy-target ×
SDK {js, dotnet, python} × compat-window — but decomposed (see TEST-STRATEGY.md), not full cross-product.

| Scenario | Asserts | Seeded from |
|---|---|---|
| **Sync round-trip / no-duplicates** | edit → sync → edit → sync → restart → sync ⇒ exactly ONE server feature | honua-collect#102 |
| **GeoServices error surfacing** | force a 200+`{error}` ⇒ every SDK raises (not success); `honua_geoservices_error_total` increments | sdk-js#309, sdk-python#122, server#2243 |
| **gRPC-web authenticated call** | query over grpc-web with apiKey ⇒ authorized; timeout/retry honored | sdk-js#308 |
| **Published-artifact consumption** | npm/nuget/pip install from staging; `terraform init` customer tarball; `docker run`+/healthz; run codegen | sdk-js#310, iac#81, grpc#45 |
| **Contract-compat window** | SDK v(N-1) against server vN ⇒ pass within supported window | version-contract-drift |
| **Upgrade** | prior platform release → candidate ⇒ migrations fwd+rollback, zero-downtime, old clients work | DB/config-across-versions |
| **Deploy-target parity** | the canonical set runs identically on every deploy target | platform promise |

Reference target for the heavy SDK × scenario matrix = **local docker** (free, per-PR). Cloud targets run the
slim canonical set for **deploy-target parity** + infra seams, nightly. AWS-first build order (see TEST-STRATEGY.md).

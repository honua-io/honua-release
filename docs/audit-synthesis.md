# Honua Platform Release Audit — Synthesis & Release-Readiness Memo

Scope: 12 release-affecting repos (honua-server audited separately). 86 review agents + verifiers across 3 waves.
Every S1 verified against `origin/<default>` (the release branch). Date: 2026-06-27.

## Headline
**312 findings — 0 S0, 16 S1, 197 S2, 99 S3.** No catastrophic (S0) defects in any audited repo — a good release signal.
The 16 S1s are the release gate; 12 GitHub issues filed (collect's 5 sync facets consolidated into one).

## Scorecard
| Repo | Status | S1 | S2 | S3 |
|---|---|--:|--:|--:|
| honua-sdk-js | 🔴 | 3 | 15 | 6 |
| honua-collect | 🔴 | 5 | 18 | 9 |
| geospatial-grpc | 🔴 | 2 | 15 | 5 |
| honua-console | 🔴 | 1 | 15 | 5 |
| honua-devops | 🔴 | 1 | 14 | 6 |
| honua-esri-compat | 🔴 | 1 | 25 | 8 |
| honua-iac | 🔴 | 1 | 15 | 11 |
| honua-sdk-python | 🔴 | 1 | 16 | 9 |
| geospatial-mcp | 🔴 | 1 | 16 | 6 |
| honua-helm | 🟡 | 0 | 18 | 14 |
| honua-mobile | 🟡 | 0 | 17 | 9 |
| honua-sdk-dotnet | 🟡 | 0 | 13 | 11 |

## Cross-cutting themes (fix once, benefit everywhere)
1. **Errors that look like success (~62 findings).** GeoServices/Esri return errors as HTTP 200 with an `{error}` envelope; clients that don't inspect the body treat failures as data. Confirmed in sdk-js (#309) and sdk-python (#122); esri-compat cert lane false-passes image checks (#25). **Action: audit honua-sdk-dotnet for the same pattern; add a shared "is this an Esri error envelope?" guard to every client + the cert harness.**
2. **Weak gates / false conformance (~73).** Gates that don't gate: grpc breaking-change gate misses RPC deletion/rename (#44), grpc `buf generate` is outright broken (#45), mcp conformance reports FULL while ignoring coverage (#25/geospatial-mcp), esri-compat cert false-pass (#25). These mask other defects — fix first.
3. **Shipped-artifact integrity (~43).** Artifacts that don't actually build/install: sdk-js CDN/browser bundle never built in CI (#310), iac customer tarball breaks `terraform init` for all examples (#81), grpc codegen broken (#45). **Add a "consume the published artifact" smoke test to each release pipeline.**
4. **Idempotency / duplicate-on-sync (~44).** No idempotency keys → duplicate server features on retry/restart/re-edit. honua-collect (#102, 5 facets); mobile sync paths adjacent. Data-integrity; release-critical for field collection.
5. **Over-advertised docs/claims (~42).** Recurring platform pattern: mobile ships a fabricated "AR utility-visualization example" (README only, cites a non-existent SDK) listed as a delivered capability; parity overstatements. Credibility risk at launch — reconcile every "Current Capabilities" claim with code.
6. **Auth/authz gaps (~39).** Console uses one shared admin key with no console authn/authz (#233); sdk-js grpc-web transport sends no credentials (#308).

## Release gate
- **0 S0** — no hard blockers.
- **16 S1 across 9 repos** — must be fixed or explicitly waived with owner sign-off before release. Highest release impact: collect duplicate-features (#102), sdk-js CDN bundle (#310) + grpc-web auth (#308), the 200-OK error-envelope cluster (#309/#122), iac terraform-init break (#81).
- **197 S2** — triage; fix high-impact, backlog the rest. **NOT yet adversarially verified** (only S0/S1 were double-checked) — confirm before acting.
- **helm / mobile / sdk-dotnet** are closest to green (0 S1).

## Coverage caveats (honest gaps)
- honua-server excluded (separate track).
- Large repos had honestly-reported unreached areas: sdk-js webmap/XML(XXE) parsers + esri-compat layer + web-components XSS sinks; console business logic; mobile TS embed + viewmodels; many request/response model types live in upstream `Honua.Sdk.*` NuGet (not in the mobile repo) so some contract checks were out of reach. A second deeper pass on those hot spots is worth it for the 3 largest repos.
- Apache-2.0 vs Elastic License 2.0: honua-mobile is Apache-2.0 (not ELv2) — confirm intended licensing per repo.

## Recommended next steps
1. Fix the 16 S1 (start with themes 2 & 3 — they unblock trustworthy CI).
2. Cross-SDK pass on the 200-OK error-envelope guard (incl. sdk-dotnet).
3. Triage the 197 S2 into fix-now / backlog; adversarially verify before fixing.
4. Optional Wave 3: deep second pass on sdk-js / console / mobile unreached hot spots.

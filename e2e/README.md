# e2e/

Cross-component & cross-cloud integration/parity harness — the *executable compatibility matrix*. Real
server + DB + SDKs installed from a staging registry, NO mocks at the seams; deployed via the actual IaC to
real cloud targets for parity.

- `scenarios/canonical-scenarios.md` — the scenario list (seeded from audit findings).
- Tiers: **local-docker** (per-PR, full SDK × scenario) → **cloud parity** (nightly, slim canonical set per deploy target).
- Deploy targets: local docker, AWS {serverless, ECS, EKS}, Azure {ACA, AKS, Functions} — axis-decomposed (see docs/TEST-STRATEGY.md).
- Build order: AWS-first (you have credits). OIDC (no static creds), ephemeral envs, teardown reaper, cost guardrails.

Stub — Phase A (local-docker) first. (See docs/TEST-STRATEGY.md.)

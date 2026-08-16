# Honua Platform Release Engineering & Change Management Plan

**Thesis:** Honua is sold as a *platform* — many independently-built components (server, SDKs, console, mobile, collect, gRPC, MCP, IaC, Helm) that customers deploy and upgrade themselves. The product promise is **safe deployment across environments and safe version upgrades** — "it doesn't suck to operate." That promise is only real if release engineering *guarantees the components work together and evolve compatibly*. This plan makes the platform-ness enforceable instead of aspirational.

Grounded in machinery that already exists: the PR/nightly/release/deploy CI gate model, the merge-coordinator, the metadata-release "additive lifecycle", `version-contract-drift` tests + pinned-server-contract, `registry-pin`, and numbered DB migrations. The job is to **connect and harden** these into one enforced system — and to fix the gates the audit proved are fake.

---

## 1. Versioning & version-label semantics

Two levels, deliberately separated:

- **Component version (SemVer per repo).** `MAJOR.MINOR.PATCH`: MAJOR = breaking wire/API/contract/config; MINOR = backward-compatible additions; PATCH = fixes. The bump is **decided by tooling**, not humans — breaking-change detectors (buf for proto, REST/OpenAPI diff for server, public-API diff for SDKs, terraform input/output diff for IaC) gate the version: a breaking diff without a MAJOR bump fails CI.
- **Contract version (independent).** The wire surfaces — GeoServices/OGC/STAC REST (OpenAPI), gRPC (proto), admin/control-plane API — carry their own `contractVersion`, decoupled from impl. "server 3.7 speaks contract v2." This is what compatibility is actually checked against (impl can churn without breaking clients).
- **Platform release (calendar label).** `Honua YYYY.N` (e.g. `2026.1`). **Not** a build — it's a *certified, pinned set* of component versions known to interoperate. Customers buy/operate "Honua 2026.1," not 9 independent version numbers. This label is the unit of support, docs, and upgrade.

Pre-releases: `-rc.N` candidates promoted to the calendar label only after passing the full gate stack (§4). Deprecations carry a documented window (≥2 platform releases) before removal.

---

## 2. The platform manifest + compatibility matrix (keystone)

A single machine-readable source of truth in the **release repo** (§12):

- **`platform-manifest.yaml`** — for each platform release: exact pinned version + git SHA + artifact digest of every component, the contract versions it implements, and the required DB schema version.
- **`compatibility-matrix.yaml`** — the interop rules: e.g. `server contract v2 ⇒ sdk-js >=4.1 <5, sdk-dotnet >=3.2 <4, sdk-python >=2.0 <3, iac-module >=2.0, helm-chart >=1.4, db-schema >=44`. Expressed as ranges + a supported window (server supports clients N-2 minor).

Enforced at four points (this is the product differentiator):
1. **Build/CI** — `version-contract-drift` + contract tests assert each component still satisfies the matrix; drift fails the build.
2. **Deploy** — IaC/Helm refuse to deploy a server image whose contract version is outside the chart/module's declared range (preflight).
3. **Runtime** — server advertises `contractVersion`; SDKs check on connect and warn/refuse on mismatch; server can reject or degrade incompatible clients with a clear error (not a silent 200).
4. **Upgrade** — preflight validates current→target against the matrix before any change is applied.

The matrix is **generated** from component metadata and **published** with each release so customers (and their CI) can validate their own pinned set. This is what "keeping track of version compatibility, iac↔server↔sdk" becomes: one enforced artifact, not tribal knowledge.

---

## 3. Automated release train (cross-repo coordination)

- **Cadence:** a platform train on a fixed cadence (e.g. monthly), plus continuous per-component PATCH releases between trains. Security fixes can cut an out-of-band train.
- **Cut = freeze + snapshot.** Cutting `2026.N-rc.1` snapshots each component's current green main into a candidate manifest, then runs the full gate stack (§4) against *that exact set*.
- **Dependency-ordered pipeline:** contracts (proto/OpenAPI) → server → contract-consumers (SDKs, console, mobile, collect) → deploy artifacts (IaC, Helm, Docker) → cross-repo certification. A break upstream stops the train before wasting downstream work.
- **Coordination engine:** built on the existing merge-coordinator — it already sequences cross-repo merges; extend it to drive train cut, gate orchestration, and promotion. Change classification (breaking/feature/fix/security/docs) comes from conventional-commit + PR labels and feeds both the SemVer decision and the release notes.

---

## 4. The gate stack (release-candidate certification)

A candidate is promoted to a platform label only after **all** gates pass, in order. Each gate directly closes an audit finding where a current gate is fake or missing:

| # | Gate | What it does | Closes |
|---|---|---|---|
| a | **Build + full test suite** | Every repo builds; the *entire* test suite runs and passes (no env-gated tests silently skipped as "green"); coverage floor on critical paths | esri-compat#25 (false-pass cert), test-integrity S2s |
| b | **Contract / breaking-change** | proto breaking-change (buf — incl. RPC/service deletion+rename), REST/OpenAPI diff, SDK public-API diff; breaking change ⇒ forced MAJOR or fail | grpc#44 (gate misses deletion), grpc#45 (codegen broken) |
| c | **Cross-repo conformance** | Esri/OGC/STAC conformance suites (real assertions), SDK↔server, console↔server, `version-contract-drift`, the compat matrix | mcp#25 (FULL false claim), esri-compat#25 |
| d | **Artifact-consumption** | Actually consume each *published* artifact from a staging registry: `npm/nuget/pip install`, `terraform init` the tarball, `helm template/install`, `docker pull && run && /healthz`, run codegen | sdk-js#310 (CDN bundle never built), iac#81 (terraform init broken), grpc#45 |
| e | **Security review** | SCA/deps, secret scan, SAST (CodeQL), container scan (Trivy), IaC scan (checkov/tfsec), license compliance | security S1/S2s, console#233 |
| f | **SBOM + provenance** | CycloneDX/SPDX SBOM per artifact; SLSA provenance; sign artifacts + manifest | (new capability) |
| g | **Observability / SLO** | Deploy candidate to staging; validate error budget **including the GeoServices in-band error metric**; no regression; auto-rollback on breach | server#2243, devops#113, iac#82, helm#31 (telemetry blindness) |
| h | **Documentation** | Docs build + link-check + **advertised-vs-actual**: every "Current Capabilities" claim maps to a passing conformance test or is labeled roadmap; generate + review release notes | over-advertised-docs theme (e.g. mobile fabricated AR example) |
| i | **Upgrade** | Run the upgrade from the *previous* platform release to the candidate on a seeded env: DB migrations (forward + rollback), config migration, zero-downtime check, old-client-vs-new-server compat | DB/config-across-versions risk |

Principle: **a gate that can't fail is worse than no gate** (it manufactures false confidence — see #25, the SLO gate that green-lights failing releases). Every gate here must be able to, and have, failed.

---

## 5. Test plan / full-test-suite gate

- **Tiers** (existing PR/nightly/release/deploy model, made honest): PR = fast unit + contract + governance; nightly = conformance + perf + security; release = full matrix + upgrade + artifact-consumption; deploy = post-apply validation.
- **No silent skips:** env-gated tests must run in the release tier with their backing services provisioned (the audit found suites passing while skipping the meaningful tests). The shard-coverage invariant is enforced.
- **Cross-repo integration suite** lives in the release repo and runs the real SDK↔server↔console↔deploy paths against the candidate image.
- **Upgrade test** (gate i) is a first-class suite: seed prior release → apply candidate → assert data intact, no downtime, old clients still work.

---

## 6. Security review + BOM / provenance

- Security gate (4e) blocks on criticals; results attached to the release.
- **BOM per artifact** (SBOM, CycloneDX) + a **platform BOM** aggregating all components — shipped with the release so customers can scan what they run.
- **Provenance + signing:** SLSA-style attestation, signed images/packages/manifest; customers (and the deploy gate) verify signatures before install. This is core to "safe to operate."

---

## 7. Documentation gate (advertised-vs-actual)

The audit's recurring credibility risk. Make it structural:
- Capability claims live in a machine-checkable manifest mapped to conformance tests; a claim with no passing test fails the docs gate or must be tagged `roadmap`.
- Docs versioned per platform release; upgrade guides generated from the change metadata; dead-link + example-build checks.

---

## 8. Metadata, change tracking & release notes

- Every PR carries a conventional-commit type + change-class label → drives SemVer bump, changelog entry, and release-note section automatically.
- The metadata-release "additive lifecycle" becomes the changelog/manifest store.
- **Traceability chain:** platform release → component version → git SHA → PRs → issues → SBOM → gate evidence. One click from "Honua 2026.1" to "exactly what changed, the upgrade impact, and the proof it was tested." Release notes generated per component and aggregated at the platform level, including an explicit **breaking-changes + upgrade-actions** section.

---

## 9. Publishing (all keyed off the manifest)

- **SDKs:** npm (`@honua-io/*`), NuGet, PyPI — versioned, provenance-signed; gate (4d) proves they install and import before publish.
- **Docker:** server/console/workers — multi-arch, signed, SBOM-attached; tags = component version + platform label.
- **IaC:** versioned terraform modules in a registry (extend `registry-pin`) + the customer tarball (fix #81); pinned to the manifest so `module "honua" { version = "2026.1" }` deploys a coherent, certified set.
- **Helm:** chart repo; chart version ↔ appVersion(server) ↔ platform label, range-checked against the matrix.
- A single **publish step driven by the manifest** so no component can be published at a version the matrix doesn't bless.

---

## 10. Database schema & config management across versions

- **Expand-contract migrations:** add (additive, online) → backfill → switch reads → contract later. Guarantees every release is rollback-safe; never destructive in one step. Numbered, with forward **and** rollback tested in gate (4i).
- **DB schema version** is a first-class entry in the compatibility matrix (server X requires schema ≥ N; refuse to start otherwise).
- **Config:** schema-versioned with safe defaults; deprecations carried ≥2 releases with warnings; a config-migration tool; no breaking config change without a migration path. Config schema is validated at deploy and on upgrade preflight.

---

## 11. Safe deploy & safe version upgrade (the operability promise)

- **Promotion:** dev → staging → prod, each gated; deploy-gates + post-apply validation (PR template already has the slots — wire them).
- **Strategies:** canary / blue-green / rolling per component; **automated rollback on SLO breach** (now possible because of the GeoServices error metric); health/readiness everywhere.
- **Preflight upgrade check** (customer-runnable): validates current→target against the compat matrix, dry-runs DB migrations, diffs config, confirms backups — *before* touching anything. This is the headline "safe upgrade" UX.
- **One-manifest operate:** deploy or upgrade a whole certified platform version with one pinned manifest; clear rollback; observability that actually sees errors. That is "doesn't suck to operate," made concrete.

---

## 12. The release repo (org structure)

A central **`honua-release`** repo (or a home in honua-devops):
- the platform manifest + compatibility matrix (source of truth),
- the release-train pipeline + merge-coordinator orchestration,
- the cross-repo certification + upgrade-test harnesses,
- aggregated BOM, release notes, provenance, and the published compat matrix.

Each component repo owns: its SemVer + breaking-change gate, its build/test/contract gates, its signed artifact + per-component changelog + SBOM, and it *reports its contract version* up to the manifest.

---

## 13. Phased rollout (don't boil the ocean)

- **Phase 0 — Foundations:** define version/contract semantics; create the release repo, `platform-manifest.yaml`, `compatibility-matrix.yaml`; encode today's versions; classify the audit's release-engineering findings into this backlog.
- **Phase 1 — Fix the fake gates (prereq):** codegen (#45), proto breaking-change (#44), conformance false-pass (#25 ×2), artifact-consumption gate (#310/#81), and the GeoServices error metric + SLO/alert wiring (#2243/#113/#82/#31). Until these are real, nothing downstream can be trusted.
- **Phase 2 — Certified train:** cross-repo conformance + artifact-consumption + upgrade gate wired into an RC pipeline producing a pinned manifest.
- **Phase 3 — Supply-chain + docs:** SBOM/provenance/signing, security gate, advertised-vs-actual docs gate, automated release notes.
- **Phase 4 — Operator UX:** preflight upgrade, one-manifest deploy, canary + auto-rollback. Ship the "safe to operate" story.

---

## 14. How this retires the audit's release-engineering findings

The "weak gates," "artifact integrity," and "telemetry blindness" themes are not bugs to patch once — they're the **absence of this system**. Phase 1 fixes the specific findings; Phases 0/2–4 ensure they can't recur, because the manifest + matrix + gate stack make compatibility, artifact-validity, and observability *release-blocking by construction*. That enforced platform coherence is the thing Honua is actually selling.

---

## 15. AI-orchestrated release (GHA + MCP)

**Principle: AI proposes, the pipeline disposes.** The AI decides *when* to cut and *interprets* gate results; GitHub Actions deterministically enforces *what must be true*. The AI can trigger and read, but cannot perform release steps by hand or override a red gate. Safety lives in GHA, not in the model.

### GHA structure (built for triggering + machine-readable results)
- **Reusable workflows everywhere.** Each component repo exposes its gates as `workflow_call` units (`gate-build-test.yml`, `gate-contract.yml`, `gate-artifact-consume.yml`, `gate-security.yml`). They run in PR CI *and* are callable by the train — same code, no drift.
- **Release repo owns `release-train.yml`** triggered by `workflow_dispatch` (inputs: `platform_label`, `pins` or `auto-snapshot`, `dry_run`). It: snapshot main → build candidate manifest → fan out to component gates via `workflow_call`/`repository_dispatch` → cross-repo conformance + upgrade gate → SBOM/notes → tag RC → (gated) promote.
- **`workflow_dispatch` is the trigger surface for *both* humans and AI** — there is exactly one way to cut, and it's the same one. Cross-repo fan-out via `repository_dispatch` with a GitHub App token.
- **Promote = a separate job behind a GHA `environment`** (`production`) with a required human reviewer and a protected-branch deployment policy. Promotion preflights the environment through the GitHub API and fails before release work if either protection is absent. This is the human/AI approval boundary; the AI cannot skip it.
- **`concurrency` group** on the train so two cuts can't race.
- **Every gate emits a machine-readable report** (a `gate-report.json` artifact + job summary): `{gate, status, why, evidence_url}`. The AI parses *that*, never scrapes logs. The train packages its report with the exact candidate manifest and compatibility matrix; the report binds both by SHA-256 plus immutable source/run identity and an explicit live/dry-run mode. Promote requires a successful live train from the repository's protected current default branch, verifies that bundle against the Actions and repository APIs, and requires all gates green before it finalizes, signs, or releases anything. It never replaces certified files with a later checkout.

### Repository configuration prerequisites

Promotion remains intentionally unavailable until configuration outside git matches the workflow's
trust model:

- Protect the current default branch, enforce the rule for administrators, require pull requests,
  and require the GitHub Actions `validate` check. `manifest-validate.yml` runs that stable check on
  every pull request so path filters cannot deadlock or bypass it.
- Configure the `production` environment to allow only protected branches, require the expected human
  reviewer, and enable `prevent_self_review`. The declared configuration is settings-as-code in
  `certification/production-approval.yaml`; `promote.yml` compares the live environment against it and
  refuses on anything missing, unreadable, weakened, drifted, or stale, and
  `.github/workflows/repo-control-drift.yml` monitors the same comparison read-only between releases.
  **This repository is now public**, so required environment reviewers are available on every plan —
  the earlier Enterprise dependency applied to this repository while it was private and would return
  if it were re-privatised. Do not weaken the preflight to work around a plan limitation.
- `prevent_self_review` plus the preflight's independent-actor check means the required reviewer must
  not be the actor that dispatches `promote`: automation (or a second human) dispatches, the reviewer
  approves. The gate requires exactly one declared reviewer, so a single-seat owner who needs to
  dispatch personally must **rotate** the required reviewer to another human — never disable
  self-review protection, and never add a second reviewer alongside (the checker refuses more than
  one; multi-reviewer continuity is tracked in honua-release#93).
- Full rationale, admin runbook, reviewer rotation, break-glass, and re-lock procedure:
  `docs/PRODUCTION-APPROVAL-GATE.md` (honua-release#44).

### Identity, least privilege, provenance
- The AI acts as a dedicated **GitHub App** with scoped permissions: `actions:write` (dispatch) + `contents:read` + read checks/artifacts — **but no publish/sign/tag rights.** Tagging, publishing, and signing are done by the *workflow's own OIDC identity*, so the AI never holds signing keys and can't exfiltrate them. Keyless signing (OIDC → Sigstore/SLSA) ties provenance to the workflow, and every dispatch records the triggering actor (which AI/human).

### Near-term: AI coordinates
The AI watches signals (green mains, change backlog, cadence, open S1 count, freeze flag) → decides "cut `2026.2-rc.1`" → `gh workflow run release-train.yml -f platform_label=…` → polls `gate-report.json` → on red, reads the failing gate, triages/files, holds; on green, requests promote (which hits the human-gated `production` environment). The AI **drafts release notes** from change metadata (it's good at that) but the *facts* — versions, SBOM, gate evidence — come from the pipeline.

### Eventual: AI cuts its own release via MCP
Expose release ops as **thin, policy-enforcing MCP tools** over the GHA surface (natural home: geospatial-mcp + the existing MCP tooling):
`release.status()` · `release.cut_rc(label, dry_run)` · `release.gate_report(run_id)` · `release.promote(label)` · `release.rollback(env, to_label)` · `release.freeze(on|off)`.
The **MCP server holds the policy and the GitHub App creds**; it checks who/what may cut, the freeze flag, and required-approval state *server-side* before dispatching. The AI calls tools; it never touches tokens or gates directly.

### Autonomy expands inward-out as the gates earn trust
- **Tier 1 (now):** AI may `cut_rc` + deploy to **staging** autonomously; **prod promote = human approval** (GHA environment reviewers).
- **Tier 2:** AI may promote to prod *only if* the SLO/canary gate (incl. the GeoServices in-band error metric) stays green for N minutes; human can veto in the wait window.
- **Tier 3:** fully autonomous cadence, with humans on the **kill switch** (`release.freeze`) and auto-rollback on SLO breach.

### Hard prerequisite (the cautionary tale from this very audit)
Autonomous AI release is only safe once **the gates can't lie.** The audit found an SLO release gate that green-lights a failing release (devops#113) and a platform blind to its own error rate (server#2243/iac#82/helm#31) — an AI handed those gates would confidently ship broken releases on a cadence. **Phase 1 (real gates + real telemetry) is a non-negotiable prerequisite for AI autonomy.** Don't give the AI the keys until a red is guaranteed to be a real red.

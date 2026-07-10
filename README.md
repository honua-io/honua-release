# honua-release

The platform release-engineering home for Honua. Honua is sold as a *platform* — many independently-built
components (server, SDKs, console, mobile, collect, gRPC, MCP, IaC, Helm) that customers deploy and upgrade
themselves. This repo is where "the platform fits together" is **enforced**, not assumed.

It owns:
- **`platform-manifest.yaml`** — the pinned, certified set of component versions for each platform release (`Honua YYYY.N`).
- **`compatibility-matrix.yaml`** — the interop rules (which component versions work together: iac↔server↔sdk↔db). Source of truth.
- **`.github/workflows/`** — the automated **release train** (`release-train.yml`) and the environment-gated **promote** (`promote.yml`).
- **`certification/`** — cross-repo conformance + artifact-consumption gates.
- **`e2e/`** — the cross-component & cross-cloud integration/parity harness (the executable compatibility matrix).
- **`release-notes/`**, **`bom/`** — aggregated release notes, SBOM, provenance.
- **`mcp/`** — MCP release tools (eventual AI-coordinated / autonomous release cutting).

Shared reusable CI workflows live in the org `.github` repo; each component repo owns its SemVer + gates and
reports its contract version up to the manifest here.

## How a release works (summary)
`workflow_dispatch` → snapshot main → build candidate manifest → fan out to component gates → cross-repo
conformance + artifact-consumption + upgrade gate → emit a digest- and run-bound certified-candidate bundle
→ SBOM + notes → tag RC → (environment-gated) promote to `Honua YYYY.N`. Promotion consumes only the exact
manifest and matrix in that bundle and verifies them against the certifying Actions run before release work
begins. **AI proposes, the pipeline disposes** — gates live in GHA and can't be overridden.

See `docs/RELEASE-ENGINEERING-PLAN.md` and `docs/TEST-STRATEGY.md` for the full design. Work is tracked in the
**Phase 0/1 epic** (issues in this repo).

> **Hard rule:** do not give AI release autonomy until the gates can't lie. Phase 1 (real gates + real
> telemetry) is the prerequisite — see the plan.

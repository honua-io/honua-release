# gRPC `v0.2.0-alpha.1` cut — cross-repo runbook (honua-release#41)

`geospatial-grpc` merged a **sanctioned pre-v1 wire break** (issue
[geospatial-grpc#48](https://github.com/honua-io/geospatial-grpc/issues/48) Option A, PR
[#69](https://github.com/honua-io/geospatial-grpc/pull/69), merged 2026-07-03): the eight
job-lifecycle control-plane messages that were copy-pasted across five services were promoted to
shared types in `execution_types.proto`, and `SpecService` was converged onto them. The proto
*package* is unchanged (`geospatial.v1`); the *types* moved. `conformance/VERSION` and
`src/Geospatial.Grpc/Geospatial.Grpc.csproj` on trunk both declare `0.2.0-alpha.1`.

Nothing downstream can consume it, because **no release was ever cut**. This runbook is the ordered
procedure to close that, and the reason the order matters.

## Current state (verified 2026-08-16)

| Fact | Evidence |
|---|---|
| trunk carries the break at `73fc882` | `git show origin/trunk:conformance/VERSION` → `0.2.0-alpha.1`; no `*.proto` or `conformance/` delta between the break commit `21be085` and trunk |
| **trunk CI is red, and has been since the break merged** | runs `31032320777` (`73fc882`) and `30311400779` (`cc506bf`) both `conclusion=failure`; the only failed step in either is `Check Breaking Changes (push vs previous release tag)` |
| every downstream job is therefore skipped | in run `31032320777`, `Publish to Buf Registry` (which also cuts the fixtures release), `Conformance Fixtures`, `Pack .NET Protocol Package` and `Generate Code` are all `conclusion=skipped` (`needs: lint-and-validate`) |
| no `0.2.0-alpha.1` release or package exists | `gh api repos/honua-io/geospatial-grpc/releases` → highest is `v0.1.0-alpha.3`; `gh api /orgs/honua-io/packages/nuget/Geospatial.Grpc/versions` → only `0.1.0-alpha.1`, `0.1.0-alpha.2` |
| consumers are two releases behind | `honua-server` and `honua-sdk-dotnet` both pin `Geospatial.Grpc 0.1.0-alpha.2` in `Directory.Packages.props` |

## The two tag namespaces (this is what went wrong last time)

`geospatial-grpc` has **two independent, both load-bearing** release tag namespaces:

| Tag | Consumed by | Produces |
|---|---|---|
| `v<VERSION>` | `ci.yml` buf breaking baseline (`git tag -l 'v*' --sort=-v:refname \| head -n1`), and the same selector in this repo's `gate-contract.yml` `proto-breaking` check; `conformance/fetch-fixtures.sh` | the buf `--against` baseline, and the GitHub Release (cut by `ci.yml`'s `publish` job from `conformance/VERSION`) carrying `conformance-fixtures-<VERSION>.tar.gz` |
| `geospatial-grpc-v<VERSION>` | `publish-dotnet-protocol.yml` (`on.push.tags: geospatial-grpc-v*`) | the `Geospatial.Grpc` NuGet package (GitHub Packages, plus nuget.org when `NUGET_API_KEY` is configured) |

`0.1.0-alpha.3` was cut with **only** the `v*` tag. That is precisely why it has a fixtures release
but no package, why `honua-sdk-dotnet`'s conformance job carries an explicit "fixtures are published
but the matching `Geospatial.Grpc` package is not — this is distribution drift and the pins cannot be
promoted yet" warning, and why no consumer ever moved onto it. **Cutting only one tag again
reproduces the same dead end.**

There is also an ordering trap. The fixtures release is created by the `publish` job in `ci.yml`
("Publish to Buf Registry"), which `needs: lint-and-validate` — the job that is currently failing on
the breaking check. So CI cannot produce the release until the baseline advances, and the baseline
only advances when a higher `v*` tag exists. **Creating the `v0.2.0-alpha.1` tag by hand is what
breaks that cycle**; it is not optional and cannot be delegated to CI.

That `publish` job is also gated on
`if: github.ref == 'refs/heads/trunk' && github.event_name == 'push'` and runs in the `production`
environment. A `workflow_dispatch` re-run therefore **skips it and produces no fixtures release** — the
red run must be re-run *as the push it was*, and the `production` environment's reviewer may have to
approve it.

## Ordered procedure (release lead — outward-facing, irreversible)

Steps 1–4 create public tags and publish public packages. They are deliberately **not** automated
here.

```bash
# 0. Preconditions — expect 73fc882…, 0.2.0-alpha.1, 0.2.0-alpha.1
cd geospatial-grpc && git fetch origin --tags
git rev-parse origin/trunk
git show origin/trunk:conformance/VERSION
./conformance/check-version.sh   # asserts VERSION == csproj <Version>

# 1. Advance the buf breaking baseline. Highest `v*` tag IS the baseline, in both
#    geospatial-grpc/ci.yml and honua-release/gate-contract.yml — no workflow edit is needed.
git tag -a v0.2.0-alpha.1 73fc882b1ae00d0a4a348aeadfba9f48b1a0317c \
  -m "geospatial.v1 schema 0.2.0-alpha.1 — sanctioned pre-v1 wire break (#48/#69)"
git push origin v0.2.0-alpha.1

# 2. Re-run the RED TRUNK PUSH RUN — not `gh workflow run`. The job that creates the fixtures
#    release is gated on `github.event_name == 'push'`, so a workflow_dispatch run skips it.
#    Re-running the existing push run preserves the event and the sha.
gh run rerun 31032320777 --failed -R honua-io/geospatial-grpc
#    (If that run has aged out of re-run eligibility, land any no-op commit on trunk instead.)
#    The `publish` job uses the `production` environment — approve the deployment if prompted.
# verify — expect conclusion=success, and the release to carry conformance-fixtures-0.2.0-alpha.1.tar.gz
gh run view 31032320777 -R honua-io/geospatial-grpc \
  --json conclusion,status --jq '"status=\(.status) conclusion=\(.conclusion)"'
gh api repos/honua-io/geospatial-grpc/releases/tags/v0.2.0-alpha.1 --jq '.assets[].name'

# 3. Publish the NuGet package. THIS tag, and only this tag, triggers publish-dotnet-protocol.yml.
git tag -a geospatial-grpc-v0.2.0-alpha.1 73fc882b1ae00d0a4a348aeadfba9f48b1a0317c \
  -m "Geospatial.Grpc 0.2.0-alpha.1"
git push origin geospatial-grpc-v0.2.0-alpha.1

# 4. Verify the package actually landed before telling any consumer to move.
gh run list --workflow publish-dotnet-protocol.yml --limit 1 \
  --json conclusion,status -R honua-io/geospatial-grpc
gh api '/orgs/honua-io/packages/nuget/Geospatial.Grpc/versions?per_page=100' --jq '.[].name'
```

Step 3 publishes to nuget.org **only** if `NUGET_API_KEY` is configured; otherwise the lane warns and
skips, and the package is GitHub-Packages-only. Whether Honua publishes to public registries at all
is an open owner decision tracked in honua-release#57 — this runbook does not decide it, and a
GitHub-Packages-only publish is sufficient to unblock every internal consumer.

### Precondition check for the manifest re-pin

`platform-manifest.yaml` pins `geospatial-grpc` to `0.2.0-alpha.1` / `73fc882…`. The
`proto-breaking` check in `gate-contract.yml` clones that pinned sha and runs
`buf breaking --against .git#tag=<highest v* tag>`; if the pinned sha carries the break while
`v0.1.0-alpha.3` is still the highest tag, that gate **fails**. So the manifest state is only safe
once step 1 has landed:

```bash
# must print v0.2.0-alpha.1
git ls-remote --tags https://github.com/honua-io/geospatial-grpc.git 'refs/tags/v*' \
  | sed 's#.*refs/tags/##' | grep -v '\^{}' | sort -V | tail -n1
```

## Downstream consumers (after step 4 confirms the package)

Each of these is the owning repo's own PR; none of them can build before the package is published.

**`honua-server`** — canonical `.proto` stays in `geospatial-grpc`; the server consumes generated
bindings through the package and must not vendor protos (`CLAUDE.md` → *Proto Ownership*). Bump
`Directory.Packages.props` `Geospatial.Grpc` `0.1.0-alpha.2` → `0.2.0-alpha.1`, then apply the type
renames from the [#69 migration checklist](https://github.com/honua-io/geospatial-grpc/pull/69):
per-service `Validate*Response`/`DryRun*Response` → shared `ValidateResponse`/`DryRunResponse`;
`Get*JobRequest`/`Get*JobResponse`/`Get*JobResultRequest`/`Cancel*JobRequest`/`Cancel*JobResponse`/
`Submit*JobResponse` → the shared `…Job…` types; `SpecService.CancelApply` now takes
`CancelJobRequest` keyed on `job_id` (`apply_token` is reserved) and returns `CancelJobResponse`;
`ApplySpecEvent` emits `job_id` (field 10), typed `ParameterValue` inputs, and
`DryRunResult`/`ErrorDetail` for cost and warnings. Note this is a **two-release** jump
(`0.1.0-alpha.2` → `0.2.0-alpha.1`), not one.

**`honua-sdk-dotnet`** — regenerate against `0.2.0-alpha.1` and move the fixture pin in the same
change: `Directory.Packages.props`, `conformance/FIXTURE_VERSION`, `conformance/PINS.md`,
`conformance/README.md`, `docs/protocol-integration-tests.md`, and
`tests/Honua.Sdk.Conformance.Tests/ConformanceFixtures.cs` (`PinnedVersion`) all carry
`0.1.0-alpha.2` today. Tracked in
[honua-sdk-dotnet#264](https://github.com/honua-io/honua-sdk-dotnet/issues/264).

**`geospatial-mcp`** — spec/conformance repo with **no** `Geospatial.Grpc` package dependency. Its
only coupling is prose citing `geospatial.v1` message names (`spec/resources.md`, `spec/taxonomy.md`,
`spec/corpus.md`, `spec/planning.md`). The break renamed job-lifecycle control-plane messages, none
of which those documents name, and `StyleRef`/`AttributeValue`/the geometry vocabulary are untouched
— so there is nothing to regenerate. Re-confirm after the cut rather than assuming.

## Then, in this repo

Merge the `platform-manifest.yaml` + `compatibility-matrix.yaml` re-pin (prepared in honua-release#41)
and confirm the gate is exercising the new baseline rather than passing vacuously:

```bash
python tools/validate_platform.py
gh workflow run gate-contract.yml -f platform_label=grpc-0.2.0-alpha.1 -f enforcement=strict
```

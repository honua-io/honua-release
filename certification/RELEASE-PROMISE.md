# Release promise receipts

`release-promise.v1.json` is the producer contract for the founder's 14 November
2026 amendments and the 15 November approved release gates plan. Its 33 promise
rows include four additional RC and GA requirements: `cloud-image-aot`,
`cloud-no-redis-source`, `security-agent-default-deny`, and `operates-8-upgrade`.
RC requires these alongside the ten fault rows, hardened default, zero open high
findings, cloud install, white-hat and chaos evidence.
AI journeys and injection evidence are reported for RC and required for GA.
Unproven AI journeys must ship marked Preview; a green RC gate does not certify them.

The workflow names in the contract reserve producer interfaces. They do not claim
that those workflows exist or that any live test has passed. Missing producers,
missing receipts, incomplete API history and unavailable credentials fail closed.
The aggregate only reads Actions evidence; it does not deploy or run paid evaluations.

Each producer runs from its repository's `trunk` through `workflow_dispatch`,
`schedule`, `repository_dispatch` or `workflow_run` and uploads a `release-promise-receipts`
artifact. Each row has its own JSON file at the artifact root, named by `receipt`
in the contract. The Actions API authenticates the repository and workflow;
receipt-supplied provenance is never trusted. The latest completed execution
and every overlapping execution must pass. A newer failure without a receipt
invalidates an older pass, even when the newer run targeted a different candidate.
Re-run the producer for the intended candidate in that case.

Example receipt (the row's own assertions must match the contract):

```json
{
  "schema": "honua.release-promise-receipt/v1",
  "row": "cloud-launch-stack",
  "status": "PASS",
  "serverSha": "<40 hexadecimal characters>",
  "imageDigest": "sha256:<64 hexadecimal characters>",
  "platformLockDigest": "sha256:<64 hexadecimal characters>",
  "observedAt": "2026-11-15T06:00:00Z",
  "stackName": "honua-gate-example",
  "assertions": {
    "live": true,
    "orchestrator": "ecs",
    "redis": false,
    "defaultImage": "postgis-only"
  }
}
```

`observedAt` must fall inside the authenticated run's execution and the last seven
days. All three candidate identifiers must match together. Hardened-profile and
cloud rows require a `honua-gate-` stack name. The `security-open-high` row is
supplied as a manifest input (`source: "manifest-input"`) built on the lane host
from the private tracker with the founder `gh` login and written to the private
artifacts directory — no private tracker token lives in `honua-release` CI. It
carries only `openHighFindings` (integer zero), `assertions: {"privateScoped": true}`,
and the common identity fields; any unexpected field is rejected. Never pass tracker
titles, bodies, or issue IDs. The aggregate reads the sanitized manifest input from
`--security-summary` / `SECURITY_SUMMARY_FILE` or `security_summary_json` and checks
candidate identity, seven-day freshness, and the strict field allowlist.

Run the gate on trunk with `server_sha`, `server_digest`,
`platform_lock_digest` and `profile` (`rc` or `ga`) workflow inputs. Cross-repository
producer artifacts require `RELEASE_GH_TOKEN` with Actions read access; the default
token suffices for producers in honua-release. The summary artifact is
`release-promise-report`; its JSON lists every row, requirement and status, and
binds the candidate. RC admission includes this gate without a manifest-pin
fallback. GA admission additionally requires all five AI rows plus protocol compatibility and capacity. Admission rechecks
row freshness at its own evaluation time.

The outcomes rulings require all 35 measurements in `outcomes` at RC: the existing
19 timing measurements and 16 sentence/click counts. `gate-dr.yml` supplies
`recovery-operates-restore`; `gate-release-outcomes.yml` supplies the other 34.
Both producers use the same measurement interface. It uploads one common-schema receipt per metric with numeric,
finite, nonnegative `value`, the contract unit (`minutes`, `sentences`, or `clicks`),
and assertions `live: true` and
`releaseNotesReported: true`. The same artifact must contain `release-notes.md`
with an exact line for each value, for example:

```markdown
<!-- honua-outcome:install-console --> 12.5 minutes
```

The collector reads the archived notes itself; receipt-supplied note text is
ignored. The producer must archive the release notes used for publication.
The aggregate reports each measured value and whether its GA target passed;
values alone do not certify any live promise row. RC requires the measurements
and notes, without enforcing targets. GA additionally requires `gaMaximum` to be set and
met for minutes and sentences. Clicks remain measured only at GA; their
`targetMet` is null and no target is enforced. The unattended target is zero person-minutes;
other targets remain unset until approved, so they cannot silently pass GA.
No measurement or target is fabricated by the gate.

The 14 November 23:45 PT ruling additionally requires `security-whitehat` and
`chaos` for RC (and retains them for GA). Their reserved producers are
`gate-whitehat.yml` and `gate-chaos.yml`. They currently have no live receipt.
White-hat receipts require `stackName: honua-gate-whitehat` (or that prefix with a
suffix), a nonempty `stackId`, and integer-zero `openNewHighFindings` and
`openNewCriticalFindings`. Their two `reviews` identify distinct reviewers and
exact models `claude-fable-5-2` and `gpt-6-astra`, each with `source: true`,
`live: true`, `status: PASS`, a distinct root-level Markdown `report` filename,
and its hexadecimal `reportSha256`. Both sanitized reports must be archived in
the same authenticated artifact. The common receipt binds both reports and the
stack to the candidate SHA, image digest and platform lock. Detailed security
findings stay in the private tracker; public artifacts contain sanitized evidence.

Chaos receipts contain exactly nine `faults`, with ids `stop-tasks`, `wedge-task`,
`stop-database`, `fill-database`, `crashing-release`, `error-release`, `block-probe`,
`stop-watchdog`, and `flood-requests`. Every fault has `telemetry`, `logging`, and
`remediation` objects, each with `status: PASS` and a nonempty sanitized `evidence`
reference. A top-level PASS without these per-fault grades cannot certify chaos.

The aggregate is admitted only from a completed, successful trusted trunk workflow
execution in honua-release. RC gate Markdown carries the verified numeric outcomes
into the published notes. GA promotion collects fresh evidence under the current
trunk policy before checking out the certified candidate source, using that train's
retained manifest and exact lock bytes. A refusal stops publication; successful
promotion appends the verified outcomes to GA notes and retains its aggregate report.

The eight workflow suffixes for both `sentences-<workflow>` and
`clicks-<workflow>` are `install`, `publish-layer`, `style`, `share`, `build-app`,
`sign-in-roles`, `recover-incident`, and `migrate-service`. Sentences count user
sentences to the agent. Clicks count clicks plus typed fields in Console and
Studio using Playwright. Both use the same workflow and candidate, and both
require an exact archived notes line with their own unit, for example:

```markdown
<!-- honua-outcome:sentences-install --> 3 sentences
<!-- honua-outcome:clicks-install --> 18 clicks
```

New producer interfaces and required assertions (in addition to common candidate
identity and authenticated execution) are:

| Row | Producer | Assertions |
|---|---|---|
| `cloud-image-aot` | `gate-launch-stack.yml` | `live`, `publishedAotImage`, `sizeBudgetMet`: true; `stackName` uses `honua-gate-` prefix. Published image digest must equal the receipt candidate digest. `imageSizeBytes` is the positive integer uncompressed image size: RC <=370,000,000 bytes; GA <=220,000,000 bytes. The aggregate retains this measurement and rechecks the requested profile; a boolean size assertion alone cannot pass. |
| `cloud-no-redis-source` | `gate-source-facts.yml` | `redisPackages`, `redisSourceReferences`: integer zero at the candidate SHA; this is source evidence, without a live-stack assertion. Both counts use the candidate's own `scripts/ci/check-retired-cache.py` predicate and exemptions. `redisPackages` counts Redis-named package declarations in MSBuild, `package.json`, `requirements*.txt` and `pyproject.toml` files; `redisSourceReferences` counts matching lines in every other tracked regular file, manifests included, except `docs/`, `tests/` and migrations whose content matches their pin under `eng/schema/migration-hashes/`. A missing checker, or a checkout that is not exactly the candidate commit, leaves no receipt. |
| `security-agent-default-deny` | `gate-agent-safety.yml` | `live`, `allMutatingRoutesChecked`, `proposalOnly`, `agentIdentityAudited`: true. Every mutating route must return a proposal without making a direct change. |
| `operates-8-upgrade` | `gate-upgrade.yml` | `live`, `rollingUpgrade`, `nMinusOneReaderCompatible`, `rollbackPassed`, `launchStack`: true, using the previous release image on the Launch Stack. |

These interfaces reserve evidence requirements; they do not implement producers
or certify a live pass. Shell runs are debugging evidence only.

`gate-agent-safety.yml` (`tools/agent_safety_gate.py`) implements `security-agent-default-deny`
without an installed stack. It boots the exact candidate image@digest with the e2e compose harness
and mints an agent-kind API key. As that agent it submits every operation that the candidate's
catalog marks other than read-only, and each one must come back as a proposal awaiting approval.
It then calls every mutating route in the candidate's admin OpenAPI document and on the candidate's
committed agent route rule list. A route without its own agent rule must answer 403. A route with
its own rule must not succeed unless the rule says it changes nothing. Each proposal must name the
agent as `requestedByAgent`, each refusal must be in the audit trail under the agent actor, and the
administrator state read before the walk must be unchanged. Parameters an operation needs before
its policy decision come from `certification/agent-safety/operation-fixtures.json`, filled from
the state `tools/agent_safety_seed.py` creates with the administrator key. An operation that never
reaches the decision leaves the row failing. A route with its own rule counts only when its answer
shows the rule decided (403, an `operator.agent.*` reference or a proposal). A documented route the
candidate does not map counts only when the fixtures give the reason. Sign-in routes under
`/admin/auth/` are listed, not judged. Fixture `probes` send further requests to a route whose rule
refuses only some changes, such as an edit of content another person owns. The route scope is every
mutating route in the candidate's admin OpenAPI document plus every admin and Console route on its
agent route rule list; Console routes without their own rule are not enumerated, because the
candidate publishes no Console route list. Protocol data-edit routes (OGC
Features transactions, GeoServices `applyEdits`, WFS-T) and GP job submission follow their layer
and process grants and are outside this row; the receipt's `scope` field says so.

The existing gates remain the producers for these kept rows:

| Row | Required | Producer workflows (all must pass) |
|---|---|---|
| `supply-sbom` | RC, GA | `gate-sbom.yml` |
| `supply-scan` | RC, GA | `gate-security.yml` |
| `contract-compat` | RC, GA | `gate-contract.yml`, `gate-contract-live.yml` |
| `docs-executable` | RC, GA | `gate-executable-docs.yml`, `gate-docs.yml` |
| `clients-installed` | RC, GA | `gate-installed-clients.yml` |
| `operates-telemetry` | RC, GA | `gate-observability.yml` |
| `operates-restore` | RC, GA | `gate-dr.yml` |
| `protocol-compat` | GA; reported at RC | `gate-protocol-certification.yml` |
| `operates-capacity` | GA; reported at RC | `capacity-soak.yml` |

Each kept producer emits `producerPassed: true` only after its validated raw
verdict is `pass`. Bootstrap acceptance of blocked or skipped checks cannot emit
PASS. Pass `platform_lock_digest` on dispatch or reusable calls; it identifies the
signed candidate manifest. The adapter obtains source SHA and image digest from
the exact manifest consumed by the gate (protocol certification uses the exact
expected identifiers its evaluator checked). A run without that digest remains
useful for diagnostics but emits no promise receipt and cannot certify stage 2.
The digest is sha256 of the qualification lock that the receipt step regenerates
from the manifest (`kept_gate_receipt.ensure_lock`), not the canonical lock-draft
digest in `docs/SDK-SERVER-COMPATIBILITY.md`. `dispatch-promise-producers.yml`
(`tools/promise_dispatch.py`) computes it and dispatches every stackless producer
for the `platform-manifest.yaml` server pin. It runs daily, on pin changes and on
manual dispatch. The tool lists the producers it leaves out and why: they need an
installed stack, a reviewer, the private tracker, release-train or spend.
For companion producers the aggregate evaluates each workflow independently;
one successful contract or docs producer cannot hide its missing or failed peer.

The DR adapter additionally archives `recovery-operates-restore` in minutes,
converted from the validated full-platform receipt's `measurements.rtoMs`, with
the exact release-notes line. The upgrade adapter refuses kind/Compose-only
success: its report must include candidate-bound live Launch Stack, N-1 reader,
rolling-upgrade and rollback assertions. Until the Launch Stack harness supplies
that proof the row remains unproved. Local adapter tests never establish a live pass.

Protocol receipts compare every evaluated component SHA, SDK package version, cut time, and requirements revision with the lock-bound manifest. Capacity receipts retain `sourceObservedAt`; admission checks that original observation against the seven-day window as well as checking the authenticated adapter run.

# Hybrid evidence production

Release evidence can come from a workflow-owned disposable target or from an
independent producer. Both need identity, lineage and freshness checks before a
release consumer can use them. The place a producer runs does not determine what
its result proves.

See [candidate preparation](CANDIDATE-PREPARATION.md),
[burn-in promotion](BURN-IN-PROMOTION.md) and the
[test strategy](TEST-STRATEGY.md) for candidate and qualification requirements.
This guide describes producer boundaries and the standing-demo canary contract;
it does not grant publication authority or allow release gates to be skipped.

## Producer boundaries

| Producer | Evidence boundary |
|---|---|
| Disposable target checks | Exercise the deployed candidate through real HTTP or the required client; retain target, artifact identity, inputs, results and cleanup evidence. |
| Independent compatibility or recovery producer | Deliver a versioned envelope from the required client or environment, with enough lineage and freshness information for the consuming gate to validate it. |
| Standing-demo canary | Observe an existing deployment on its own schedule. A monitoring result alone does not certify the selected release candidate. |

Expensive drills, licensed client runs and persistent deployments can produce
out of band. Their consumers must validate the required evidence identifier,
source and target identity, candidate lineage, completeness and freshness bound.
A downloaded file or successful producer job alone does not establish those facts.
Advertising a capability as GA requires qualifying evidence for that capability;
stale, missing or mismatched evidence cannot establish it.

Missing credentials, unseeded data, absent harnesses and unavailable prior-release
baselines must remain explicit gaps. A `blocked` probe is not a live pass or an
implicit release exception. Any permitted exception must come from the applicable
current gate policy, not an old bootstrap skip list. Plain HTTP cannot prove HSTS;
transport-dependent checks must describe what they actually exercised.

## Standing-demo canary

[demo-canary.yml](../.github/workflows/demo-canary.yml) runs
[e2e/demo_canary.py](../e2e/demo_canary.py), combining canonical checks and canary
probes. The workflow defines the schedule and the conditional extra runs during
a governed selected-lock burn. Manual dispatch can select a target URL.

`HONUA_DEMO_API_KEY` supplies authentication for key-gated probes. Without it,
those probes report `blocked`. Unseeded data can also block individual probes.
The process may exit successfully with blocked checks; read the report rather
than interpreting a green job as full qualification. Report-only latency results
must remain distinguishable from gating checks.

When a selected promotion record is configured, the workflow authenticates its
retained mint and observes the ECS deployment before and after probes. Bound
probes refuse redirects. Those binding checks must succeed before observations
can be attributed to the selected deployment. Monitoring another deployment does
not establish candidate provenance.

## Probe quarantine

[e2e/canary-quarantine.yaml](../e2e/canary-quarantine.yaml) records a probe's
owning issue, reason, `since` date and hard `reviewBy` date.
[e2e/quarantine.py](../e2e/quarantine.py) applies these rules, covered by
[e2e/test_quarantine.py](../e2e/test_quarantine.py):

- Only a matching `fail` becomes `quarantined`. A `pass` or `blocked` result is
  unchanged. An optional `failureContains` narrows the owned failure; a different
  failure remains red.
- After `reviewBy`, the failure stays red. Extending an entry requires a reviewed
  registry change.
- Entries whose probes no longer fail (`stale`) or no longer exist (`unknown`)
  appear in workflow annotations. Applied entries appear in the summary with
  their owning issue.
- A quarantined failure can avoid failing the monitoring job and opening its
  regression alert, but remains a gap in the report and published evidence.
  Quarantine never turns failing capability evidence green.

Quarantine is not a substitute for credentials, seeded data or an implemented
probe. Legitimate absence is `blocked`; a probe's report-only policy is separate.
Malformed registry entries fail validation instead of silently disabling rules.

## Live-canary envelope

The emitter produces schema `honua-evidence.live-canary-envelope/v1`. Required
fields are `schema`, `manifestId`, `targetEnvironment`, `runAt` and `probes`.
Each emitted probe has `probeName`, non-empty `capabilityKeys`, `status`
(`green` or `red`) and `lastGreenAt`. The mapping is
`demo_canary.PROBE_CAPABILITY_KEYS`; only mapped, non-blocked results become
capability probes. The full check list stays in the gate report.

A quarantined probe is `red`, with its owning issue in the detail. An unmapped
quarantined check makes `overallStatus` red. Otherwise, mapped quarantine makes
the overall status partial. Without quarantine,
pass/fail map to green/red and a blocked run is partial. A consumer must inspect
probe results as well as the overall status.

The envelope also records `targetUrl`, `sourceRepo`, `sourceRef`, `sourceRunUrl`
and `candidateServerSha`. The last field is the revision advertised by the live
deployment; authenticated candidate binding requires the separate workflow
checks. An alternate target must retain its own environment identity rather than
being relabelled as the canonical demo.

The workflow uploads the report and envelope, commits a versioned envelope to
`honua-evidence/data/producers/live-canary/`, then dispatches aggregation.
Delivery or aggregation-dispatch failures remain pipeline failures even when
probes succeeded. The evidence consumer evaluates delivered artifacts for
freshness; a local output file does not prove delivery or a current aggregate.

# Honua 2026.1 capacity envelope and frozen SLO

The normative, machine-readable contract is
[`certification/capacity-envelope.v1.json`](../certification/capacity-envelope.v1.json). This page
explains the support claim; it does not carry independent numbers.

## Scope

Operator ruling A (2026-09-13) excludes `activeSubscriptions` and `alertEvaluationsPerSecond` from the 2026.1 capacity envelope. Realtime subscriptions and customer alerting are **Preview**; Preview features carry no capacity promise. The eight GA dimensions are `tenants`, `services`, `layersPerService`, `featuresPerLayer`, `maximumFeaturePayloadBytes`, `concurrentVirtualUsers`, `gpWorkers`, and `gpQueueDepth`. The eight required SLO signals remain availability, error rate, p95/p99 latency, throughput, queue age, saturation, and recovery.

Options B (keeping Preview dimensions in the lock as informational) and C (a second Preview
producer before the cut) were declined. A receipt may still report Preview observations; the
gate echoes them as informational and never uses them to qualify the GA envelope.

The 2026.1 support claim is bounded to the topology in the lock: one tenant, one service, four
layers per service, 10,000 features per layer, 1 MiB maximum feature payloads, 170 concurrent
virtual users, and one GP worker with a queue depth of 100. Larger or differently shaped deployments are not certified by this gate.

The soak uses the `soak` profile for at least 3,600 seconds. It must report availability, error
rate, p95 and p99 latency, throughput, oldest queue age, saturation, and recovery time. The
acceptance denominator is one complete candidate-bound soak at the entire declared envelope. All
eight signals are required; a skipped, null, non-finite, stale, or revision-mismatched signal fails.

## Substrate (operator ruling A, 2026-09-12)

The 2026.1 soak and disaster-recovery envelope is the **local-docker substrate**: the single-tenant
local Docker install (PostGIS, Redis and a local file-storage volume) that the candidate manifest's
`disasterRecovery` block resolves. Every recovery and capacity claim on this page is bounded to that
substrate and to nothing else. **AWS qualification remains unqualified in 2026.1** — ECS, Lambda and
every other cloud shape stay outside the qualified envelope for this release, and no local-docker
receipt extends to them. Recovery evidence for the local-docker substrate is produced by
[`e2e/dr-drill/full_platform.py`](../e2e/dr-drill/README.md) and validated by
`tools/validate_dr_receipt.py`; a soak receipt does not stand in for it, and it does not stand in
for a soak.

## Freeze and allowance

The lock's `frozenAt` and `baseline` identify the amended freeze and its measured source:
[local-docker candidate soak 34753540275](https://github.com/honua-io/honua-server/actions/runs/34753540275),
with an [immutable attested receipt](https://raw.githubusercontent.com/honua-io/honua-server/e185c1aaa3b0a45704a9f517dea828d1111db62d/capacity/9f2f16a5b9d19becf052f8b2635cf7c2ce109fdd-34753540275.json). It ran the manifest-pinned
`9f2f16a5b9d19becf052f8b2635cf7c2ce109fdd` image
`sha256:a5d962958ec8a6890ecd0f5f34f1da9c08a9d464da0418bdfbbc381c754d30fc`
in Production on a four-CPU Ubuntu local-docker runner, with four 10,000-feature layers,
170 virtual users and a 3,600-second observation window. All eight GA dimensions were verified
and all eight SLO signals observed. The older `2a98428e` nightly used the small fixture;
it is historical evidence and is no longer the threshold baseline.

The first [baseline 34749955367](https://github.com/honua-io/honua-server/actions/runs/34749955367)
observed p95 587.26 ms, p99 775.17 ms and 1,617.83 successful requests/s. Its derived limits did
not reproduce in the independent run 34753540275: p95 was 746.50 ms, p99 929.28 ms and throughput
1,254.18/s. That completed run is now the baseline, yielding 822 ms, 1,023 ms and 1,191/s bounds
with the unchanged allowances. The lock retains both runs and the failed qualification.

The current baseline error rate is **4.8903%** (259,240 failures / 5,301,057 requests). The absolute
ceiling remains **5%**, which also covers the first baseline's 4.9165% reading. This capacity budget does not waive
functional-correctness gates. The observed values and derivation are recorded in the lock.

Latency limits include the existing 10% allowance, rounded up to whole milliseconds. The
throughput floor includes the existing 5% regression allowance, rounded down to whole requests
per second. Availability, queue age, saturation and recovery retain their existing bounds,
which this run demonstrably meets; they receive no additional allowance. The error ceiling also
receives no additional allowance during evaluation. `regressionAllowance` is never applied twice.

The producer's measurement definitions remain explicit: availability, queue age and saturation
come from the driver's 3,600-second window; error rate and worst-scenario p95/p99 come from the
whole load run; throughput is successful requests divided by its 4,020 seconds, including ramps.
Recovery is a subsequent container restart to the first served query. A soak is separate from
full-platform disaster recovery. These readings must not be presented as different measurement
windows or as cloud qualification.

The amended lock must be committed before a **new** qualification soak begins. The baseline
receipt cannot qualify that new lock: its lock digest and start time predate the amended freeze.
Any later amendment likewise requires a new committed lock digest and another post-freeze soak.

## Receipt

`tools/check_capacity_soak.py` validates the lock and an extracted evidence bundle. The release train
runs this gate with its required `capacity_receipt_url`, which is the attested evidence ZIP rather than
a numeric JSON document. A missing or failing soak therefore blocks certification and promotion.
Candidate and observed revisions must equal the manifest-pinned `honua-server` SHA, and every replica
must name the same immutable image digest.

The ZIP contains the receipt plus every raw request-ledger, metric, load, and recovery artifact the
receipt references. The checker re-hashes those bytes, requires an immutable Actions artifact URL and
raw observation population for each, and rejects missing evidence or changed bytes. Every one of the
eight GA envelope dimensions must be exercised on the candidate topology; declared-only, skipped, demo,
source-built, or Preview/proxy workloads fail. Every one of the eight SLIs carries a frozen query and
hash, owner, alert, runbook, exact UTC window, candidate identity, raw-artifact references, exercised
workload references, observation population, computed value, and lock-derived verdict. Ratio signals
retain numerator and denominator; distribution/gauge/duration signals retain sample counts. Recovery
also retains an injected/detected/recovered timeline, while saturation retains worker, database, and
Redis populations separately and gates on their maximum.

Before extraction, the workflow verifies SLSA provenance for the complete ZIP, pins the signer to
`honua-io/honua-server/.github/workflows/load-soak-nightly.yml`, pins the source digest to the manifest
candidate, and denies self-hosted attestations. The receipt, raw hashes, producer identity, and workflow
run are therefore one signed subject. This is single-tenant GA evidence only: it creates neither a
per-tenant SLO nor a demo-environment SLA. No skipped or numeric-only outcome maps to green.


## Recomputed observations and remaining qualification

The `capacity-observations` artifact uses `honua.capacity-observations/v1`. It binds the
candidate image/SHA, producer, topology, lock hash and window to complete, disjoint request
intervals for every replica. Request buckets are lossless joint histograms of protocol,
HTTP status, in-band failure, duration and count. They are interval deltas from the full
serving population, never cumulative-counter snapshots, percentile averages or retained tails.
The checker recomputes success/error ratios, nearest-rank p95/p99 and throughput from that
single population. Periodic worker/database/Redis and queue observations, GA workload samples,
and injected/detected/recovered events for all three dependencies must cover the same window.
Sampling gaps or collection failures fail the gate. All eight queries are frozen in the lock;
changing a query and rehashing it inside a receipt cannot change the gate calculation.

The manifest's image digest is an independent checker input. Matching only the SHA or supplying
another well-formed image digest is insufficient. Provenance requirements are part of the amended
lock. The historical numeric baseline predates that lock and has no bound observation population, so
it cannot qualify a candidate.

A green result requires the approved producer to attest this observation schema for the locked
local-docker envelope: immutable candidate image, exercised GA dimensions, retained recovery probes,
and the raw artifacts the receipt cites. Numeric signal values, a source-built load report, or a
unit fixture are not a soak receipt and must not be submitted as release evidence. Preview
observations may be echoed and are not part of the GA denominator.

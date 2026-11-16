# Honua 2026.1 capacity envelope and frozen SLO

The normative, machine-readable contract is
[`certification/capacity-envelope.v1.json`](../certification/capacity-envelope.v1.json).
This page explains its qualification requirements; it does not carry independent numbers or
establish a current candidate pass.

## Scope

The six GA dimensions are `tenants`, `services`, `layersPerService`, `featuresPerLayer`,
`maximumFeaturePayloadBytes`, and `concurrentVirtualUsers`. The locked topology is one tenant,
one service, four layers per service, 10,000 features per layer, 1 MiB maximum feature payloads,
and 170 concurrent virtual users. Larger or differently shaped deployments are outside this claim.

Realtime subscriptions and customer alerting are Preview and carry no capacity promise.
`gpWorkers` and `gpQueueDepth` remain configured and exercised at their locked values (one worker,
queue depth 100), but are reported as Preview evidence, not GA requirements. GPServer requests
are excluded from the GA SLO population.

The `soak` profile requires at least 3,600 seconds at the entire declared envelope. All eight
structured signals remain required: availability, error rate, p95/p99 latency, throughput,
queue age, saturation, and recovery. Queue age is the Preview GP queue's oldest job: its value
and evidence are reported, but its threshold does not gate GA qualification. Missing, skipped,
null, non-finite, stale, or revision-mismatched required evidence fails.

Steady-state signals use the measured window. A separate fault drill follows that window;
it must recover every worker and Redis fault within 19 seconds and every Postgres fault within
36 seconds, with no data loss and no stuck GP jobs. Recovery evidence uses that fault period.

## Substrate

The qualified scope is **local-docker**: a single-tenant Docker install with PostGIS, Redis,
and local file storage, resolved by the candidate manifest's `disasterRecovery` block.
ECS, Lambda, and other cloud shapes are outside this capacity and recovery envelope. A local
receipt cannot qualify them. Local full-platform disaster recovery uses
[`e2e/dr-drill/full_platform.py`](../e2e/dr-drill/README.md) and
`tools/validate_dr_receipt.py`; capacity soak and full-platform disaster recovery require their
own evidence and cannot substitute for each other.

## Freeze and allowance

The committed lock's `frozenAt`, `receiptContract.frozenAt`, and SHA-256 bind the qualification
contract. Its baseline and derivation records explain the frozen limits; historical measurements
do not establish qualification of the current candidate.

The current bounds are availability at least 0.9999, error rate at most 0.05, p95 latency at most
822 ms, p99 at most 1,023 ms, throughput at least 357 requests/s, and saturation at most 0.85.
The lock stores a 30-second queue-age threshold, which is reported as Preview rather than gated.
Recovery also must meet the stricter per-dependency fault-drill bounds above.

Latency limits already include the 10% allowance, rounded up to whole milliseconds; throughput
already includes the 5% allowance, rounded down to whole requests/s. Never apply
`regressionAllowance` a second time. Other bounds receive no additional allowance. The capacity
error budget does not waive functional-correctness gates.

The checker applies the lock's scoring rules:

- Expected 413 refusals for low-zoom OGC tiles over the locked layer remain in the request and
  throughput population, but count as expected refusals rather than errors or outages.
- GP queue age is reported without gating its threshold. Only sampling failures matching the
  lock's Preview sampling-failure prefixes may be informational; other collection failures fail.
- Database pool saturation gates on its time-mean over the measured window. Worker and Redis
  gate on their peaks; the maximum of those three ruled components must meet the bound.

A lock amendment must be committed before a new qualification soak starts. A baseline receipt
cannot qualify an amended lock: its digest and start time predate the amended freeze. Every
amendment requires a fresh lock digest and another post-freeze soak.

## Receipt

`tools/check_capacity_soak.py` validates the lock and extracted evidence bundle. The release
train's required `capacity_receipt_url` identifies the **attested evidence ZIP**, not a numeric
JSON document. Missing or failing soak evidence blocks certification and promotion.

Candidate and observed revisions must equal the manifest-pinned `honua-server` SHA. Every replica
must name the same manifest-pinned immutable image digest, supplied independently to the checker.
Matching only the SHA or supplying another well-formed image digest is insufficient.

The ZIP retains the receipt and every raw request, metric, load, and recovery artifact it cites.
The checker re-hashes those bytes and requires immutable Actions artifact URLs and observation
populations. All six GA workload dimensions must be exercised on the candidate topology;
declared-only, skipped, demo, source-built, or proxy workloads cannot qualify it. GP dimensions
remain configured and reported as Preview evidence.

Each required signal includes the frozen query and hash, owner, immutable HTTPS alert and
runbook references, exact UTC measurement window, candidate identity, topology, raw-artifact
and exercised-workload references, observation population, computed value, and lock-derived
verdict. Ratio signals retain numerator and denominator; other signals retain sample counts.
Saturation retains separate worker, database, and Redis populations. Recovery retains the
injected/detected/recovered timeline and explicitly names the separate fault window.

Before extraction, the workflow verifies SLSA provenance for the whole ZIP, pinning the signer
to `honua-io/honua-server/.github/workflows/capacity-soak-candidate.yml` and the source digest to
the manifest candidate. Self-hosted attestations additionally require live Actions job and
restricted runner-group verification under the committed
[dedicated runner policy](DEDICATED-RELEASE-RUNNER.md); an empty policy admits none.

The checker binds verification to the evidence: each verified statement names exactly the ZIP's
SHA-256 subject, and the certificate's workflow ref, run id/attempt, runner identity, and source
commit must match the receipt and candidate under the approved runner policy. A self-declared
producer identity is insufficient. This is single-tenant GA evidence; it creates neither a
per-tenant SLO nor a demo-environment SLA. Skipped or numeric-only results never map to green.

## Approved producer

The approved producer is honua-server's `capacity-soak-candidate.yml`. It boots an immutable
candidate under the Production policy, seeds the locked envelope, drives the soak, runs the
recovery drill, and attests the evidence ZIP. `load-soak-nightly.yml` is not an approved producer:
it builds a checkout and emits load reports without the required attestation contract.

Dispatch the producer on a branch or tag whose head is the manifest-pinned server SHA, with
`candidate_sha` set to that same SHA. The certificate source digest, `producer.sourceRevision`,
`candidateIdentity.serverRevision`, and `observedRevision` must name one commit. Dispatching on
trunk to soak an older candidate attests the wrong source and is refused.

The producer must emit:

1. One ZIP of root-level files: at most 64 members, 64 MiB per file, and 256 MiB total, containing
   `capacity-soak-receipt.json` and all cited raw artifacts. Attest the ZIP with
   `actions/attest-build-provenance` (SLSA v1) under the approved runner policy and publish it at
   an immutable HTTPS URL used as `capacity_receipt_url`.
2. A receipt with `schemaVersion: 2`, `evidenceScope: single-tenant-ga`,
   `profile: soak`, committed `lockSha256`, candidate SHA/image digest, observed server revision,
   and exact UTC `window` of at least 3,600 seconds starting after the freeze and matching
   `steadyStateSeconds`. Include topology (replica ids, failure domains, image digests, database,
   Redis, GP workers), signing identity/signature, and `producer` repository, workflow path/ref,
   source revision, integer run id/attempt, and `https://slsa.dev/provenance/v1` predicate type.
   Require `status: completed`, except that `status: incomplete` is accepted when every sampling
   failure matches the lock's permitted Preview prefixes. This exception changes no other
   required evidence, signal, or GA threshold.
3. Exactly one raw artifact of kind `capacity-observations`, using
   `honua.capacity-observations/v1`, with the same candidate, window, topology, producer, and lock
   digest. Use `populationMode: complete-disjoint-intervals`, sample periods no greater than
   60 seconds, a request count, and gap-free, non-overlapping per-replica/incarnation interval
   deltas. Buckets retain `{count, durationMs, httpStatus, inBandError, protocol}`. Metric rows
   retain `{at, worker, database, redis, queueAgeSeconds}`; workload rows retain timestamps,
   locked dimensions, `executionMode: candidate-topology`, and `proxy: false`. Both sample
   series cover the measured window from start to end without gaps beyond the sample period.
   `samplingFailures` must be empty except for the explicit Preview exception. Retain the
   separate `faultDrill`, whose `recoveries` equal the observation document's recovery events.
4. Raw-artifact entries with `id`, `kind`, bundle `path`, byte `sha256`, positive
   `observationCount`, and a URI of the form
   `https://github.com/honua-io/honua-server/actions/runs/<producer runId>/artifacts/<id>`.
5. Exercised GA workloads at the locked target and observed values, with frozen workload queries,
   candidate identity/window, and populations matching the raw workload rows. Report Preview
   workloads separately. Include all eight structured signals with values and populations that
   the checker recomputes from raw observations, including saturation components and recovery
   events. The reported threshold verdicts must match the frozen lock; the checker separately
   applies its ruled GA scoring and Preview exclusions.

The fault drill must follow and be excluded from the measured window. Its data-integrity
readback must retain all four complete layer counts and equal, present SHA-256 values for the
expected and observed maximum payload, with `payloadIntact: true`. Report the GP stuck-job list
and drill failures; a producer verdict alone cannot establish recovery or data integrity.

Publish negative evidence when the soak misses the lock so the gate can refuse it. Never omit
a failed dimension or signal.

## Recomputed observations and remaining qualification

Request buckets are lossless joint histograms from the complete serving population, not
cumulative-counter snapshots, percentile averages, or retained tails. The checker recomputes
ratios, nearest-rank latency percentiles, and throughput from that population, applies the
locked GA exclusions and expected-refusal policy, and independently evaluates saturation and
the later fault drill. Changing a query and rehashing it inside a receipt cannot change the
frozen gate calculation.

Qualification requires the approved producer's authenticated, candidate-bound evidence for
the locked local-docker envelope. Historical numeric baselines, source-built reports, unit
fixtures, and Preview observations cannot establish a current candidate pass.

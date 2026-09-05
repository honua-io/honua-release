# Honua 2026.1 capacity envelope and frozen SLO

The normative, machine-readable contract is
[`certification/capacity-envelope.v1.json`](../certification/capacity-envelope.v1.json). This page
explains the support claim; it does not carry independent numbers.

## Scope

The 2026.1 support claim is bounded to the topology in the lock: one tenant, one service, four
layers per service, 10,000 features per layer, 1 MiB maximum feature payloads, 170 concurrent
virtual users, and one GP worker with a queue depth of 100. The September 4 amendment excludes
active subscriptions and customer alert evaluations from GA capacity obligations: both remain
Preview. The ten original dimensions are accounted for as eight GA workloads and two explicit
Preview exclusions; multi-tenancy remains Preview/Trial. Larger or differently shaped deployments are not certified by this gate.

The soak uses the `soak` profile for at least 3,600 seconds. It must report availability, error
rate, p95 and p99 latency, throughput, oldest queue age, saturation, and recovery time. The
acceptance denominator is one complete candidate-bound soak at the entire declared envelope. All
eight signals are required; a skipped, null, non-finite, stale, or revision-mismatched signal fails.

## Freeze and allowance

The thresholds were frozen at `2026-09-01T10:05:00Z`, before the candidate soak. They are based on
the unskipped Production baseline from packet 68: server revision `2a98428eâ€¦`, workflow run
`33492138360`. That run completed 1,470,402 requests with zero failures, about 1,885 successful
requests/second aggregate, worst-scenario p95 553.98 ms, and worst-scenario p99 578.56 ms.

Latency limits include 10% headroom and the throughput floor allows a 5% regression from that
historical threshold input. The baseline numbers do not satisfy the receipt contract and are not
promotion evidence. Availability, errors, queue age, saturation, and recovery have no additional
post-result allowance. The values in the committed lock are final for this candidate series; an
amendment requires a new lock version before another soak begins and cannot bless an observed run.

## Receipt

`tools/check_capacity_soak.py` validates the lock and an extracted evidence bundle. The release train
runs this gate with its required `capacity_evidence_url`; a missing or failing soak therefore blocks
certification and promotion. Candidate and observed revisions must equal the manifest-pinned
`honua-server` SHA, and every replica must name the same immutable image digest.

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
another well-formed image digest is insufficient. The new provenance contract has its own freeze
time; the historical numeric baseline cannot backdate provenance or qualify an earlier run.

This change provides the consumer and regression fixtures. The existing approved
`load-soak-nightly.yml` is still a source-build HTTP load runner, not a producer of this schema.
An immutable-image distributed producer, actual GA workload instrumentation, retained recovery
probes, and an attested candidate bundle remain required before this gate can turn green.
Synthetic unit fixtures are not a soak receipt and must never be submitted as release evidence.

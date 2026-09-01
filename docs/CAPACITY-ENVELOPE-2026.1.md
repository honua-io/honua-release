# Honua 2026.1 capacity envelope and frozen SLO

The normative, machine-readable contract is
[`certification/capacity-envelope.v1.json`](../certification/capacity-envelope.v1.json). This page
explains the support claim; it does not carry independent numbers.

## Scope

The 2026.1 support claim is bounded to the topology in the lock: one tenant, one service, four
layers per service, 10,000 features per layer, 1 MiB maximum feature payloads, 170 concurrent
virtual users, one GP worker with a queue depth of 100, 1,000 active subscriptions, and 10 alert
evaluations per second. Larger or differently shaped deployments are not certified by this gate.

The soak uses the `soak` profile for at least 3,600 seconds. It must report availability, error
rate, p95 and p99 latency, throughput, oldest queue age, saturation, and recovery time. The
acceptance denominator is one complete candidate-bound soak at the entire declared envelope. All
eight signals are required; a skipped, null, non-finite, stale, or revision-mismatched signal fails.

## Freeze and allowance

The thresholds were frozen at `2026-09-01T10:05:00Z`, before the candidate soak. They are based on
the unskipped Production baseline from packet 68: server revision `2a98428e…`, workflow run
`33492138360`. That run completed 1,470,402 requests with zero failures, about 1,885 successful
requests/second aggregate, worst-scenario p95 553.98 ms, and worst-scenario p99 578.56 ms.

Latency limits include 10% headroom and the throughput floor allows a 5% regression from that
working baseline. Availability, errors, queue age, saturation, and recovery have no additional
post-result allowance. The values in the committed lock are final for this candidate series; an
amendment requires a new lock version before another soak begins and cannot bless an observed run.

## Receipt

`tools/check_capacity_soak.py` validates the lock and a receipt. A receipt binds the candidate
revision and SHA-256 of the exact lock, proves its observation began after the freeze, includes all
signals and the whole declared envelope, and carries a non-empty signing identity and signature.
Before evaluation, the workflow also uses GitHub artifact-attestation verification to prove that
`honua-io/honua-server` signed the receipt; self-asserted signature text is insufficient. The
workflow uploads the validated receipt as immutable run evidence. No skipped outcome maps to green.

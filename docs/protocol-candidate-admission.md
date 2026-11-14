# Standalone protocol certification

`protocol-certification-candidate.yml` admits protocol evidence independently of
other train jobs. Dispatch on trunk with `lock_run_id`, `lock_run_attempt`, and
`platform_lock_digest` for the exact completed candidate producer. It downloads
the producer's `candidate-manifest` artifact; there are no editable SDK, ledger,
server, or enforcement overrides.

The shared candidate verifier requires the protected trunk producer and exact
run/attempt, hosted-runner provenance, immutable source manifest/matrix bytes,
and byte-identical lock regeneration. A signed lock or attested inputs with an
exact unsigned qualification lock can authenticate the candidate. Dry runs do
not qualify. The candidate remains in `rc` lock status during qualification,
including qualification for GA; this does not admit an already promoted lock.

The wrapper derives every input supplied by release-train to its same pinned
protocol evaluator, runs tier `release` with `strict` enforcement, and uploads
`protocol-certification-receipt`. Its pass requires both a successful evaluator
job and `overall_status=pass`. The receipt names `serverSha`, `imageDigest`, and
`platformLockDigest` in one object. Failed or skipped certification produces a
failed receipt. A prepare failure cannot create a receipt; the exact lock in
the run title still supersedes an older pass without certifying anything.

`rc_gate_window.py --set ga` reads this candidate-bound wrapper instead of the
train-only evaluator. It is required for GA and remains outside RC admission;
the existing 19 required RC gates and advisory AI policy are preserved. A
working manifest with a pending protocol ledger cannot pass strict evaluation.
This tooling change records no live candidate pass and does not start the RC2
candidate verification batch.

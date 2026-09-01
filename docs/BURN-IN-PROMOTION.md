# Burn-in promotion evidence

Promotion is a fail-closed re-tag of the exact certified RC bundle. It does not rebuild candidate
artifacts. The source of truth is a committed `certification/promotions/<rc-label>.json` record that
conforms to `certification/promotion-evidence.v1.schema.json`; Actions run IDs and their retained,
immutable artifacts are the evidence, not job outputs from an earlier workflow or repository variables.

## Start or reset a burn

Commit the exact `platform-lock.json`, then record its byte digest, that commit's full SHA, and the UTC
burn-start time. The burn-start commit must be an ancestor of the promotion record. Any later commit
that touches `platform-lock.json` invalidates the entire record—even if a later commit restores the old
bytes—so a candidate-affecting change requires a new lock, burn-start commit, clock, trains, and canaries.

## Required receipt

The record names:

- the freeze train's run ID as `rcTrainRunId`;
- exactly three distinct, passing live/strict release-train runs, ordered `freeze`, `during`, `after`;
- exactly seven distinct passing scheduled demo-canary runs, ordered at six-hour cadence; and
- the same `sha256:` lock digest on the burn start, every train, and every canary.

The freeze train must complete no later than burn start, the during train within the first 48 hours,
and the after train from hour 48 through hour 72. Promotion itself is allowed only from hour 48 through
hour 72. Canary intervals must be 5h30m–6h30m so schedule jitter is tolerated without accepting a
missing six-hour slot.

The promotion workflow downloads every run named by the record, verifies successful workflow identity,
compares the record timestamps with immutable Actions metadata, and validates the run artifacts. It emits
`promotion-readiness.json` with a result for lock identity/history, burn window, strict trains, canaries,
and exact-RC selection. Any failed condition stops before tag or release creation.

After the record is committed, the next successful strict train lets `request-promotion.yml` dispatch the
protected promotion as the scoped App identity. The protected environment remains the independent human
approval boundary. The promoted Git tag targets the freeze RC's certified source SHA, and the release
uploads that run's original manifest, matrix, gate report, and platform lock (plus signatures); no build
command exists in the promotion workflow.

# 2026.1 GA DB upgrade chaos hunt

Execution date: 2026-09-03 UTC  
Prior image: `ghcr.io/honua-io/honua-server:nightly-aot-ac30266`  
Candidate image: `ghcr.io/honua-io/honua-server:nightly-aot-4ca8326`

The driver uses the existing packet-94 Compose harness and seed. The seed published the real
`e2e_src_fs` service as layer 9 with two features. The prior baseline contained 119 journal rows;
the candidate advanced it to 120 by applying migration 107, `Honua.Server.Migrations.107_AddRbacRoleTombstones.sql`.

| Scenario | Outcome | Evidence / classification |
| --- | --- | --- |
| Kill and restart at every migration boundary | PASS | Killed the candidate while migration 107 was executing; restart converged to the expected journal and seeded row checksums. |
| Image rollback against migrated schema | PASS | Prior image served both seeded `e2e_src_fs` features after candidate migration 107. |
| Concurrent app start during migration | PASS | Two candidate processes contended on the migration lock; one journal advanced and both observed convergence with unchanged seeded data. |
| Partial failure inside a multi-statement migration | BLOCKED | The synthetic PostgreSQL backend-termination probe reached the transaction, but the disposable PostGIS container shutdown during the isolated rerun before the post-termination assertion could be trusted. No server issue filed from this result. The driver now always records this scenario as `blocked` (not evidence); a probe through the candidate runner is [honua-release#440](https://github.com/honua-io/honua-release/issues/440). |
| Journal/schema divergence | FAIL — P0 | After migration 107 was journaled, `honua.layers` was dropped and the candidate restarted. `/healthz/ready` returned 200 and logs reported “No database migrations to apply”; the missing journaled schema was not detected. |
| Migration re-run idempotency | PASS | Repeated candidate startup changed neither the journal nor seeded data counts/checksums. |

The confirmed divergence finding is filed in `honua-io/honua-server` with `bug-hunt/2026-09-03`,
`release/2026.1`, and `priority/P0`.

That issue is [honua-server#4001](https://github.com/honua-io/honua-server/issues/4001); it was closed on
2026-09-04. The FAIL row above is the 2026-09-03 observation and is not rewritten. Re-run
`gate-upgrade-chaos` with `scenario=journal-schema-divergence` against a candidate image that contains
the fix to record a new observation.

This branch contains hunt coverage only: no production server code or migration was changed.

## Review corrections (2026-09-28)

The historical PASS rows above are observations from the original harness, not certification with
these corrected checks. Boundary polling reused old container logs, concurrent startup accepted one
starter (including failure text), and convergence omitted physical schema. Those observations need
fresh runs. The historical tagged image coordinates were not digest-bound; their original bytes
cannot be established by resolving those tags today.

New runs require `repository@sha256:<64 lowercase hex digits>` for both images and record them in
`images.json`. Startup recreates the server container to isolate logs. Concurrent startup requires
both container health checks and removes both one-off containers on every exit. Divergence uses the
same readiness horizon as normal startup. Recovery compares a schema-only dump as well as journal
and seeded data. Skip-prepare reruns require the new expected schema dump and a valid saved seed
layer ID; old evidence directories must be prepared again.

The hand-written SQL partial-migration probe was removed because it never invoked the candidate
migration runner and could report `pass` from PostgreSQL rollback alone. `partial-migration-failure`
now always records `blocked` with that reason and makes the driver exit non-zero, so `scenario=all`
stays red rather than certifying application migration atomicity. A contract test asserts that the
scenario cannot record `pass`. The replacement — a candidate-owned multi-statement fixture, a
deterministic interruption point after a statement, and rollback/journal/schema/retry assertions
through that runner — is tracked in
[honua-release#440](https://github.com/honua-io/honua-release/issues/440).

Correction validation: 21 driver regression tests passed, including stale logs, one unhealthy
starter, migration-failure output, cleanup on creation/journal/state failures, delayed readiness,
saved layer validation, digest enforcement, and database-namespace cleanup. A disposable
PostGIS 16-3.4 smoke test exercised the actual schema capture/assert functions: unchanged schema
passed; independently dropping an index, constraint, column, or table failed while journal and
seed rows still matched. These checks validate harness behavior, not a new prior/candidate matrix.

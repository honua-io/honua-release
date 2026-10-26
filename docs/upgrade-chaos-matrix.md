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

## Partial migration failure through the candidate runner (2026-10-25, honua-release#440)

`partial-migration-failure` now interrupts the candidate's own DbUp runner inside a real pending
migration. It no longer sends a hand-written transaction to PostgreSQL. The runner applies every
pending script and that script's `public.schema_versions` INSERT in one transaction
(`.WithTransaction()` in `PostgresDatabaseMigrationRunner.BuildUpgrader`). Before the candidate
starts, the harness holds `SHARE` on `public.schema_versions`. The runner then executes the first
pending script's statements and blocks on its own journal INSERT, uncommitted. The probe acts only
after it observes that pause directly:

- the runner backend is waiting on a lock held by the harness, on an `insert … schema_versions`;
- it has a transaction id and at least one granted write-mode relation lock, so the script has
  already written;
- the last DbUp `Executing Database Server script '<name>'` line names a pending candidate
  migration.

If it cannot observe all three, the probe records `blocked` and exits non-zero, never `pass`. A
no-op or already-applied script cannot satisfy them.

The probe interrupts twice from the same restored baseline:

| Mode | Interruption | Additional runner assertion |
| --- | --- | --- |
| `backend-terminated` | `pg_terminate_backend` of the paused runner backend; the server process survives | the runner logs a migration failure and `/healthz/ready` stays unready |
| `server-killed` | `SIGKILL` of the candidate container | the kill must succeed and the container must be observed not running; otherwise the mode fails |

For both modes, the server is stopped before the journal lock is released, and the probe waits until
no client backend remains. It then asserts the following against the pre-migration snapshot: the
interrupted script has no journal row, the journal is unchanged, the schema-only dump is
byte-identical, and the seeded row checksums are unchanged. Finally it restarts the candidate. The
retry must become ready and converge to the reference journal, schema and seeded checksums.

The reference state in `prepare_fixture` is now produced by migrating a database restored from the
baseline dump, the same path every scenario takes. A `pg_dump` restore re-deparses CHECK expressions
(`ARRAY[...]::text[]` becomes `ARRAY[(...)::text]`). Before this change, every restored-baseline
recovery comparison (boundary kills, concurrent start and this probe) diverged on that difference
alone.

Validation (local Docker, 2026-10-25). These runs exercised this probe; they are not a fresh full
matrix certification:

- Prior `honua-server` 1d387ac (pushed to a disposable local registry for digest coordinates,
  `sha256:8e666f0c…`, migrations through 133); candidate nightly 8cb074f
  (`ghcr.io/honua-io/honua-server@sha256:2d7877c4…`, adds 134–136). `CHAOS_SCENARIO=partial-migration-failure`:
  both modes `pass`. The runner paused inside `Honua.Server.Migrations.134_VersionCanonicalDeployments.sql`
  with a transaction id, 8 granted write-mode relation locks (6 `AccessExclusiveLock`), and its
  `RowExclusiveLock` on `schema_versions` ungranted. `backend-terminated` logged
  `Database migration failed: 57P01` and stayed unready.
- The same probe with a broken recovery injected into the real database after the interruption
  (one committed leftover table, or a journal row for the interrupted script) recorded `fail`:
  "partial schema from interrupted '…134…' survived" and "interrupted migration '…134…' was journaled".
- Before the reference-state fix, the first real run interrupted and rolled back correctly but
  failed retry convergence on CHECK-deparse differences alone. That is how the restored-baseline
  mismatch above was found.

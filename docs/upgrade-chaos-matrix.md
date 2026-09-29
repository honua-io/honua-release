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
| Partial failure inside a multi-statement migration | BLOCKED | The synthetic PostgreSQL backend-termination probe reached the transaction, but the disposable PostGIS container shutdown during the isolated rerun before the post-termination assertion could be trusted. No server issue filed from this result. |
| Journal/schema divergence | FAIL — P0 | After migration 107 was journaled, `honua.layers` was dropped and the candidate restarted. `/healthz/ready` returned 200 and logs reported “No database migrations to apply”; the missing journaled schema was not detected. |
| Migration re-run idempotency | PASS | Repeated candidate startup changed neither the journal nor seeded data counts/checksums. |

The confirmed divergence finding is filed in `honua-io/honua-server` with `bug-hunt/2026-09-03`,
`release/2026.1`, and `priority/P0`.

That issue is [honua-server#4001](https://github.com/honua-io/honua-server/issues/4001); it was closed on
2026-09-04. The FAIL row above is the 2026-09-03 observation and is not rewritten. Re-run
`gate-upgrade-chaos` with `scenario=journal-schema-divergence` against a candidate image that contains
the fix to record a new observation.

This branch contains hunt coverage only: no production server code or migration was changed.

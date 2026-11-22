# Database upgrade chaos contract

The [upgrade chaos driver](../e2e/harness/upgrade-chaos.sh) runs real prior and
candidate server images against disposable PostGIS data. The manual
[workflow](../.github/workflows/gate-upgrade-chaos.yml) collects hunt evidence;
it does not itself change the release promotion verdict. Each candidate needs
fresh observations. This document records requirements, not a certification result.

## Inputs and isolation

Both `HONUA_PRIOR_SERVER_IMAGE` and `HONUA_CANDIDATE_SERVER_IMAGE` must use
`repository@sha256:<64 lowercase hex digits>`. The driver records those exact
coordinates in `images.json`; resolving a historical mutable tag today cannot
establish its original bytes. Use Docker Compose, curl and jq with the driver's
other declared command prerequisites in a disposable environment.

The default Compose file is `e2e/harness/compose.candidate.yml`. Set `E2E_OUT` to a
private output directory and provide a disposable `E2E_API_KEY` for seeding. Keep
raw database dumps, credentials and infrastructure identities out of public
artifacts. The driver seeds its prior image, snapshots the baseline and prepares
the candidate reference state from a restore of that same baseline. Journal
entries, a schema-only dump and seeded row checksums are all required: matching
journal rows alone cannot prove physical schema or data recovery.

Server startup recreates the container so migration-boundary detection cannot
reuse old logs. PostGIS must be fully initialized. Concurrent-start probes
require both starters to become healthy and clean up both one-off containers on
every exit. Readiness and divergence use the same observation horizon.

## Scenarios

Set `CHAOS_SCENARIO=all` for these six scenarios, or select one by name:

| Scenario | Required observation |
| --- | --- |
| `migration-kill-every-boundary` | Interrupt each observed migration boundary; restart converges to the reference journal, physical schema and seeded checksums. |
| `image-rollback` | The prior image can still serve the seeded data after the candidate migrates the schema. |
| `concurrent-app-start` | Both candidates complete healthy startup with serialized migration and unchanged seeded data. |
| `partial-migration-failure` | Both interruption modes below prove rollback of the candidate runner's uncommitted migration, followed by successful retry convergence. |
| `journal-schema-divergence` | A missing journaled schema object keeps readiness unready and emits a bounded schema diagnostic; a journal-only no-op migration decision is insufficient. |
| `migration-rerun-idempotency` | Repeated candidate startup becomes ready and changes neither the journal nor seeded checksums. |

A blocked or failed scenario is not passing evidence and makes the driver exit
nonzero. Preserve the matrix, image identity, logs and state comparisons in the
workflow's `upgrade-chaos-evidence` artifact. Driver regression tests and local
smoke probes establish harness behavior, not a fresh prior/candidate matrix.

## Candidate-owned partial migration failure

The probe interrupts the candidate's own migration runner inside a real pending
migration. A hand-written PostgreSQL transaction cannot prove application
migration atomicity. The runner applies each pending script and its
`public.schema_versions` journal insert in one transaction.

Before startup, the harness holds `SHARE` on `public.schema_versions`. It must
observe all three conditions before interrupting:

- The runner backend waits on a lock held by the harness for its journal insert.
- The backend has a transaction ID and a granted write-mode relation lock.
- The last DbUp executing-script log line names a pending candidate migration.

If any condition is unobservable, record `blocked`, exit nonzero and retain the
reason. An already-applied or no-op script cannot establish this interruption.

Run both modes from the same restored baseline:

| Mode | Interruption and required assertion |
| --- | --- |
| `backend-terminated` | Terminate the paused PostgreSQL backend while the server process survives; require migration-failure logs and readiness to remain unready. |
| `server-killed` | Send `SIGKILL` to the candidate container; require a successful kill and observed stopped container. |

Stop the server before releasing the journal lock and wait for all client
backends to leave. The interrupted script must have no journal entry; the whole
journal, schema-only dump and seeded checksums must equal the baseline. Restart
the candidate and require readiness plus convergence to the candidate reference
journal, schema and seeded checksums. Committed leftover objects or an inserted
journal row must fail the probe.

## Reruns and cleanup

`CHAOS_SKIP_PREPARE=true` requires an existing baseline dump, expected journal,
expected checksums, expected schema dump and a valid saved seed layer ID. Prepare
a new directory when any of those inputs is missing or belongs to another image
pair. Do not reuse historical outcome files as current observations.

The driver normally removes its Compose stack and volumes on exit.
`CHAOS_KEEP_STACK=true` is for explicitly managed debugging: its operator owns
subsequent teardown and must not claim cleanup until removal is verified.

The separate `compose-upgrade-rollback` scenario retains its legacy name but exercises
forward upgrade and candidate restart for the required `compose-upgrade` job in
`gate-upgrade`. It seeds its own prior installation and is excluded from `all`.
It requires the declared `HONUA_CANDIDATE_DB_SCHEMA` and a pre-upgrade backup.
Its `COMPOSE_UPGRADE_FAULT` controls (`drop-seed-rows`, `journal-stall`,
`restart-journal-change` and `restart-row-change`) are negative probes, never
substitutes for a successful unmodified upgrade and candidate restart. The prior
image is never started against the migrated database in this scenario.

# ARC services fixture interface

`services-fixture-scenarios.json` is a committed fixture-to-scenario catalog. Its exact SHA-256
is pinned in `agent_e2e_local_expectations.py`; it is also listed in `ai_arc_runner.RUNNER_FILES`.
Catalog loading checks both digest and schema. The fixture ID alone selects every scenario and the
corpus variant; the caller cannot select a scenario or change the query budget.

| Fixture ID | Corpus variant | Scenarios | Supported fault |
| --- | --- | --- | --- |
| town-A | A | P10, P15 | http_503_once, response_lost_after_commit |
| town-B | B | P10, P15 | http_503_once, response_lost_after_commit |

The ARC owner integrates through `ai_arc_services.run_fixture_services`, passing the decrypted goal
in memory as `goal`, plus `fixture_id`, `fault`, and `vm=ctx.slot.vm`. Unknown fixtures, missing
expectations, and faults without a compatible scenario plan return a named model-category failure
before agent dispatch.

P10 seeds imported fixture tables without publishing a service. Its frozen roles require a completed
agent `honua_publish_service` call on the exact seeded schema, table and connection, plus a new live
service and layer. The controller reads all published features and the raw table independently and
requires them to match, as well as comparing before/after table snapshots to the frozen fixture.
It scores the last model query, including a failed or foreign final query, and records the count
of all feature queries. The catalog freezes a limit of five; exceeding it fails the filter assertion.
The runner replays only the selected sealed action with controller credentials and scores the
result against the frozen oracle. The 503 is injected once on a query after the publication action. The lost-response plan forwards
the publication, confirms the committed service through a controller read, then returns a 503.
Its failed-looking action receives credit only when the sealed action digest, captured upstream
response and recorded controller confirmation agree. P15 runs only if P10 passes and the fault fired.

P15 uses the live service bound to the retained P10 case receipt and checks the service and layer
again before seeding its saved map. Its bootstrap custody remains in the case receipt as harness
evidence; it is not agent-work credit. The adopted slot supplies the SQL transport, which verifies
the reviewed reader role and uses the same structured preflight as the local runner.

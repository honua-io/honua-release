# ARC services fixture interface

`services-fixture-scenarios.json` is a committed fixture-to-scenario catalog. Its exact SHA-256
is pinned in `agent_e2e_local_expectations.py`; it is also listed in `ai_arc_runner.RUNNER_FILES`.
Catalog loading checks both digest and schema. The fixture ID alone selects every scenario and the
corpus variant; the caller cannot select a scenario or change the query budget.

| Fixture ID | Corpus variant | Scenarios |
| --- | --- | --- |
| town-A | A | P10, P15 |
| town-B | B | P10, P15 |

The ARC owner integrates through `ai_arc_services.run_fixture_services`, passing the decrypted goal in
memory as `goal`, plus `fixture_id` and `fault`. Unknown fixtures, missing expectations, and faults
without a compatible scenario plan return a named model-category failure, before agent dispatch.
The existing P10 and P15 scenarios publish fixtures as the controller, and neither defines a fault
plan. Therefore the fixture entry point currently fails closed: it is not admission-ready. A
successful implementation must import raw inputs, prove the agent published the service with its
own recorded action and live readback, and inject a compatible fault before removing the guard.

P10 scores the last model query, including a failed or foreign final query, and records the count
of all feature queries. The catalog freezes a limit of five; exceeding it fails the filter assertion.
The runner replays only the selected sealed action with controller credentials and scores the
result against the frozen oracle. P15 bootstrap custody remains in the case receipt as harness
evidence; it is not agent-work credit.

# Local corpus expectations (`<ID>.json`)

One frozen file per scenario, read by `tools/agent_e2e_local_expectations.py` and, through
`Scenario.expected()`, scored by `certification/agent-e2e/corpus/oracle.py` `verify()`.
Expectations come from the pinned fixtures (`corpus/town-*`, `corpus/oracles/town-*.json`),
never from a model run, and cover development variants A and B only. A file that names
variant `C` is refused.

```json
{
  "schemaVersion": 1,
  "scenario": "P10",
  "derivation": "where each value comes from in the fixtures",
  "variants": {
    "A": {"<assertion id>": <expected value>, "...": "..."},
    "B": {"...": "..."}
  }
}
```

The assertion ids under each variant must be exactly the pack's assertion list for the
scenario. An expected value is one of:

- literal JSON derived from the fixtures (for example the protected-content baseline);
- `{"$fixture": {"assertion": "<id>", ...}}`: resolved with
  `oracle.fixture_expectation(variant, <id>, ...)`; any other keys are passed as keyword
  arguments, and `"sortFeaturesBy": "<property>"` sorts the resulting features;
- any JSON containing `{"$role": "<role>"}` where a live identifier belongs. Roles are
  logical names, not ids a model created:
  - `seeded:<kind>:<name>`: a value in a seed-phase controller response, bound by the
    scenario's `roles()` (`RoleBinding(role, step, pointer=...)`);
  - `created:<kind>[:<name>]`: the single live resource enumerated after the session
    (`RoleBinding(role, step, items=..., id=..., owner=..., before=...)`) that is owned by
    `RunRecord.agent_principal` **and** absent from a complete seed-phase enumeration of the same
    path by the same principal. Both constraints are mandatory.

  At scoring time the runner resolves each role from its own recorded responses and
  substitutes the live id into the expectation before `oracle.verify`. An unresolved role,
  a created role that matches zero or several resources, or a created role that collides with
  another role fails the assertion.

## Frozen projections (required for every scored assertion)

```json
"projections": {
  "<assertion id>": {
    "spec": { "...": "tools/agent_e2e_local_projections.py operations" },
    "reads": {
      "<step>": {"kind": "http|mcp", "method": "GET", "path": "<full-match regex>",
                 "principal": "controller|agent|approver|anonymous", "phase": "seed|between|observe",
                 "origin": "controller|session-result", "replayOfModelAction": true}
    }
  }
}
```

The runner scores an assertion only with its frozen spec, and only from records that match the
frozen read for each step they use. A step the spec reads but `reads` does not declare fails.
`replayOfModelAction` requires the record to be a controller replay of a sealed model action of the
same case with the model's exact arguments. `variantProjections.{A|B}` may override per variant.

## Exclusions

`{"$excluded": "<ruling id>"}` in place of an expected value takes the assertion out of the measured
arc. Only rulings registered in `tools/agent_e2e_local_expectations.py` `EXCLUSION_RULINGS` are
accepted, and only for the assertions or scenarios they name. Today that is
`founder-ruling-no-multi-tenant-2026.1` (`tenant.denied` anywhere, and all of P18). Excluded
assertions are listed in every receipt as `excluded` with the ruling. They are never a pass, and a
case is a pass only when at least one assertion was scored and passed. A case whose every assertion
is excluded does not run; the aggregate lists it under `excludedCases`, outside the pass-rate
denominator.

The precedent is `../p10-local-docker.json`, whose protected-content resource ids are the
logical name `shelters`; `P10.json` carries the same values.

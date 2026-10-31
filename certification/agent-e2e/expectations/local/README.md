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

## Frozen roles and customer files

`"roles": [...]` holds the logical-role bindings (`RoleBinding` fields). The runner uses only these; a
scenario's `roles()` must repeat them exactly. A `created:*` role needs the owner filter, a seed-phase
baseline made with the identical request (method, path, principal, request body or MCP arguments), and
exactly one completeness proof, `total` or `next_page`. The pointer must exist in both enumerations.

Created-role completeness comes only from a reviewed enumeration contract (`ENUMERATION_CONTRACTS` in
`tools/agent_e2e_local_projections.py`, reviewed against honua-server a4bde4b8 with file:line citations).
The reviewed contracts are:
- unpaged: `GET /api/v1/admin/connections` (active connections only) and `GET /api/v1/admin/services` (one row per service name);
- cursor (`/nextCursor`): `GET /api/v1/admin/jobs`, and the MCP tool `honua_list_jobs` (`/result/structuredContent/nextCursor`);
- total (`/data/total`): `GET /api/v1/studio/package-drafts`, `GET /api/v1/studio/content-items` and `GET /api/v1/console/content`;
- total (`totalCount`): the MCP tool `honua_list_layers`.

These are deliberately not contracts: `GET /api/v1/admin/api-keys` hides some keys without a count, and the
import job lists return active jobs only. Request a page large enough to finish in one read. A binding may not
set its own `total`/`next_page`/`page_limit`. Both reads must start at the beginning (no offset, skip or
cursor), and neither may carry continuation, truncation or count evidence that contradicts completeness.
To add an endpoint, ask core: entries are reviewed against honua-server. Authorship may be `owner` (a row
field equal to the agent principal, rendered by a reviewed `owner_format`: `{id}` or `api-key:{id}`) or
`created_by` (`{"tool": ..., "pointer": "/result/...", "where"?: {"argument": "/ptr", "role": NAME}}`: the id
appears in the server's captured reply to a successful sealed agent call of that tool, optionally only the
call whose own argument equals another role's id, e.g. the source table of one of several publications).
Controller SQL is structured: `recorder.table_inventory(step, "honua_data")` (a reviewed complete contract,
`items` `""`) and `recorder.table_rows(step, "honua_data", table, columns)`; scenarios never write SQL. A row read
binds to a table role with `{"sqlFrom": "table" | "qualified"}` (the table it named), and a frozen read rule for
it uses `kind` `sql`, `method` `SELECT` and a `path` regex over the generated SQL (or `".*"` with `addresses`).
A `modelReplays` projection needs a frozen read rule keyed `"$modelReplays"`; every replay it consumes is checked
against it and hashed into the assertion evidence. `{"fullPath": true}` compares a whole request path. Inventory preservation
(`"resource": "inventory"`) needs `"items"` and meets the same completeness rule.

Runner roles need no binding: `session:previous`, `session:current`, `session:visible-message-ids` and
`session:handoff-sha256` (the sha256 of the session-1 final answer).

Frozen read rules may also pin `"session": N` (with `replayOfModelAction`: the replayed model action came
from session N) and `"exactPath"`. A rule's `phase` may be `seed`, `between` or `observe`, and the runner
scores the record only in that phase. A `protectedContent` row may set `"beforePhase": "between"`, or
`"resource": "inventory"` for an inventory snapshot; an inventory row's reads must pin `exactPath`.

Other roles: `recorded:<name>` is the canonical sha256 (bare hex, the same format as the `canonicalSha256`
projection) of a value at `pointer` in a seed- or between-phase read (use it for "persisted unchanged" checks).
`fault:committed-resource` is the verified-commit resource id of a fired `response_lost_after_commit` fault. `child:<kind>` is a scalar in an observe-phase read that
addresses its `parent` role.

`"customerHandoffs": {"A": ["database:<name>"]}` attaches a database login the scenario seeded during seed with
`stack.extra['databaseHandoff'](name, schema)`; its password is redacted from exported evidence.

`"customerFiles": {"A": [...], "B": [...]}` is the case's attachment inventory. Only manifest files with
visibility `selected-case-input` in the variant's `town-<variant>/` folder can be attached, and only those
whose `scenarioIds` (when present) name the scenario. Controller-only files (oracles, SQL seeds) are refused.

`protectedContent` rows must name the protected resource's identity roles (`resource`). Both snapshots must
address every one of them and be the identical request.

## Exclusions

Exclusions come only from the frozen preregistration list in `certification/ai-arc/PREREG-arc-v1.md` (it lands with honua-io/honua-release#1163, introduced in commit bb655ee1; `PREREG_REFERENCE` in `tools/agent_e2e_local_expectations.py` records the pointer, and a test checks the list against the document once it is present):
`tenant.denied` (any scenario), `P18` (the whole scenario) and `custody.worker-replaced@local-docker`. The
runner applies them itself (`prereg_exclusion`). An optional `{"$excluded": "<id>"}` marker must name the
covering entry, and no other marker is accepted. Excluded assertions are listed in every receipt with their
exclusion id and are never a pass. A case whose every assertion is excluded (P18) does not run, but it
stays in `n` as not passed, and the aggregate reports it under `excluded` / `excludedCases`. The aggregate
rejects any receipt whose assertion rows differ from the pack inventory, or whose excluded rows differ from
the preregistered list.

The precedent is `../p10-local-docker.json`, whose protected-content resource ids are the
logical name `shelters`; `P10.json` carries the same values.

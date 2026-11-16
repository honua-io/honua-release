# Contract breaking-change suppression

`gate-contract` runs `tools/check_contract_suppression.py` against
`certification/contract-suppression-policy.yaml` and a fresh repository-variable listing.
Its `contract-suppression` fragment contributes to the candidate's `gate-report.json`.
A historical observation of a variable or token does not establish its current state.

## Gate requirements

The server repository variable `OPENAPI_ALLOW_BREAKING_CHANGES` disables OpenAPI
breaking-change enforcement for every pull request when enabled. It must be absent
or false before the first published control-plane API release. The gate reads the
variable; it does not change repository settings.

| Input or condition | Result |
|---|---|
| Variable absent or false, replacement mechanism evidenced, window audit healthy | `pass` |
| Variable true | `blocked` |
| Listing unreadable or value unparseable | `blocked` |
| Replacement mechanism not declared landed with evidence | `blocked` |
| Audit missing or adverse | `blocked` |
| Audit stale while suppression is active | `blocked` |

`blocked` remains visible in bootstrap runs and fails strict release enforcement.
Unknown state never counts as disabled suppression. The policy register remains a
consumed gate input: its recorded audit must be evaluated for freshness, rather
than treated as current certification because it exists in the repository.

## Cross-repository read

`RELEASE_GH_TOKEN` needs read access to the server repository's Actions variables.
The workflow must read the complete listing, combining paginated responses into
one JSON object. A failed single-variable lookup cannot distinguish an absent
variable from missing access.

The decision record exposes:

| Field | Meaning |
|---|---|
| `variable_readable` | Whether the listing was successfully read and interpreted |
| `suppression_state` | `active`, `off`, or `unknown` |
| `suppression_active` | Fail-closed interpretation; unknown counts as active |

Unreadable evidence identifies the token/access failure separately from active
suppression. The read step claims success only after `gh api` succeeds. On failure,
the evaluate step supplies `--unreadable-reason`; neither the job nor its fragment
may convert an unknown state into a pass. Logs print variable names or bounded
failure information, never the full listing or token.

## Per-PR acknowledgement

The server's `scripts/ci/openapi-breaking-change-policy.py` resolves the scoped
replacement for the repository-wide switch. When that switch is false, an
intentional break requires both the exact `OPENAPI_BREAKING_CHANGE_APPROVED` task
in the PR template and an update to a breaking-change document: the control-plane
migration guide, versioning-and-support, or release checklist.

`scripts/ci/validate-openapi-contracts.sh` emits an Actions warning and job-summary
section listing suppressed findings and their acknowledgement source. The global
variable takes precedence over per-PR acknowledgement while enabled; the scoped
mechanism alone does not close the gate.

## Audit and publication

Replay the server's checker with enforcement enabled for every trunk commit since
`suppression.window_opened_at` that touched `docs/developer/api-specs/`, comparing
each commit with its first parent. Use an isolated checkout, record every result,
and investigate every flagged change before assigning an audit verdict. A checker
execution failure is not evidence of a clean contract.

The replay covers curated contract documents. Runtime changes that do not touch
those documents require separate OpenAPI drift checks.

Before publishing the first control-plane API release:

1. Repeat the window audit through the intended candidate and update
   `window_audit`, including its coverage timestamp and findings, in the register.
2. Have the release lead delete the global variable or set it false in the server
   repository. Intentional breaks then require per-PR acknowledgement.
3. Run `gate-contract` against the candidate with a fresh variable listing and
   strict enforcement. Require a passing `contract-suppression` fragment and retain
   the resulting `gate-report.json` as release evidence.

For offline decision-core inspection, supply a securely stored, combined listing:

```bash
python3 tools/check_contract_suppression.py \
  --policy certification/contract-suppression-policy.yaml \
  --variable-listing /secure/path/variables.json
```

`tools/test_check_contract_suppression.py` tests the decision core and workflow
wiring. These tests do not prove current token access, variable state, or a live
release pass.

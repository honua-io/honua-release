# Candidate classification fixtures

These inputs exercise check classification, workflow enrichment and latest-attempt
selection. They are test fixtures, not execution records or qualification evidence.
All revision hashes, run/job/suite/workflow identifiers, timestamps and URLs are
synthetic. URLs use `example.invalid`; dates start in 2000.

Check names, app identities, workflow paths, outcomes, attempt numbers and relative
ordering preserve the cases needed by the policy tests. Repeated identities remain
consistent across check and workflow records. Distinct suites remain distinct.

- `sdk-dotnet.json`: advisory certification alongside core build/test checks.
- `sdk-js.json`: advisory jobs and superseded build and release-check attempts.
- `server-observer.json`: workflow-scoped observer exclusions and refusal when actual
  Actions job and pinned shard selection observations are missing.
- `server-parity.json`: observer exclusions that require the recorded workflow path.
- `server-shard-attempts.json`: failed and successful attempts in distinct suites;
  enrichment joins them by workflow identity, while missing enrichment fails closed.

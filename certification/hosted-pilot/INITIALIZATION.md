The first saved campaign plan uses `dispatch_budget = null`. It creates the
retained accounting store and its state-only initialization role, and returns
`accounting_initializer` with the actual role, candidate and namespace. Controller,
fixture and native-bootstrap dispatch handoffs remain null; the dispatcher has
no `states:StartExecution` grant. This staging is paid infrastructure and still
requires the existing pilot owner budget, including its nonzero historical,
live-resource and cleanup holds.

A current signed, non-dry-run `candidate-manifest` from the trusted release train
is required next. The candidate key is `agent_e2e_gate.component_binding(lock)`,
which binds `components` and `contentDigests`; hashing the manifest or choosing an
old lock does not produce the current candidate identity. The bootstrap job
runs the existing full candidate authenticator before sealing accounting.

Review `certification/agent-e2e/bootstrap.json` into source with exactly:
`schemaVersion: 1`, `candidateKey`, `history: {path, sha256}`, the original
`ledger`, `expiresAt`, and nonempty `liveLiabilityReferences`. The ledger uses the
named $2,000/$200 amendment, retains its complete prior version-2 history, and
has no model attempts or paid admissions. Its reserved infrastructure operation
`initialization-history-usage` has purpose `telemetry`, no actual charge, and
`basisDigest = canonical_digest(history JSON)`. Its amount must cover rounded-up
gross account usage plus a diagnostic allowance, at least $151 for the dated
collection. The existing bootstrap infrastructure and cleanup rows, all legacy
liabilities and the live resources/cleanup obligations also remain in the
original ledger. Each liability reference is a tracked, hash-bound JSON file
under the reviewed namespaces with schema
`honua.hosted-pilot-retained-liability/v1`, `candidateKey`, nonempty `operationIds`
that still have reserved rows, and the actual retained `observation` metadata.
`operationIds` names each row's `operationId` group, not its individual `id`
reservation. For example, one live cleanup operation can retain separate
infrastructure and cleanup reservation IDs.
Unknown charges never become settled zero charges.

Dispatch the existing `agent-e2e-campaign.yml` with `mode=bootstrap` and the exact
trusted candidate run/attempt. This mode authenticates the candidate, seals the
reviewed nonzero ledger and attests `bootstrap-accounting.json` under the existing
campaign workflow identity. It does not freeze a model campaign, invoke a model
or create AWS resources. Wait for the actual successful completed producer run.

Then review `certification/agent-e2e/initialize.json` into source with exactly:
`schemaVersion: 1`, the same `candidateKey`, actual `state` and
`initializerRoleArn` from Terraform, the same `history` reference, `expiresAt`,
and `bootstrap: {artifactDigest, sourceSha, runId, runAttempt}` from that actual
completed producer. Dispatch `hosted-pilot-initialize.yml`. It independently
checks the exact successful run and attestation, observes its dedicated AWS
caller, and invokes the existing fenced `DurableExecution` initialization.
Existing current or historical object versions, missing state heads and partial
initialization remain reconciliation cases. No generation or budget is reset.

The workflow publishes only the actual generation reference/digest and original
ledger/artifact digests. Review that observed digest and original reservation
bindings into `dispatch_budget` and use the ordinary saved-plan workflow to
enable the handoffs. Each subsequent native bootstrap, human workspace setup,
fixture preparation and model worker must use this same retained generation.
No preparation or worker may bootstrap a second allowance.

For human workspace preparation, an actual Console session must provide the
private immutable `human_operator_credential` reference. Only the trusted
controller receives that OperatorBearer. Its native target read must positively
identify a human, then the actual issued agent must independently read its own
principal and the human-created workspace/turn. The observed Studio owner UUID
and canonical workspace actor are distinct fields. Neither the target identity
projection nor local controls qualify hosted Cognito sign-in.

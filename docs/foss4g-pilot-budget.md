# FOSS4G candidate budget amendment

The accepted FOSS4G pilot authorization raises the retained candidate ceiling to
$2,000 inclusive of failures, shared infrastructure and cleanup, protecting $200
for cleanup. It does not authorize a new ledger, erase spent funds, start paid
work, or change the existing RC campaign's $500/$50 contract.

The trusted controller may explicitly amend an initialized ledger for the same
candidate:

```bash
python3 tools/agent_e2e_budget.py amend-foss4g \
  --ledger retained-candidate-ledger.json \
  --candidate-key sha256:<exact-existing-candidate-digest>
```

Use the actual existing digest, not the placeholder. Retain the ledger and its
controller concurrency lock across all campaigns. The command accepts one fixed
owner policy, `foss4g-ai-2026-owner-2000-200`; there is no arbitrary ceiling option.
This CLI is an authorized controller operation, not an independent authentication
mechanism for owner approval. Never expose the ledger or this operation to the
agent sandbox.

Schema 3 binds a full prior schema 1/2 ledger snapshot with its canonical SHA-256.
All historical attempts, operations, reservations and settled charges remain in
the same ledger and count toward the new ceiling. Historical rows cannot be
removed or changed, except an existing reservation may settle to its observed
charge. Repeating the amendment is idempotent; `init` never resets an existing
ledger. An actual charge above its reservation blocks the amendment and later
work even if the total fits the larger ceiling. Later independently observed
cleanup charges can still be retained after an overrun.

The ordinary `init` command still creates schema 2 with $500/$50. The existing
252-case RC qualification gate requires schema 2 and rejects an amended ledger;
it cannot silently qualify a candidate using the FOSS4G allowance. The pilot
profile records this separate authorization but does not consume it automatically.
At most four isolated paid attempts may run concurrently; this is not a
four-attempt total across the matrix. The profile names this
`maxConcurrentPaidAttempts`. Schema 3 now provides candidate-scoped admission
operations in the same retained ledger:

```bash
python3 tools/agent_e2e_budget.py acquire-paid-attempt \
  --ledger retained-candidate-ledger.json --candidate-key "$CANDIDATE_KEY" \
  --admission-id "$ADMISSION_ID" --owner-token "$CONTROLLER_OWNER_TOKEN" \
  --campaign-id "$CAMPAIGN_ID" --case-id "$CASE_ID" --attempt 1 \
  --expires-epoch "$PAID_ATTEMPT_EXPIRY"
```

The trusted controller supplies a distinct admission ID, a private owner token of
20–128 permitted characters, and an absolute expiry no more than 6,000 seconds
in the future. `check-paid-attempt` with the same candidate, ID and token refuses
expired, released or foreign-owner admissions. An expired admission **still
occupies capacity**: time passing does not prove its worker stopped. The same
campaign/case/attempt cannot be readmitted under a new ID or token.

`release-paid-attempt` requires the matching owner token, `--cleanup-status pass`,
and `--infrastructure-reservation-id` / `--cleanup-reservation-id` referencing
exact-attempt **settled** reservations. Only the trusted controller may assert
cleanup pass, after independently observing worker/resource cleanup. A cleanup
failure, unknown charge or reservation overrun retains both liabilities and the
occupied admission for reconciliation. Release never removes history, costs or
attempt identity. The owner token is not a model-facing credential and must not
enter the agent sandbox.

Existing local ledger locking and fenced DynamoDB/S3 snapshot publication retain
these admission transitions. The existing worker launch guard also requires an
active unexpired admission matching its candidate, attempt ID and exact
campaign/case/attempt/mode, in addition to reserved liabilities; released or
stale slots cannot fund a worker start. No competing controller or timeout-based lease
recycling was added. The existing controller's candidate-wide lease remains
stricter (one controller at a time). The RC252 dispatcher explicitly blocks a
schema 3 ledger before backend preflight or model work: it has no pilot-case
oracles or dispatch contract. Pilot dispatch still needs to invoke admission and
check before paid work, independently observe cleanup, settle all charges, then
release. These primitives do not claim pilot dispatch integration or four-way
parallel execution is available. Per-case reservation attempt numbers retain
their existing 1/2 meaning.

No live ledger was amended and no paid run was started while implementing this
support. Tests use temporary synthetic ledgers only.

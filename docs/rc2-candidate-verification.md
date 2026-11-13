# rc.2 candidate verification evidence

This is preparation for [#1132](https://github.com/honua-io/honua-release/issues/1132)
and [#1133](https://github.com/honua-io/honua-release/issues/1133). Execution starts
only when the release owner opens the candidate verification window. Preparing or
merging this document does not discharge either issue's acceptance checklist.

## Candidate admission

Use the normal `nightly-certification.yml` pipeline. Do not dispatch another night
to prepare these tickets. Its resolver prepares unadmitted inputs; deployment
qualification and protocol producers then qualify and bind the snapshot. The train
adopts that snapshot and mint runs only after the train succeeds. A prepared
deployment ref or a snapshot ref alone is not a qualified nightly lock.

On the first successful nightly event, preserve these identities together:

| Identity | Evidence to retain |
| --- | --- |
| Nightly execution | Repository, run URL, run ID, attempt, reviewed release SHA |
| Candidate inputs | Full `protocol_ledger` output `candidate_ref`, frozen manifest and matrix bytes and hashes |
| Server | Manifest source SHA, artifact source revision, immutable image digest |
| Installed components | All frozen versions, source revisions and artifact integrity values |
| Admission | Minted `nightly-lock/<label>` tag and exact lock bytes/digest; train gate report and binding |
| Raw results | Per-gate artifacts, producer run URLs, attempts, cell results and durable evidence URIs |

Follow [the cut runbook](rc2-cut-runbook.md) for lock generation and binding.
Keep the snapshot commit, server source SHA, manifest hash and platform-lock digest
distinct. No mutable trunk pin, release tag alone, successful job badge, or manifest
hash substitutes for the lock used by the candidate's gates.

## Reuse the candidate's train

| Acceptance area | Existing candidate-bound path | Required evidence |
| --- | --- | --- |
| Local Docker journey | Train `gate_e2e` calls `e2e-local-docker.yml` with `candidate_ref` and `require_real: true` for a real night | Stage-level report and committed terminal receipt; all stages required by the issue must execute |
| Installed SDKs | Train `gate_installed_clients` calls `gate-installed-clients.yml` with `candidate_ref` | Published package integrity, per-scenario matrix, raw requests/responses and blockers |
| Protocols | Nightly `protocol_ledger` dispatches producers, joins their evidence and binds the ledger; train `gate_protocol_certification` checks the frozen ledger with `tier: release` and exact expected identities | Ledger digest, denominator source revision, each producer's source/image identity and per-cell verdict |

Download existing artifacts once from the completed run, into a fresh evidence
directory, using `gh run download RUN_ID --repo honua-io/honua-release --dir DIRECTORY`.
Keep the run attempt with the downloaded evidence. Do not rerun a merged PR's checks
or dispatch standalone workflows to reproduce evidence already supplied by the train.

The planner `tools/candidate_verification.py plan` only prints workflow names. Its
`--candidate-sha` syntax check does not authenticate a snapshot or bind execution.
The `record` CLI has no persistence sink and refuses to record results.
`candidate-verification.yml` remains disabled; enabling its job would still only
print a plan. Neither is an automatic candidate dispatcher or completion recorder.

Standalone `e2e-local-docker` and `gate-installed-clients` manual dispatch inputs do
not expose `candidate_ref`; the train's reusable calls do. `Installed client
certification` is a separate workflow and does not adopt immutable candidate inputs.
Its green default-manifest run cannot stand in for this candidate's installed-client
proof. Missing issue-specific coverage stays blocked until its producer supplies
evidence tied to the frozen candidate; it is not inferred from aggregate train green.

## Other acceptance evidence

For every row, compare the issue's current acceptance text with the raw candidate
evidence. Record a separate verdict for each criterion, including those the train
does not currently produce.

| #1132 criterion | Evidence to join |
| --- | --- |
| MCP attestation | Exact spec/schema-index attestations, frozen repin and `collect_post_gate` verification; no expired exception |
| Published maps | Public/private readers of the published map and proof that installer fallback was absent |
| Provisioning lineage | Candidate deployment parameters carrying lineage; retain the declared deferred server field |
| Operate recovery | Installed REST/MCP recovery receipts and observed recovered state |
| Terminal journey | Local Docker stages 1–8 with server-emitted identities and durable committed receipt |
| Durable sharing | Portal restart and raw responses proving the same durable share |
| CNG | Candidate producer's per-cell breakdown and corresponding status document |
| Governed sharing | Conversational proposal, approval and governed sharing observations |
| Model composition | Real-model compose/save/reopen/propose/approved-URL trace using released pins |
| Map rendering | Full map specification rendering plus authority and revoked-source observations |
| Support policy | Policy citation in the candidate lock/record |

## Installed clients and protocol acceptance evidence

| #1133 criterion | Evidence to join |
| --- | --- |
| gRPC producer | Exact-candidate canonical-client producer receipts |
| Migration producer | Candidate migration execution and durability receipts |
| Supported cells | All 39 supported cells and the two declared D6 blockers, each with a durable URI |
| .NET fixtures | Installed-client fixture verdicts from the published package |
| .NET package | Installed NuGet identity/integrity and real server interaction |
| .NET coverage | Truthful skip/coverage report with missing execution kept blocked |
| JS admin | Published SDK admin qualification receipts |
| JS adapter | Adapter audit and RBAC observations |
| JS selection | Bundle/selection and terminal receipt evidence from released pins |
| JS ordinary MCP | Ordinary MCP/Studio installed-client trace |
| JS runtime floor | Supported-runtime execution and published runtime declaration |
| JS roster | MCP roster and published-layer observations |
| Python | Installed edit and gRPC execution receipts |
| External protocols | All 52 external cells, including exact-candidate CITE and PyQGIS producer evidence |

## Recording and failures

The release verification owner records one criterion per row on the two issues:
candidate snapshot, lock digest, criterion, `pass`/`fail`/`blocked`, producer run URL
and attempt, durable raw-evidence URI, and the observed result. A blocked or skipped
criterion is never a pass. Do not check an issue box based only on implementation
merging or fixture/control tests. Close an issue only when every required criterion
has qualifying candidate evidence.

Route failures for these two tickets to the release verification owner. Product
repairs remain with their existing owners, including Redis work. Preserve the
failed run and its partial evidence; request a bounded producer repair with the
exact candidate identity and failing criterion. Re-execution requires the normal
release authorization and gates. Do not poll CI or start a parallel nightly.

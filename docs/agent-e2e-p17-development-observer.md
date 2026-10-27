# P17 development denial observer

This extends the existing Studio native development producer under issue #623.
It does not activate a campaign or qualify the frozen 252-case release pack.
P16 key lifecycle and P19 injection remain unsupported. Frozen packs, expected
principals, oracle semantics and release pins are unchanged.

A development bundle may declare `honua.studio-denial-bindings/v1` alongside the
mandatory `honua.worker-custody-bindings/v1`. All four P17 assertions remain
required. The denial contract retains the publication relationship fields plus
`targetId` and the bounded full `taskPrincipal` projection, independent `observers`, separate `requesters` with the original
workspace/turn, and `anonymousObservers`, each indexed by attempt. The requester
principal must equal the native custody task principal. Only
`approval.pending.proposalId` and the two `approval.no-bypass.requests` resourceId
slots may bind that proposal ID. Request methods GET/POST, statuses 200/403 and
principals remain frozen literals. The task principal projection contains actorId,
studioOwnerId, tenantId, authenticationKind and isHumanSession; additional or
changed identity fields are refused rather than discarded. There is no recursive interpolation.

After one actual model-selected owned pending proposal, the controller verifies
the current task target and full principal against the original native custody.
It issues an authenticated GET of that same proposal and an ordinary empty-body
POST to its approve route, which accepts no body parameters. No permission or
role is granted to make this probe reach a particular branch. If the task cannot
read the proposal, or its POST gets a generic permission refusal rather than the
specific separation-of-duties response, this producer reports unsupported and
cannot pass P17. A generic 403 is not a self-approval observation.

The controller independently reads back the unchanged AwaitingApproval proposal
and both authenticated and anonymous publication absence. It retains bounded,
credential-screened HTTP intent/response originals and marks the witness
`controller-issued-denial-probe`; this is never inserted into the model action
journal. The gate replays native proposal/target identity, the specific denial,
poststate, route absence and raw response digests. Each witness role must use
its fixed producer step suffix and common probe prefix, with the original
ScenarioHttp reference-key semantics. The trusted controller performs those
requests sequentially; step purposes are not independent transport timestamps
or proof against a compromised controller fabricating its own records. Original bootstrap retirement
and separate worker custody proofs remain mandatory. Ambiguous requests are not
retried: operators must reconcile the original retained request. A successful
approval or changed publication state is a failed proof, not a permission denial.

Local HTTP fixtures demonstrate deterministic producer and replay behavior only.
They are not live Server observations, model behavior evidence, or installed
qualification. A future approved campaign still needs coherent candidate inputs,
original custody and accounting, source-frozen development expectations, and the
existing release gates and operator approvals.

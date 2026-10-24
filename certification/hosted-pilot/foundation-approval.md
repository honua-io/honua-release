# Foundation deployment approval

The foundation workflow checks the versioned policy in
`foundation-environment.json` before starting its credential-bearing job. A
missing environment, different reviewer identity or additional deployment branch
refuses execution. This check runs before GitHub can implicitly create an
unprotected environment for the deployment job.

After the release implementation is reviewed and merged, run
`hosted-pilot-foundation-environment.yml` on release trunk. It uses the existing
release GitHub administration credential to create the declared environment and
its exact trunk branch policy. It verifies the result and refuses to overwrite
an existing different protection policy. Configuration stays in source and the
checked-in workflow; no manual repository or AWS configuration is required.

Plan preparation uses `terraform-ephemeral`. Select one phase explicitly:
`custody-only` or `remote-backed-foundation`. Plan-only dispatches discard the
binary and cannot later be converted into apply authorization.

An apply dispatch retains its newly prepared binary privately on the original
runner. Its review artifact is published before the separate
`terraform-live-approval` job starts. The declared reviewer is Mike McDougall;
the owner can review a dispatch they initiated. The assistant does not approve or
bypass that review. The approval job binds actual native review history to the
exact original source, run/attempt, phase, configuration and saved-plan hash. The
original runner waits for the protected job to finish successfully, then applies
that binary only and stops. The dependency graph never makes approval depend on
the still-running prepare/apply job.

Review metadata shows immutable preparation, approval and execution deadlines.
The approval window is at most twenty minutes and leaves fifteen minutes for
execution, within the unchanged original funding and infrastructure expiry.
Preparation is limited to eight minutes, apply to twelve minutes, and the runner
job to forty-five minutes, leaving bounded recovery/cleanup time. A separate
watchdog has no AWS credentials and alone receives `actions:write`; it verifies
its exact workflow/source/run/attempt and accepts no other cancellation target.
It cancels its own failed or expired run so an unreviewed environment job cannot
wait indefinitely. Original apply-admission metadata is retained even on failure.
Cancellation can leave partial resources or unknown costs: all original funding,
expiry and liabilities remain held, and native state/recovery observations are
required before retry or settlement. No timeout is a deletion or zero-cost proof.

The first phase creates and verifies only custody. The next dispatch produces a
distinct full-foundation plan, preserving custody's original creation identity.
A previous custody plan is never authorization for full-foundation apply. Recovery
can identify the original producer without an Actions artifact; consult the
bootstrap recovery instructions before retrying. Neither phase qualifies a hosted
candidate or launches a model worker.


Recovery retains a successful exact-plan completion separately from the original
creator's status and tags. When a creator failed, foundation preparation must
verify that later successful native recovery and its original human approval.
If only completion acknowledgement was interrupted after canonical state was
secured, custody reconciliation permits a freshly reviewed reads/no-ops plan
only. No imports, creation, update, deletion, retagging, financial settlement or
expiry extension is permitted by that path.

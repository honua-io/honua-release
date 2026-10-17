# Dedicated release runner admission

Owner decision: use a dedicated Linux VM/host for Actions-controlled release verification
when hosted capacity is constrained. Tracking: [release#645](https://github.com/honua-io/honua-release/issues/645).
The committed `certification/release-runners.v1.json` initially has **no approved groups**.
The policy implementation does not register a runner or qualify any candidate.

## Capacity evidence trust

The capacity gate still cryptographically verifies the bundle, signer workflow, source commit
and predicate with `gh attestation verify`. Its checker retains exact bundle SHA, source,
workflow ref, run, attempt and SLSA dependency checks. GitHub-hosted producers remain accepted.

A self-hosted producer additionally needs `tools/release_runner_policy.py` admission. That
checker fetches the exact run attempt and all jobs from GitHub, checks successful execution,
the active workflow identity and protected-trunk ancestry, then checks the producing job's
runner group against both committed policy and current organization access restrictions.
Only the capacity producer's single job is admitted. Additional jobs require a reviewed
policy change; a skipped job, fork/PR run, unapproved group, broad repository access, wildcard
workflow access, job before group approval, API failure or absent metadata fails admission.
Labels alone cannot admit a runner. The gate retains API observations and the policy digest.
The checker reads metadata itself; an uploaded JSON file cannot assert runner trust.

The capacity producer runs at `refs/tags/nightly-candidate/<full-source-sha>` or protected
trunk. Add each exact candidate workflow ref to the group's selected workflows before its
run. Wildcards, arbitrary branches and pull-request refs are not permitted. Candidate bytes
must be on protected trunk. This does not change trunk-only lock minting.

## Activation prerequisites

1. Provision a dedicated Linux host/VM, separate from shared developer worktrees. Register
   an organization group named `honua-release-heavy`, selected repository `honua-io/honua-server`,
   with workflow restrictions enabled for the exact capacity workflow refs above. GitHub's
   selected-repository and selected-workflow controls are both required. Do not use Default.
2. Run one heavy job at a time. Create a fresh disposable VM/job environment from a pinned
   base image for each job; destroy it after success, failure or cancellation. Use ephemeral
   runner registration and retain host/controller cleanup records externally. Ephemeral
   registration alone does not erase the filesystem. The runner API observation does not
   prove disk disposal: operations must qualify the lifecycle before approving the group.
3. Pin the base image digest, Actions runner version/checksum, Docker, .NET SDK/native AOT
   tools, Python, Node and GitHub CLI in the host provisioning configuration. Match repository
   SDK/global.json requirements. Reserve RAM/disk for the capacity envelope. Keep the host
   controller's resource semaphore outside job-controlled directories. Provide a separate
   restricted lightweight pool for canaries so a soak cannot starve the burn schedule.
4. Keep host administration, runner registration and AWS provisioning credentials outside
   job images. Browser, SDK and model processes receive only their application-scoped test
   context. Do not place AWS credentials in a runner-wide environment or persistent home.
5. Commit the real group ID and approval time in this repo after reviewing the group's
   lifecycle and restrictions. The entry shape is:

   ```json
   {
     "id": 123,
     "name": "honua-release-heavy",
     "repositories": ["honua-io/honua-server"],
     "workflow": ".github/workflows/capacity-soak-candidate.yml",
     "disposableJobEnvironment": true,
     "heavySlots": 1,
     "approvedAt": "2026-10-17T00:00:00Z"
   }
   ```

   These are illustrative values, not an approved identity. A capacity-only group cannot
   silently become a pool for all release workflows. Additional protected producers need
   their own reviewed access policy and identity verification.
6. Supply `RELEASE_RUNNER_READ_TOKEN` with read-only organization self-hosted-runner and
   repository Actions metadata access to the consumer. The train passes only this secret
   to the capacity gate. The job token remains the fallback for hosted producers; it does
   not gain organization administration privileges. Missing API access blocks self-hosted
   admission. Registration needs separate operator administration access.
7. Update the server producer's runner selection in a reviewed PR to the fixed approved
   group and Linux/X64/`honua-release-heavy` labels, preserving timeout, serialization,
   attestation, artifact upload and unconditional teardown. Do not accept arbitrary caller
   runner labels or route PR builds to this group. Keep hosted execution available until
   hosted/dedicated parity has passed on the same immutable candidate and frozen fixtures.

## Qualification and completion

Run the same candidate image/digests and locked workload on hosted and dedicated producers.
Retain raw workload, SLO, attestation, run/job/attempt, runner-policy and cleanup evidence.
Compare the full required populations and assertions; a contract-test pass does not count
as the capacity run. Exercise refused unapproved runner/workflow and failed cleanup cases.
Only then record pool activation and extend routing to other approved component/AWS gates.

The API currently exposes no registered runners for honua-release, and the operator token
used for implementation cannot read organization runner groups. Registration, the real group
ID, clean-host/toolchain qualification, producer routing and live parity remain open in #645.
No CI result in this PR asserts those tasks are complete. Standalone terminal runs remain
diagnostic. If Actions dispatch, identity lookup or artifact APIs are unavailable, certification
and publication stop rather than accepting a hand-written receipt.

GitHub references: [runner group restrictions](https://docs.github.com/en/rest/actions/self-hosted-runner-groups)
and [workflow job identities](https://docs.github.com/en/rest/actions/workflow-jobs).

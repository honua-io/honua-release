# Native ECS qualification evidence

The release train consumes the Redis-off ECS Fargate qualification for its frozen platform manifest. The producer contract uses the maintained Marketplace CloudFormation Launch Stack, observes the ECS service with `tools/launch_stack_observations.py`, records the installed journey and refusal receipts, and verifies deletion of its disposable stack and resources. The producer must resolve the pinned image to the ECS task platform and verify the running task digest against the frozen manifest’s digest for that CPU architecture.

The trusted producer is a successful manual run of `.github/workflows/native-ecs-qualification.yml` on this repository's trunk. Publish one artifact named `native-ecs-qualification-<identity-sha256>` after cleanup. Compute the identity as the SHA-256 of the bare manifest SHA-256 and lock digest, written on separate newline-terminated lines in that order. The evidence has this layout.

- `qualification.json` contains `schemaVersion` (1), `cell` (`aws-ecs/redis-off`), `deployment` (`cloudformation-ecs-fargate`), `status` (`pass`), string `runId` and `runAttempt`, `serverSha`, `imageDigest`, `candidateDigest`, `serverImage` (the full digest-pinned reference), `lockDigest` (empty when absent), and `journeyReportSha256` (bare SHA-256 of the report bytes).
- The envelope also contains `stackId` (the disposable CloudFormation stack ARN with a `honua-gate-` name), `installedAt`, and `installation` (the Launch Stack observation plus the producer-resolved `resolvedPlatformImageDigest`). The envelope's `cleanup` contains the same `stackId`, `stackStatus` set to `DELETE_COMPLETE`, an empty `remainingResources` list, and `observedAt`. All timestamps use UTC.
- `gate-report-cloud.json` and its installed journey and refusal receipt files follow the existing `check_journey_receipts.py` contract. Their observation times fall between install and deletion.
- For nightly model coverage, include the existing terminal-model canary receipts as `model-attempt-1.json` and, if recorded, `model-attempt-2.json`. The model receipt links to the SHA-256 of the passing deterministic journey receipt in this artifact.

GitHub supplies the run and attempt records independently of the artifact. The train retains its verification decision and the producer identity together. A missing artifact, a mismatched attempt, or a failed installation, deterministic journey, refusal, or cleanup check leaves the gate red. Model evidence has a separate advisory verdict.


Nightly dispatches the trunk producer after protocol certification binds the candidate snapshot. The train requires an explicit producer run ID and its original attempt. Standalone RC admission remains closed until it can verify the complete frozen bundle.

The producer currently freezes the candidate and retains failure diagnostics under `native-ecs-diagnostics-<run>-<attempt>`. Completing the live producer requires the approved immutable gate-bound Marketplace template URL and parameter contract in `us-east-1`, gate identity and terminal operator bootstrap, and a maintained Redis-off lifecycle harness. That harness must install the pinned image, capture the installed-journey and refusal evidence, and verify stack deletion and an empty residual resource inventory. Track admission and those dependencies in release #1546.

`tools/native_ecs_lifecycle.py` supplies the lifecycle core. The injected creator
uses the admitted template and the persisted attempt token as the CreateStack
client request token. The core checks the adapter's account and region before
creation, confirms the stack's ownership tags and ARN, and verifies the running
task's platform digest before it calls the journey. It saves the pre-delete
resource inventory so that a resumed cleanup can verify the same resources.
Replay can reconcile an interrupted attempt and verify deletion. A fresh
qualification requires a new attempt.

Live wiring still needs these inputs:

- The immutable `us-east-1` Marketplace template URL or object version and
  content digest, the `ServerImageUri` parameter, and the approved parameter
  values or template evidence that establish Redis-off. Release #1546 needs
  ownership for the template handoff.
- The admitted GitHub OIDC role and CloudFormation execution role, with the
  stage account, permissions boundary, template trust and deletion permissions.
  The s-ci-stages lane owns first-create IAM and rollback under IaC #607.
- The installed endpoint and supported terminal operator bootstrap and secret
  references for journey and refusal receipts. Release #1546 needs ownership
  for that bootstrap. The live runner needs protected durable storage for the attempt journal,
  verified ARN and pre-delete inventory.
- The certified Server commit, image index digest and Linux platform digests
  from the frozen release candidate.

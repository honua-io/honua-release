# Operate stage: product boundary

The local Docker and ECS stages share the same boundary: one model session calls
the installed server's `/mcp` operations tools, the harness approves the exact
proposal as a separate principal, and the product executor performs the approved
action. The harness owns scenario setup, fault injection, customer probes and
independent controller observations. Model text and tool results are not state
observations; locate resources using tool-result hints only after verifying them
with a fresh controller read. A fault-dependent assertion requires a fired fault
receipt. Attempts, failed actions and retry-prompt injections belong to the same
session and are recorded separately.

Setup starts on rc.1; the target release is the candidate bound by the release
gate. Do not score the stage from harness-executed deploy or restore commands.
All priced scenarios remain `not-real` until this boundary and the complete
scenario-specific observations are implemented and verified.

## Executor assessment

Source checked at honua-server trunk `013613e7136b6b90a50b505b0dd5190cb6d1e692`:

- `honua_propose_platform_release_convergence`, `honua_propose_rollback` and
  `honua_propose_deploy_operation` seal Deploy proposals in
  `src/Honua.Server/Features/Infrastructure/Monitoring/McpPlatformOpsReader.cs`
  at lines 149–182 and 246–329. The approval endpoint applies through the gateway
  (`src/Honua.Server/Features/Admin/ProposalEndpoints.cs:218–265`), and
  `DeployOperationExecutor` submits to the deploy workflow
  (`src/Honua.Server/Features/ControlPlane/Executors/DeployOperationExecutor.cs:78–104`).
- The registered self-hosted backend is `YarpRollingDeployBackend`
  (`src/Honua.Server/Startup/BatchAndDeployBackendsRegistration.cs:98–102`). It
  starts standalone replicas and requires out-of-band migrations
  (`src/Honua.Server/Features/ControlPlane/YarpRollingDeployBackend.cs:198–231,294–325`;
  `SelfHostedRollingProxy.cs:490–519`). Its rollback handles replica routing, not
  database restore (`YarpRollingDeployBackend.cs:553–618`). This does not prove
  management of the installed Compose project or restore across migration 142.
- `honua_propose_finding` seals the finding's recommended action kind and
  payload (`McpPlatformOpsReader.cs:185–200`). Admin configuration actions use
  the registered admin-config applier (`src/Honua.Server/Features/ControlPlane/Executors/AdminConfigOperationExecutor.cs:15–37`);
  those facts do not establish a general Compose restart or configuration executor.
- The local-substrate bridge test uses marker images and a separate test host and
  proxy (`tests/dotnet/Honua.CloudIntegration.Tests/LocalSubstrateAgentRollbackBridgeTests.cs:530–581`).
  It is not a qualifying installed-Compose upgrade or restore test.

The product gap and a minimal bounded executor proposal are tracked in
[honua-server #6811](https://github.com/honua-io/honua-server/issues/6811).
That issue's non-goal is any change to cloud target executors. The cloud
assessment below is separate. These are static source findings, not a completed
product-path trial.

## Cloud executors (ECS, EKS, Lambda)

The following assessment describes the historical server tree identified above; it is
not current for ECS. ECS release-image convergence merged in [#6825](https://github.com/honua-io/honua-server/pull/6825)
at `a5e593bdf6cf3d201f7721b67121ef1367d50df7`, and ECS/Lambda workload
restart and configuration merged in [#6826](https://github.com/honua-io/honua-server/pull/6826)
at `aa1ecb755c529eec49f3582120444bf5110623ac`. Database restore across migration
142 remains open in [#6823](https://github.com/honua-io/honua-server/pull/6823).
The candidate must include the relevant merged executor and its cell role must
be configured before any such action may be counted.

At the historical server tree, An approved proposal runs only when a canonical executor
matches. `ProposalEndpoints.HandleApproveProposal` returns 422
`operation-executor-unavailable` when none does
(`src/Honua.Server/Features/Admin/ProposalEndpoints.cs`, approve handler).
The approver cannot be the requester
(`src/Honua.Server/Features/ControlPlane/OperationGateway.cs`, separation of
duties on apply). Convergence and rollback both seal `OperationClass.Deploy`
(`McpPlatformOpsReader.cs:149–182` and `268–317`). That is a new forward deploy
to a prior revision for rollback, not the in-flight abort
`control-plane.deploy.rollback`. `DeployOperationExecutor` submits
`DeployWorkflowService` (`DeployOperationExecutor.cs:78–104`). The service
selects a backend by `(Backend, TargetKind)`. Nothing in `honua-server/src`
calls `RegisterTaskDefinition`, Lambda `PublishVersion`, `RestoreDBInstance`,
or an ECS force-new-deployment.

| Action | ECS | EKS | Lambda |
| --- | --- | --- | --- |
| Update to a newer release | `AwsEcsAlbDeployBackend` (`honua-aws-ecs-alb`) calls `UpdateService` on the canary with an existing task-definition ARN (`AwsEcsAlbDeployBackend.cs:471–487`, `517–537`, `610–672`). The client uses the process credential chain (`490–496`). `RequiresOutOfBandMigrations` is true. GitOps ECS only queues a hand-off and observes `ManualInterventionRequired` (`GitOpsDeployBackends.cs:1052–1112`). | No class named for EKS. Registered backends are `honua-gitops-kubernetes` (hand-off, cannot roll back) and `honua-kubernetes-argo-rollouts` (`BatchAndDeployBackendsRegistration.cs:81–102`). The native binding name `honua-kubernetes-deployment` is not registered (`honua-iac` `docs/runtime-binding.md:32–49`). | `AwsLambdaGitOpsDeployBackend` calls `UpdateAlias` only when `desiredRevision` is an already published numeric version (`AwsLambdaGitOpsDeployBackend.cs:55–62`, `100–159`; `AwsLambdaAliasClient.cs:67–90`). An image digest is rejected. Migrations are out of band. |
| Rollback | The proposal is another Deploy. In-flight `RollbackAsync` shifts ALB weight to the stable target group and does not call `UpdateService` (`AwsEcsAlbDeployBackend.cs:891–931`). Completion checks the stable service's current task definition (`941–990`). | GitOps reports `ManualInterventionRequired`. Argo can set an image when Argo is installed. Native recovery today is `helm rollback`. | Alias switch back to a published version, on the same backend. |
| Restore across migration 142 | No restore operation class, no MCP restore tool, and no database restore call. The ECS and Lambda backends require out-of-band migrations. | Same. The native workload is not installed. | Same. |
| Restart or workload config | `AdminConfigOperationExecutor` (`AdminConfigOperationExecutor.cs:15–37`) applies only `RedriveDeadLetters`, `PauseChannel`, `ResumeChannel`, and `TuneBoundedAdmission` (`OpsActionAdminConfigChangeApplier.cs:30–49`). No ECS, EKS, or Lambda restart. | Same applier. | Same applier. |

The retained controller role is assumed by the honua-release producer, not by
`DeployOperationExecutor`. Its boundary allows ECS/ALB rollout, a Lambda alias
switch, and CloudWatch reads, and denies every action when
`aws:ResourceTag/Environment` is `cert` or `demo`
(`honua-iac` `infrastructure/terraform/bootstrap/aws-update-rollback-controller/main.tf:22–62`).
The per-cell grant is `ecs:UpdateService` on the canary service only
(`infrastructure/terraform/modules/aws-ecs/deployment-safety.tf:103–128`).
A cert cell at `*.cert.demo.honua.io` is outside that grant. The controller
cannot restore a database.

Tracked product work, which this harness does not simulate:

- [honua-server #6812](https://github.com/honua-io/honua-server/issues/6812) — an approved convergence or rollback cannot place a release image on ECS or Lambda.
- [honua-server #6813](https://github.com/honua-io/honua-server/issues/6813) — no product executor restores a cloud database across migration 142.
- [honua-server #6814](https://github.com/honua-io/honua-server/issues/6814) — no governed restart or workload-config executor.
- [honua-iac #351](https://github.com/honua-io/honua-iac/issues/351) — the update/rollback controller cannot restore a database, and its boundary denies a cert or demo cell.
- EKS native workload remains [honua-iac #267](https://github.com/honua-io/honua-iac/issues/267) and [honua-devops #224](https://github.com/honua-io/honua-devops/issues/224).

The ECS stage is unwired and cannot pass even if the product gaps close.
ECS image-convergence #6825 and workload-ops #6826 are merged; this harness
still does not establish that either executor is included in the candidate or
that its cell identity is configured.
`MigrationStateReader`, `DatabaseJournal`, `ProposalCapture`, and the signed
`ecs_handoff` envelope are separately tested components. A fake test covers the
proxy, a distinct approver process, product state and controller readback. They
are not yet connected to verified live task credentials, a protected signing-key
and public-key handoff, or the real job-level approval policy. Before
wiring, run the model in a separate credential-free job with the proposer-only
MCP proxy, approve only IDs from its trusted recording ledger in a separate
approver process, and implement independent migration-state, probe, schema and
proposal-audit reads. The provision job receives neither operate key. O02 needs
service-config restore; a deploy rollback does not satisfy it.

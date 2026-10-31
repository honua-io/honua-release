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
These are static source findings, not a completed product-path trial.

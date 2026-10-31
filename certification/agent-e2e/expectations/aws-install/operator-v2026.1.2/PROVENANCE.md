# v2026.1.2 protocol recordings

Source: honua-devops tag `v2026.1.2`, commit
`2fcd96c8d80c55b69ec5f187b58b6852de1bb5dd`.
Release asset: `honua-devops-linux-x64.tar.gz`, SHA-256
`926294a5fdccf3a6cf157edbed11fc53c91c2f1b8d134e1e1d7ca948713dc680`.
The adjacent published `.sha256` was downloaded and verified before execution.

- `tools-list.json`: actual MCP `tools/list` (28 tools, complete schemas).
- `plan.json`: actual `provision_infrastructure` response. The process seam is
  offline: a local STS-only shim returns the upstream test role, a local substrate
  emits the upstream `fixtures/honua-iac/documents/exact-plan-metadata.json` shape,
  and Terraform show returns one fixture resource. The metadata digest is recomputed
  for those explicit fixture plan bytes, actor, root and timestamps. No AWS API is
  contacted and the plan remains `offline-test` / not release-qualified.
- `deploy-identity-contract.json`: the minimal exact-action contract supplied to that
  recording, matching the tagged operator's own `TerraformTestRoot` schema. This is
  a protocol fixture, not an export from the release account's deployed bootstrap.
- `install-intake.json`: actual empty-input `install_honua` response.
- `approval-dev.json`: actual shipped `--issue-provision-approval` output for the
  recorded plan, using `local-hmac-dev` in environment `dev`. It is non-evidentiary,
  expired fixture data. Tests never submit it to AWS or use it as live approval.
- `approval.schema.json`: copied byte-for-byte from the tagged operator's
  `contracts/honua-devops-provision-approval.v1.schema.json`.
- `wildcard-contract-refusal.json`: actual operator response after replacing the
  fixture's ECS actions with `ecs:Create*`, a pattern present in honua-iac v0.6.0's
  `bootstrap/aws-exec-identity/manifests/aws-ecs.json`. This reproduces the
  producer/consumer mismatch without claiming to export a deployed role.

Re-record with `tools/fixtures/agent-e2e-aws-install/record.py --binary <verified-binary>
--devops-repo <source-repo> --workdir <new-scratch-directory> --output <fixture-directory>`.
The recorder's AWS shim accepts only STS get-caller-identity; apply always exits 91.
The controller tests separately inject SDK response documents and subprocess runners.
They do not substitute operator-invented response fields or assert live install success.

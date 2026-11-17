# Retired Terraform cloud cells

The Terraform release-cell producer and its scheduled reaper are retired. The reusable
`e2e-cloud-aws.yml` and `e2e-cloud-aws-cell.yml` interfaces remain callable so existing
release callers receive an explicit failure. They report `blocked`, have no cloud
credentials and perform no provisioning. Manual `agent-e2e-aws-install` execution also
fails closed; its pull-request operator protocol tests remain available without cloud access.

Deployment evidence is assembled by the `deployment_qualification` job in
`nightly-certification.yml` using `tools/nightly_deployments.py assemble`. Retirement does
not satisfy a deployment or release gate. A caller that still requires a Terraform cell
remains blocked until its evidence contract is migrated to maintained qualification and
fresh authenticated deployment evidence passes that contract.

## Recovering existing resources

Removing a producer does not prove its resources have been deleted. The manual-only
`cloud-cell-retirement.yml` workflow remains for an authorized operator to recover a
specific retained Terraform state. It serializes recovery and refuses active legacy cloud
runs. It is neither a recurring sweep nor a replacement producer. Reconcile outstanding
resource inventory and retained state before claiming cleanup; never recreate a cell to
obtain teardown evidence.

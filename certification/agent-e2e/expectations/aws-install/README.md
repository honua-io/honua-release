# AWS install eval expectations

One case is one topology goal. The customer sentence is in `goals.json`. The agent
drives the honua-devops operator (`describe_environment`,
`analyze_customer_requirements`, `recommend_deployment_topology`,
`provision_infrastructure`, `install_handoff`, `verify_install_handoff`). The
harness owns the release-cell slot, approves the exact saved-plan digest as the
customer, and scores what it observed. It does not offer `iac_plan` or `iac_apply`.
Model prose is not evidence. The first attempt is the only attempt, and the receipt
is not replaced.

Setup installs `2026.1-rc.1`. The workflow's `lock_digest` is the later ops
candidate, not the image this stage installs.

## Product gaps (live path fails closed)

`product-gaps.json` lists what honua-devops trunk cannot do for these goals
(honua-io/honua-devops#244).
`provision_infrastructure` advertises `aws-ecs` / `size=small` only, and that
lane requires `redis_enabled=true` and has no custom-domain variable. Native EKS
live install and Lambda first-install are not qualified. There is no
`install_honua` entry tool. The live case records those gaps and does not spawn
the operator, assume a deploy role, or apply. Preflight exits non-zero while the
gaps are open and while the operator stdio bridge is disarmed, so the workflow
does not assume a role. The observer tests in
`tools/test_agent_e2e_aws_install.py` exercise approval and scoring with an
injected operator; the CLI cannot turn that mode on.

## Action manifest (interim)

`reference-actions.json` is the curated Allow list from honua-iac
`infrastructure/terraform/bootstrap/aws-ecs`, `aws-eks` and `aws-serverless` at
`d2f3919fed00535816522ab8555c71fd5ab78cdc`. NotAction ceilings and Deny
statements are not part of a deploy identity. Lane g42
(`feat/aws-topology-deploy-identity`) is adding per-module manifests. Until that
lands, this file is the reference set, and the receipt says so.

Feature slices, by service prefix, taken from those curated documents:

- `redis`: `elasticache`
- `custom_domain`: `acm` and `route53`
- `waf`: `wafv2`
- `gp_batch`: `batch`
- everything else in that compute's Allow list is `core`

A goal's manifest is `core` plus the slices it turns on. The identity on the
saved plan must equal that set. Actions outside it are not approved. A proper
subset may be approved and then fails at apply, and the trial is a model failure.

## What a pass is

The plan topology equals the goal. The plan's deploy-identity actions equal the
manifest. The arguments do not ask for administrator access, an `iam:*`
wildcard, an IAM user, or a long-lived access key. The customer approval is the
harness receipt bound to that plan's `planSha256` and `planMetadataDigest`.
Apply ran as the slot role only: STS `GetCallerIdentity` and the CloudTrail
events name that role and no other. The cell answers `/healthz/live` and
`/healthz/ready`, and an anonymous read of the service named by the verified
handoff at `https://<slot>.cert.demo.honua.io/rest/services/<service>/FeatureServer/0/query`
returns 200 with no request headers. Teardown inventory for the release-cell
run id is empty. The final answer names the observed hostname and layer id.
No secret appears in the tool results or the final answer.

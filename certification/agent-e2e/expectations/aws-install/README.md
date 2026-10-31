# AWS install evaluation

The model drives the shipped honua-devops v2026.1.2 MCP binary. The workflow downloads
`honua-devops-linux-x64.tar.gz` and its `.sha256`, verifies that digest and the harness's
immutable archive digest, and checks out honua-iac at the platform manifest revision
(v0.6.0 on this branch). `install_honua` is the entry tool; the six installation and
advisory tools remain available with their actual schemas. The full operator catalog
contains 28 tools.

The harness reads the pre-existing `bootstrap/aws-exec-identity` Terraform outputs.
`deploy_identity_contract.actions_by_service` is the goal topology's least-privilege
reference, `role_arn` is the only accepted deployment principal, and SHA-256 over the
export's exact JSON bytes is the contract digest. The harness never curates another
Allow set or creates the identity bootstrap. The three closed ECS gaps have been
removed; EKS and Lambda first-install qualification remain blocked by honua-devops#244.

The workflow's provision OIDC session stays in the controller. It assumes the exported
deployment role for the operator and a distinct bootstrap-listed signer role for the
shipped `--issue-provision-approval` command. Live approval uses `kms-mac` with the
bootstrap key ARN: the signer has GenerateMac, the operator has VerifyMac. The signed
receipt binds the plan and metadata digest, operation, stack, environment and action.
The controller records topology, account/region, slot hostname/prefix and contract
digest alongside that receipt. Apply requires byte-for-value equality of the complete
receipt, unchanged inputs and explicit confirmation. Unsafe IAM grants, IAM-user and
long-lived-key requests are recorded before manifest comparison and never approved.

The controller reads STS and CloudTrail independently of operator output. Health probes
use the cell hostname and send no authentication headers or redirects; the installed
image is read from ECS task definitions. The shipped handoff verification proves health,
authentication and the MCP roster; it does not publish a GIS layer, so this setup stage
does not invent a layer id. The final answer must name the verified cell hostname.

The slot handle is persisted before the model runs. The controller uses its original
provision/reaper credentials for `terraform destroy`, with the retained remote backend
and controller variable file, then queries live AWS inventory by both `validationRunId`
and `name_prefix` tags (plus IAM, which the tagging API omits). The workflow runs an
additional `always()` teardown before uploading the redacted receipt. Raw stderr,
Terraform inputs/state and subscription authentication are never uploaded. Integrity
checks run before and after every operator call, including both handoff tools, and at
finish; teardown still runs when integrity fails.

## Release-session configuration

Only a trunk dispatch in `aws-cell-provision` is permitted. This lane does not dispatch
or merge. Configure the existing bootstrap backend with
`HONUA_AWS_INSTALL_IDENTITY_BACKEND_HCL`; configure the cell with
`HONUA_AWS_CELL_STATE_BUCKET`, `HONUA_AWS_CELL_BACKEND_ROLE_ARN` and
`HONUA_AWS_CELL_ROUTE53_ZONE_ID`. The bootstrap must match the selected topology, trust
the provision session to assume its deployment/signer roles, and permit the run's
resource prefix. `HONUA_AWS_INSTALL_OPERATOR_CONTRACT_IDENTITY` supplies the real,
release-owned operator-contract identity pins for the rc.1 install; image and IaC pins
must agree with this checkout. No state lineage or release qualification is fabricated.
CLI versions use `HONUA_CLAUDE_CLI_VERSION` / `HONUA_CODEX_CLI_VERSION`; subscription
authentication uses `CLAUDE_CODE_OAUTH_TOKEN` / `HONUA_CODEX_AUTH_JSON` secrets. No
placeholder AWS credential is used. The optional lock run id is format-checked; this
setup stage records the later ops candidate's lock digest without claiming to attest it.

## Recorded protocol fixtures and remaining producer mismatch

See [operator-v2026.1.2/PROVENANCE.md](operator-v2026.1.2/PROVENANCE.md). The catalog,
plan, intake and development approval were captured from the digest-verified release
binary using an offline substrate fixture. These are schema/protocol evidence only,
never proof of an AWS deployment. The optional binary test compares all 28 schemas and
runs the shipped development issuer with a fresh throwaway key in environment `dev`.

An additional real refusal is retained: v0.6.0 exports wildcard actions such as
`ecs:Create*`, while v2026.1.2's `DeployIdentityContract.Load` accepts only exact action
names. The operator returns `deploy-identity-contract-required` for that contract. The
harness preserves the exported allow-list and records this as an infrastructure
refusal; it cannot repair the producer mismatch by rewriting the contract. A successful
live ECS rehearsal remains a release-session gate after that mismatch is resolved.

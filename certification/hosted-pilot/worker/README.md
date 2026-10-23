This root plans and applies one fixed worker attempt. The campaign root owns the
existing cluster, private network, retained accounting store and controller.
No worker, image builder or readiness check initializes another allowance.

Use the checked `hosted-pilot-worker-image.yml` workflow first. Its fixed reviewed
`certification/agent-e2e/worker-image.json` has `schemaVersion`, one A/B `caseId`,
`pack: {path, sha256}`, and the exact public OKF `knowledge` binding. The builder
checks out that source without executing it, exports only the selected public
files and reviewed runtime allowlist, runs the actual container controls, then
publishes to the existing foundation ECR repository. Its original immutable
pilot owner reservation is consumed once before publication. The receipt gives
the actual image and public-bundle digests; a source SHA or planned tag is not a
substitute. Held-out C inputs, controller code, oracle files and credentials do
not enter the build context.

Before preparing the MCP secret, review the exact `worker_attempt_ids` into the
campaign root and use its ordinary saved-plan workflow. That creates each
attempt's private MCP destination and admits only its lifecycle role to retained
launch claims. The independent expiry role has no DynamoDB writes. The private
human-context preparation must write the agent-only payload directly to that
actual destination and publish its immutable version metadata. A candidate
staging secret or another attempt's version cannot authorize this worker.

Review one `deployments/*.tfvars.json` with the observed image digest,
`public_bundle_digest`, original `candidate_digest`, unique `attempt_id`, fixed
`issued_epoch`, integer `lifetime_seconds` at most 6000, and matching absolute
`expires_at`. Bind the real controller role, campaign cluster/private worker
network, accounting claim table, permissions boundary, and a real cleanup SNS
topic with a confirmed monitor. `mcp_configuration` contains only the actual
`secret_arn`, `version_id` and optional KMS reference, never credential bytes.

The `accounting` object binds the original canonical state namespace,
`generationDigest`, authenticated `bootstrapArtifactDigest`, distinct original
infrastructure/cleanup reservation IDs and their `maximumUsd` ceilings. Its
`operationId` is `worker-<attempt_id>`, purpose `shared-environment`, and
`liabilityBasisDigest = worker_allowance.basis(config)`. That basis binds every
configuration field except the accounting envelope, including the actual image,
MCP version, network, identity and fixed epoch. Original object versions and
reservation state are rechecked before plan and apply. Missing history,
settled cleanup, substituted namespaces and a changed runtime cannot borrow the
reservation. The pilot owner budget remains independently required too.

Choose `certification/hosted-pilot/worker` in `hosted-pilot-deploy.yml`. The backend
is isolated at `workers/<attempt_id>/terraform.tfstate`, and apply uses the exact
saved plan. Readiness independently observes registered ECS/IAM/network,
immutable registry and secret identities, actual worker/expiry definitions,
retained claims, cleanup schedule/DLQ and confirmed alarm subscription. It does
not launch a worker or claim scenario qualification. Review the resulting
`worker_backend` into the checked backend attempt configuration before the
ordinary campaign freeze. Genuine model actions, native product observations,
worker termination and cleanup evidence are still mandatory.

The published worker module has its own legacy AWS provider configuration.
Wrapper tests therefore override that child and test wrapper boundaries only.
CI also tests the actual pinned module from its own root with its provider
mocked, using the reviewed Linux provider lock and readonly init. Those controls
include exact secret/version scopes, queue-only worker authority, fixed lifetime,
candidate fencing, independent cleanup and permission boundaries. Neither set
is a replacement for actual hosted execution.

# Release 619 delivery plan

Tracking: https://github.com/honua-io/honua-release/issues/619.

Work-package tickets (all in honua-release): [#620](https://github.com/honua-io/honua-release/issues/620)
baseline, [#621](https://github.com/honua-io/honua-release/issues/621) container infrastructure,
[#622](https://github.com/honua-io/honua-release/issues/622) durable backend,
[#623](https://github.com/honua-io/honua-release/issues/623) scenarios,
[#624](https://github.com/honua-io/honua-release/issues/624) semantic verification,
[#625](https://github.com/honua-io/honua-release/issues/625) knowledge/discovery,
[#626](https://github.com/honua-io/honua-release/issues/626) telemetry,
[#627](https://github.com/honua-io/honua-release/issues/627) producer/qualification,
and [#628](https://github.com/honua-io/honua-release/issues/628) release activation.

## Accepted architecture

- Terraform in honua-iac owns all infrastructure. Python runs tests, runtime jobs,
  observations, telemetry and accounting; it is not an infrastructure provisioner.
- Cloud execution workers are disposable ECS Fargate containers. Local Docker
  uses a dedicated CI runner with agent and Honua containers, separate from the
  trusted controller runner. Do not add standalone EC2 workers or SSM execution.
- Retain existing EKS managed-node-group container deployments. No EKS migration.
- Terraform defines container jobs, networking, IAM, per-attempt queues, state,
  lifecycle orchestration and independent cleanup. Runtime APIs start/stop these
  predefined jobs. Worker output is untrusted; independent probes decide success.
- Match the terminal canary model; currently its fallback is
  `us.anthropic.claude-sonnet-5-5` (operator ruling 2026-10-22) via `us-east-1`. Verify the requested
  and returned identities and pricing before freezing any campaign.

## Work packages and dependencies

1. Correct the baseline and issue claims; remove the rejected EC2 implementation.
2. Build the common container command transport and pinned worker image; provision
   Fargate execution and the Docker lane, isolated IAM/network/storage and cleanup
   through Terraform. Prove duplicate delivery, deadlines and controller-loss cleanup.
3. Complete the real controller backend, durable attempt/receipt registry and
   immutable evidence. Prove lease fencing, recovery and candidate-wide accounting,
   including bootstrap costs. Never reset history to regain spending allowance.
4. Implement every scenario's setup, seeding, observed runtime-ID binding,
   credentials, approvals, render/permission probes, fault injection and cleanup.
   Model actions remain model-selected, not scripted solutions to the prompts.
5. Complete fixtures and real API recipes for all 15 semantic checks on every
   target, with raw response bindings and protected-content snapshots. Refusals
   require the exact reviewed operation/status/error contract.
6. Align published OKF concepts, GIS workflows and examples with runtime discovery.
   Track server #3428 separately. Pin the actual delivered knowledge bundle and
   interface schemas; do not publish private assertions or held-out prompts.
7. Connect real traces, logs, metrics, durable artifacts, dashboards and alerts.
   Demonstrate evidence-supported failure diagnosis and missing-telemetry rejection.
8. Land the trusted campaign producer before activating release consumers. Run
   bounded resumable development shards, freeze all inputs, then qualify the whole
   matrix. Attest the complete ledger, registry, receipts and raw evidence.
9. Verify consumer rejection paths, activate release requirements, merge through
   existing gates and publish the operating/recovery guide. Close only after the
   complete live evidence audit passes.

Packages 2–3 precede paid agent execution. Complete Docker first, then ECS, EKS,
Lambda and Batch. Documentation and telemetry develop alongside the first real
vertical slice. Keep the consumer requirement draft until the producer and actual
qualifying evidence exist; split PR #607 as needed to land foundations first.

## Invariants and acceptance

- Preserve 36 scenarios, 108 prompts and 252 live cases: Docker 72, ECS Redis-on
  72, EKS 72, Lambda 18, Batch 18. Fifteen independent semantic checks per target
  are additional requirements, never substitutes for these cases.
- Development uses A/B only. C is held out for frozen qualification. Bind exact
  candidate/model/prices/images/IaC/docs/interfaces/fixtures/recipes/expectations.
- All development, failures, retries, infrastructure, model, telemetry and cleanup
  share the $500 candidate ceiling, with $50 protected for cleanup. Start with one
  active attempt and one target environment. Reserve before spending; unresolved
  liability remains reserved. Stop paid execution if full coverage is unaffordable.
- P01/P24 verify installation, revoke bootstrap authority, prove revoked access
  fails and hand off to a fresh unprivileged container. Approvers remain separate.
- P21 loses the response after map commit; L04 injects a query 503 after observed
  cold start; B05 stops after output commit before acknowledgement, then redelivers.
- Batch requires real AWS job IDs and the native ETL worker where specified. B03
  uses all 330,000 rows / 85.7 MB. No local/small/refusal substitute is qualifying.
- Retain first failures; only independently evidenced infrastructure failure may
  authorize the existing bounded retry. Unknown causes cannot be retried to green.
- Keep `honua.gis-workflow/v1` descriptive. No new interpreter or permission system.
  Execute published MCP, HTTP, CLI and Honua Spec interfaces.
- Observe cleanup completion and actual liability independently. Crash recovery,
  independent expiry cleanup, full telemetry and authenticated producer/consumer
  evidence must be demonstrated live before completion.

Desktop-client work, broad raster/3D/performance certification and EKS migration
remain excluded. Offline tests never qualify live cases.

# M1 developer-preview dogfood runbook

**developer preview; certification in progress**. This is a feasibility recording on
real AWS ECS, not a certification receipt. The Console is display-and-approve;
no dashboards are claimed. Studio, mixed topology and EKS are **Preview**.
`customer-install-manifest.json` stays `pre-cut-rehearsal`.

The program is [honua-release#376](https://github.com/honua-io/honua-release/issues/376),
canonical in [Specifica](https://github.com/honua-io/agent-delivery-spec/tree/trunk/.specifica/2026-1-release-plan-ai-cloud-to-maps/).
R18 means continuous certification on trunk: a nightly strict train on trunk head
mints a signed lock only when all gates pass. There is no freeze or hand re-pin;
gates can fail and cannot be overridden. Do not edit release pins for this run.

## Execution boundary and recording

Start from a fresh terminal with a genuine Claude session and installed Honua
CLI/SDK/MCP tools. The operator supplies credentials privately and approves each
mutation. This document does not authorize a documentation lane to write to AWS,
change IAM, or change secrets or variables. Operator commands below are **not
verified AWS observations** until the operator executes them and attaches evidence.

The committed [agent-map harness](../e2e/agent-map/README.md) currently composes a
browser map over the public Maui demo using a direct Anthropic Bedrock SDK loop.
It cannot install ECS, publish a durable map, roll back, or tear down, and its
sources are hardcoded to the demo. Do not pass an ECS URL to it and claim that it
rendered the new layer. The recorded AWS journey must use the installed server's
StudioAi Bedrock adapter and discovered CLI/MCP operations; missing operations
are blockers under #377/#381. A direct-provider harness run is separate evidence,
not proof of that adapter or of a complete M1 run.

Use synthetic data only. Capture prompts, model responses and selected tool names
and outcomes through an allowlist recorder. Keep a private, access-restricted
working directory (mode 0700); publish only reviewed, sanitized copies. Do not
record credential setup, shell environment dumps, raw Terraform plans/state,
raw tool results or browser network logs. **No secrets, DSNs, presigned URLs or
customer-identifying data** may enter the transcript, receipts, screenshots,
issue attachments or PR. Redact account/principal/resource identifiers where
needed; preserve digests and stable synthetic IDs. A final map URL must be an
ordinary HTTPS URL with no credential or signature query parameters.

Every step gets `executed/pass`, `executed/fail`, or `not-executed/<reason>` with
UTC start/end, source SHA, tool/package versions, sanitized invocation and
observable outcome. Record failures and retries as well as successes. Do not
call model-free probes or policy fixtures a genuine-model run.

## 1. Fresh terminal and prerequisites (operator, read-only)

Create a new worktree from the remote trunk, never a shared checkout:

```bash
git fetch origin trunk
git worktree add --detach ../dogfood-m1 origin/trunk
cd ../dogfood-m1
node --version
python3 --version
aws --version
docker version
npx -y -p @honua/sdk-js honua --help
node e2e/agent-map/mcp-call.mjs https://demo.honua.io honua_list_sources '{}'
```

Expected: Node meets the app's >=20.19.0 requirement; Python, AWS CLI and Docker
are available, the published CLI advertises its command surface, and the MCP
call initializes and lists public-demo sources. This read-only demo discovery
is not evidence of the AWS cell. Record registry
versions/integrities in the operator receipt. Preview floating package discovery
is not exact-lock certification. Supply the installed tool schemas to Claude;
do not invent command flags from prose.

Set these **non-secret** inputs to operator-provided values in the private terminal:
`AWS_REGION`, `AWS_PROFILE` (short-lived provision identity), `REAPER_PROFILE`,
`AUDIT_PROFILE` (read-only IAM/budget inventory), `ACCOUNT_ID`, `CELL_ROLE_ARNS`
(space-separated provision/reaper/mirror roles and workload roles), `TASK_ROLE_ARN`,
`MODEL_ARN`, `OTHER_MODEL_ARN`, `BUDGET_NAME`, `BUDGET_TOPIC_ARN`,
`CURRENT_IMAGE`, `PRIOR_IMAGE`, `ECR_REPOSITORY`, `CURRENT_DIGEST`, `PRIOR_DIGEST`,
`RUN_ID` (unique `gha-RUN_ID-aws-ecs`), and `OUTSIDE_REGION`.
Use `Owner=release-cell`, `ValidationRunId=$RUN_ID`, an ephemeral
`Environment=test`, and the approved `honuar*` namespace for every run resource.
Standing resources use `Owner=release-standing`, `Lifecycle=standing` and
`Environment=cert`; the demo stays protected. No bootstrap/apply instructions
for IAM are part of this runbook.

### Identity, least privilege and teardown deny (#208)

```bash
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}'
aws --profile "$REAPER_PROFILE" sts get-caller-identity --query '{Account:Account,Arn:Arn}'
for role in $CELL_ROLE_ARNS; do
  aws --profile "$AUDIT_PROFILE" iam get-role --role-name "${role##*/}" \
    --query 'Role.{Arn:Arn,Boundary:PermissionsBoundary}'
  aws --profile "$AUDIT_PROFILE" iam list-attached-role-policies --role-name "${role##*/}"
  aws --profile "$AUDIT_PROFILE" iam list-role-policies --role-name "${role##*/}"
  for protected in standing demo; do
    aws --profile "$AUDIT_PROFILE" iam simulate-principal-policy \
      --policy-source-arn "$role" \
      --action-names ecs:DeleteService ecs:UpdateService \
      --resource-arns "arn:aws:ecs:$AWS_REGION:$ACCOUNT_ID:service/honuarprotected/honuarprotected" \
      --context-entries "ContextKeyName=aws:RequestedRegion,ContextKeyValues=$AWS_REGION,ContextKeyType=string" \
        "ContextKeyName=aws:ResourceTag/Environment,ContextKeyValues=$protected,ContextKeyType=string" \
        "ContextKeyName=aws:ResourceTag/Lifecycle,ContextKeyValues=$protected,ContextKeyType=string" \
      --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision,Missing:MissingContextValues}'
  done
  aws --profile "$AUDIT_PROFILE" iam simulate-principal-policy \
    --policy-source-arn "$role" --action-names ecs:UpdateService \
    --resource-arns "arn:aws:ecs:$OUTSIDE_REGION:$ACCOUNT_ID:service/honuarfixture/honuarfixture" \
    --context-entries "ContextKeyName=aws:RequestedRegion,ContextKeyValues=$OUTSIDE_REGION,ContextKeyType=string" \
    --query 'EvaluationResults[].{Decision:EvalDecision,Missing:MissingContextValues}'
done
```

Expected: correct account, STS assumed roles, separate provision/mirror/reaper
identities; workload boundary present; every protected mutation and outside-region
case is `explicitDeny` without missing context. Audit the attached policy versions
against the deployed #208 contract, including forbidden role chaining and safety-tag
removal. Simulations are read-only policy evidence, not live destructive tests.
Require #208's operator activation/negative-test receipt for actual standing/demo
resources and all reachable identities before continuing. An open issue, committed
policy fixture, or broad PowerUserAccess role is insufficient.

### Bedrock and images (#207)

```bash
aws --profile "$AUDIT_PROFILE" iam simulate-principal-policy \
  --policy-source-arn "$TASK_ROLE_ARN" --action-names bedrock:InvokeModel \
  --resource-arns "$MODEL_ARN" "$OTHER_MODEL_ARN" \
  --context-entries "ContextKeyName=aws:RequestedRegion,ContextKeyValues=$AWS_REGION,ContextKeyType=string" \
  --query 'EvaluationResults[].{Model:EvalResourceName,Decision:EvalDecision,Missing:MissingContextValues}'
python3 - <<'PY'
import os, re
current, prior = (os.environ[n] for n in ('CURRENT_IMAGE', 'PRIOR_IMAGE'))
assert current != prior
for image in (current, prior):
    assert re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', image), 'immutable digest required'
print('PASS: two distinct digest-pinned revisions')
PY
aws ecr describe-images --repository-name "$ECR_REPOSITORY" \
  --image-ids imageDigest="$CURRENT_DIGEST" imageDigest="$PRIOR_DIGEST" \
  --query 'imageDetails[].{Digest:imageDigest,MediaType:imageManifestMediaType}'
```

Expected: approved model allowed, unrelated model denied, no missing context;
two distinct digests exist and match the suffixes of CURRENT_IMAGE/PRIOR_IMAGE.
Require the #207 operator receipt for ECS StudioAi configuration, exact model
access and a negative invocation through that adapter. IAM simulation alone
cannot prove Bedrock entitlement, inference-profile destinations or application
configuration. Use the exact approved model, not the harness's default model.
Do not mirror/push images with the provision or reaper identity.

### Budget, alert subscription and spend ceiling

```bash
aws --profile "$AUDIT_PROFILE" budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME"
aws --profile "$AUDIT_PROFILE" budgets describe-notifications-for-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME"
aws --profile "$AUDIT_PROFILE" sns list-subscriptions-by-topic --region "$AWS_REGION" --topic-arn "$BUDGET_TOPIC_ARN" \
  --query 'Subscriptions[].{Arn:SubscriptionArn,Protocol:Protocol}'
```

Expected: actual-spend absolute USD thresholds at **$100 and $200**, active SNS
subscribers with confirmed subscription ARNs (never PendingConfirmation), and
a current budget read. These account alerts are not the per-run ceiling.

**Per-run ceiling: USD $25 total, including Bedrock, compute, storage, networking,
logs and teardown; maximum two hours from first provisioning mutation.** Reserve
$5 of that ceiling for teardown. Stop new work at $20 or 90 minutes, on alert, or
when a trustworthy cost bound is unavailable. Teardown still proceeds safely if
cost is exceeded; mark the run failed. Budgets/Cost Explorer lag and do not cap
spend. Before install, have Claude produce this exact cost prompt:

> Using current regional AWS prices and the approved resource plan, compute an
> upper bound for two hours plus teardown and retained-resource charges. Include
> Bedrock input/output tokens for every round, provisioner Claude usage, NAT,
> ALB, RDS, ECS, S3, logs and secrets. Give the price source/time, quantities and
> USD arithmetic. Reserve $5 for teardown within $25. Refuse provisioning if
> the bound exceeds $25 or any price/quantity is unknown. Require approval of
> this bound together with the plan digest.

Expected: reviewed, timestamped bound <=$25. Keep a local running cost ledger
from all model usage and resource lifetimes; the current harness reports only the
last round's usage, so it is insufficient for cumulative model costing.

## 2. Install into the isolated ECS cell (exact Claude prompt)

Supply the checked non-secret inputs and approved cost bound to the terminal
Claude session using its installed CLI/SDK/MCP tool inventory:

> Install Honua into a new isolated AWS ECS cell in the supplied account/region,
> Redis off, using CURRENT_IMAGE and the approved honuar namespace. Every created
> resource must be inventoried with Owner=release-cell and ValidationRunId=RUN_ID;
> include service-specific resources that cannot be tagged. Use the existing
> bounded roles and workload boundary; make no IAM or credential changes. Use
> the governed provisioning plan and apply operations. Show the exact plan digest,
> remote-state identity, resource inventory and cost bound; stop for the operator
> to approve that digest. Apply only the approved saved plan. Verify HTTPS,
> server revision, readiness, anonymous admin refusal and authenticated MCP
> tools/list. Verify Claude reaches the exact approved Bedrock model through the
> installed server's StudioAi adapter and an unrelated model is denied. Return
> sanitized plan/apply/handoff/model receipts. If a required operation is absent
> or denied, record a blocker and do not substitute a fabricated call or demo URL.

Approval prompt (operator supplies the actual digest):

> I approve only plan digest <PLAN_SHA256>, the supplied account/region/run ID
> and cost bound. Execute that saved plan through the governed apply operation.

Expected: approved apply completed, HTTPS cell URL, exact CURRENT_IMAGE revision,
healthy ECS tasks, authenticated discovery, anonymous admin denied, successful
server-mediated Bedrock call and denied other-model call. Record ECS cluster,
service, task definition, plan/operation digests, and the full private inventory.

Read back using operator-supplied `CLUSTER`, `SERVICE` and `TASK_DEFINITION`:

```bash
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"
aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[].{TaskDefinition:taskDefinition,Desired:desiredCount,Running:runningCount,Pending:pendingCount}'
aws ecs describe-task-definition --task-definition "$TASK_DEFINITION" \
  --query 'taskDefinition.containerDefinitions[].image'
```

Expected: desired=running, pending=0, service's active task definition is the
one inspected and the Honua container image equals CURRENT_IMAGE. Also read
running tasks' `imageDigest` via the discovered ECS SDK/CLI inventory and match
the actual pulled digest (including platform-child digest for multiarch images).

## 3. Publish a layer and styled 2D map (exact Claude prompt)

> Through this new cell's discovered Honua CLI/SDK/MCP operations, import a
> synthetic GeoJSON FeatureCollection containing two points at [-156.47,20.89]
> and [-156.45,20.75], with properties name=A/class=1 and name=B/class=2.
> Publish a layer named dogfood-RUN_ID. Query it back and prove count=2 and the
> same coordinates/properties. Create a 2D map with class 1 blue and class 2 orange,
> a legend and a Maui extent. Render and capture a screenshot showing both styled
> points. Save, close and reopen the map; read back persisted layer/style IDs.
> Propose publication and wait for the operator's separate approval principal.
> Return the proposal digest and, after approval, the ordinary final HTTPS map
> URL. Verify that URL in a fresh unauthenticated browser session if publication
> is public. Do not render the public demo as a substitute. Stop if durable save,
> publish, approval or read-back is unavailable.

> I approve publication proposal <PROPOSAL_SHA256> as the separate approval
> principal. Publish only that proposal and verify the resulting map URL.

Expected: imported count=2, persisted styled map, proposal/approval/publication
receipts, visible blue/orange points and legend after reopening, final URL
verified before teardown. The Console may display and approve the proposal;
it does not author it. Record the map URL and sanitized screenshot now: the URL
will cease serving when the ephemeral cell is removed.

## 4. Roll back to the prior image (exact Claude prompt)

> Propose rollback of only this run's ECS service from CURRENT_IMAGE to PRIOR_IMAGE
> through deploy-preflight and deploy-rollback using the installed AwsEcsAlbDeployBackend.
> Show preflight, migration/schema compatibility and target digest; wait for
> operator approval. Refuse incompatible targets cleanly. After approval, execute
> the rollback, wait for convergence, and query the same published layer (count=2,
> unchanged data) and reopen the same styled map URL. Return sanitized operation,
> image/revision, serving and persistence receipts. Do not silently re-import data
> or recreate the map to make rollback pass.

> I approve rollback proposal <ROLLBACK_SHA256> to PRIOR_IMAGE for this run only.

Repeat the ECS read-back commands in step 2 with the new active task definition.
Expected: prior digest actually running, healthy service, same layer/map/style
readable. A compatibility refusal is a recorded failure/blocker, not a completed
rollback. M1 is not M3's signed-lock update, in-flight GP or exactly-once proof.

## 5. Read spend before teardown; approve tagged teardown

Immediately before any teardown, run the budget reads from step 1 again and this
prompt even if an earlier step failed:

> Read the running cost ledger now. Report UTC elapsed time, cumulative Bedrock
> tokens/cost, resource lifetimes/cost, current budget actual/forecast and their
> freshness, total incurred plus bounded remaining teardown/retention cost, and
> comparison with $25. Record the pre-teardown spend receipt. If over ceiling or
> unknowable, mark the run failed and still prepare safe teardown. Enumerate only
> this run's inventory and tags, verify standing/demo exclusions and the deny,
> then propose governed teardown with an exact plan digest. Do not execute yet.

> I approve teardown plan <DESTROY_PLAN_SHA256> for Owner=release-cell and
> ValidationRunId=RUN_ID only, including the explicitly inventoried untaggable
> dependents. Use the separate short-lived reaper identity. Destroy through the
> governed operation and return its durable receipt. No standing/demo mutation,
> no policy changes, no tag removal to evade a deny.

Expected: spend read timestamp precedes teardown; approved digest and durable
teardown receipt. Approval cannot override a failed policy/preflight gate.

## 6. Prove nothing was left behind (read-only verification)

```bash
aws --profile "$AUDIT_PROFILE" resourcegroupstaggingapi get-resources \
  --region "$AWS_REGION" --tag-filters "Key=Owner,Values=release-cell" "Key=ValidationRunId,Values=$RUN_ID" \
  --query 'ResourceTagMappingList[].ResourceARN'
```

Expected: no **live** run resources. This API can return historical tags and
omit unsupported/untagged resources; an empty list alone is not proof. Use this
exact prompt for exhaustive inventory reconciliation:

> With the read-only audit identity, reconcile every pre-apply, post-apply and
> teardown inventory entry against its AWS service's describe/list API, including
> untaggable dependents. Verify ECS tasks/services/clusters and retained task
> definitions, ALB/listeners/target groups, RDS instances/snapshots, ENIs, VPC,
> subnets/routes/security groups/NAT/EIPs, Redis if created, logs, S3 objects and
> buckets, run-specific ECR artifacts, secrets (including recovery windows), KMS
> keys (including pending deletion), autoscaling targets, DNS and any jobs or
> alarms created by the run. Drain every paginated response; repeat after eventual
> consistency settles. Explain any historical tag entry with a service read-back.
> Verify standing/demo inventory unchanged. Report leftovers as failures, with
> IDs privately and sanitized counts publicly; schedule approved cleanup through
> the operator, never widen IAM. Record zero live/retained run resources only when
> all entries reconcile. Shared pre-existing dependencies are baseline inventory,
> never deleted. Record any delayed deletion as a leftover until completed.

Expected: zero run-owned live or retained resources, no pending deletion charges,
standing/demo unchanged. Do not claim “nothing left behind” while retained task
definitions, snapshots, secrets, keys or orphaned untagged resources remain.
After billing data settles, rerun the budget read and reconcile actual charges
against the ledger; final spend <=$25 is a separate recorded criterion.

## 7. Attach the evidence and audit the claims (exact operator prompt)

> Review the sanitized transcript, receipts and screenshots against the redaction
> rule. Attach to honua-release#376: all attempts, source/package/model identities,
> prerequisite/deny evidence, approved digests, install/model/layer/map/rollback
> observations, final map URL and its pre-teardown verification time, pre-teardown
> spend, teardown inventory reconciliation and settled final cost. Mark every
> unexecuted step and why. Audit site/blog/release notes/support claims against
> these observations: developer preview; certification in progress; Console
> display-and-approve; no dashboards; Studio/mixed topology/EKS Preview;
> best-effort preview support; install manifest pre-cut-rehearsal. Record the
> audit URLs/revisions and verdict. Do not close #378 until every criterion passes.

## Local rehearsal (no AWS credentials)

From the fresh release worktree, execute:

```bash
docker version
npm_config_cache=/tmp/dogfood-npm-cache python3 certification/terminal-journey/run.py --mode live \
  --target certification/terminal-journey/targets/local-docker.json \
  --workdir /tmp/dogfood-local-clients \
  --output /tmp/dogfood-local-receipt.json \
  --evidence-uri file:///tmp/dogfood-local-receipt.json
cd e2e/agent-map
npm_config_cache=/tmp/dogfood-npm-cache npm install
npm --prefix app run typecheck
npm --prefix app run build
npm start
```

Expected: live driver consumes published pins, starts the manifest-pinned local
Docker stack, probes readiness/identity/admin refusal/MCP and tears down. Read
its per-stage receipt: a probe pass is not a stage pass. Typecheck/build succeed;
`npm start` advertises loopback control and Vite URLs. Stop it with Ctrl-C.
Without AWS credentials do not submit a model prompt. The app also requires the
public demo/basemap network and a browser; it has no offline local-server mode.

Record actual outputs in the PR. Local Docker cannot verify AWS IAM, SNS,
Bedrock/StudioAi, ECR inputs, billing, ECS install/rollback or AWS teardown.
The current deterministic driver and browser harness do not implement the
complete publish/save/approve journey; preserve any blocked outcomes. The
recorded real-AWS run and public claims audit remain operator work.

Refs #378 (released: the recorded AWS run and the claims audit need the operator)

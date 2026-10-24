# Governed hosted pilot

This root configures an isolated Cognito-authenticated Console/Server/embedded
Studio deployment. It does not establish a passing human session, agent campaign
or FOSS4G demonstration. Native inference and Server Bedrock remain disabled;
operations policy remains default-deny. It never changes the public demo.

All creation, configuration and teardown use these versioned roots and workflows.
Read-only inspection is permitted. Do not run local Terraform apply or make AWS
CLI/Console mutations. Nothing in this change applies or bootstraps resources.

## Deployment and authentication

The Server module is pinned to reviewed `honua-iac` commit
`1d9b3f40e1c481cbae0a61e4c7f88d1621f06595` (PR284). The root reuses its VPC
module version 5.21.0 at the parent level so Server ingress can allow the exact
single NAT address without creating a dependency cycle. Tasks have private IPs.
Console reaches Server through that NAT over certificate-validated HTTPS. Only
explicit operator CIDRs and that NAT /32 enter the public HTTPS ALB; HTTP ingress
and public task addresses are disabled.

This root issues a DNS-validated ACM certificate for both dedicated hostnames,
manages validation and CNAME records in an existing delegated Route53 zone, and
installs exact host rules. Unknown hosts receive 404. Choose two unused names
inside that zone; neither the zone apex nor an existing non-wildcard certificate
can substitute for these bindings. Account IDs and actual ARN bindings belong
in reviewed deployment configuration, not public documentation.

Console listens on loopback port 8081. The ALB targets only its oauth2-proxy
sidecar on 8080. The default proxy is official v7.15.5 Alpine, pinned to
`sha256:8e4bec4f9fe4de10de63070209204075e4407503170aa19893d6e8d99f083d1b`.
Its actual structured configuration/entrypoint is tested against that image.
Changing this digest requires checking its structured schema and security fixes.
The sidecar validates issuer, audience, nonce, verified email and PKCE S256,
requires membership in the isolated author or approver group, and replaces
incoming subject/email/trust headers. It strips incoming Authorization,
X-API-Key and X-Forwarded-Access-Token. It injects no Cognito bearer into Console.
Separate generated edge cookie/trust secrets enter only through Secrets Manager.
The Console execution role can read its own edge client secret, never the BFF
client or Server signing secret; all workload roles carry the bootstrap boundary.

Both confidential clients use code-only OAuth and minimal `openid profile email`
scopes. The BFF callback is exactly Console `/admin/auth/callback`; Server API
`Public:BaseUrl` stays on the Server origin. This requires Server PR6518's
`Oidc:AdminAuthCallbackUri` fix. The Console image must also implement
`Honua:Console:Auth:EdgeForwarded:ServerSessionIssuer`, set here to the exact
Cognito issuer, checking Server session issuer/subject before bearer issuance
and refresh. An older image that ignores that setting is not qualified.
The edge identity is raw Cognito `sub`, never an email or forwarded role.
State/profile/server binding alone does not prove same-subject BFF identity.

The pool disables public registration and self-service account recovery. Human
enrollment defaults to null. Explicit, sealed nominations create distinct
author/approver accounts and group memberships through the same saved-plan
workflow; see [human enrollment](HUMAN-ENROLLMENT.md). No invitations are sent.
Application grants, the independently scoped agent, and actual human sign-in
and approval evidence remain separate requirements.
The two configured Server admin-role slots contain only the approver group.
The Server receives `Identity__RequiredManagedIssuers__0` from this pool's exact
issuer, also used for SCIM. A compatible Server must require current managed
issuer/subject membership before granting authority; Cognito group claims alone
must not restore missing or inactive membership. This binding requires native
missing-member refusal controls and the corresponding installed image before
runtime qualification. Other deployments retain the native empty-list default.

## Saved-plan workflow

`hosted-pilot-deploy.yml` runs only reviewed trunk source and a tracked
`deployments/*.tfvars.json` file. Inputs contain secret ARNs, digest-only images,
a manifest digest and full component source SHAs. Ambient Terraform arguments,
variable files, public-demo names, broad ingress and unresolved bindings fail.
The apply deadline must be within seven days. Destroy can run after expiry.

Plan and apply use different exact-environment OIDC roles, plus a separate STS
state-access role. A value-free review artifact records source/config/provider
lock/backend/operation/run/attempt/plan digests and resource actions. The binary
plan, Terraform logs, state and output values never enter GitHub public artifacts
or logs. The binary lives in private KMS-encrypted versioned S3 with conditional
creation and deletion protection. Apply downloads that exact object version,
checks its digest/encryption and every binding, and runs only `terraform apply
saved.tfplan`. A plan older than 24 hours fails. Existing environment approval
rules are honored; no additional independent reviewer is required by this root.

The existing pilot $2,000 budget, $200 cleanup reserve and four-attempt maximum
remain distinct from the legacy RC campaign policy. A short-lived owner ledger
object, created by bootstrap IaC, binds source/config, spend, reservations and
available deployment allocation. A conditional immutable reservation object
limits each ledger reservation to one workflow run. Plan/apply roles cannot
change the ledger. This does not independently measure actual cloud spend;
the shared budget owner must reconcile spend/reservations before another run.
Destroy does not consume a paid deployment reservation or require an unexpired
ledger, preserving the cleanup path.

After apply the workflow waits for ECS stability and records task/image identity
through read-only AWS APIs. GitHub-hosted runners do not need admin ALB ingress.
Network refusal checks are available with `readiness.py` on an already permitted
operator runner; do not widen the allowlist for dynamic GitHub runner addresses.
Its receipt explicitly distinguishes skipped network checks, real human
issuer/subject binding, author refusal, approver acceptance, and model journeys.
ECS readiness is not a passing authentication/campaign receipt.

For cleanup, apply a reviewed configuration with `deletion_protection=false`
first, then dispatch operation `destroy` against the same backend. A nonempty
versioned artifact bucket refuses deletion unless `destroy_artifact_data=true`
is explicitly reviewed. Pilot RDS follows the module's nonproduction no-final-
snapshot policy; any data that must survive needs a governed backup before
teardown. Bootstrap custody is retained with `prevent_destroy`. Private binary
plans expire after seven days through the bootstrap S3 lifecycle policy.

## Bootstrap and exact prerequisites

`bootstrap/` defines the state/plan buckets, KMS key, OIDC plan/apply roles,
state-access role, immutable-plan/reservation policies, workload boundary and
owner budget ledger. The runtime boundary admits module-generated CMK decryption
only for this pilot ResetBoundary tag, its Secrets Manager encryption context and
regional Secrets Manager service; custody-key access is excluded. Foundation names are outside the workload namespace.
An explicit deny on the exact custody key remains effective even though recovery
ownership tags include the same ResetBoundary value as the workload.
The account-specific provisioning policy is an explicit reviewed input, with
no global action wildcard; grants must cover this root's tagged network,
ECS/RDS/Redis, scoped IAM, Cognito, DNS/certificate and S3 operations. No blanket
administrator policy is supplied for the deployment executor.

The concrete foundation workflow patch is
`bootstrap/foundation-bootstrap-workflow.yml`, installed as
`.github/workflows/hosted-pilot-bootstrap.yml` in `honua-iac`. Both repositories
are public. Existing IaC foundation AWS secrets stay in its workflow runner;
no foundation AWS credential is exported to release. The exact release-root SHA
must be on reviewed trunk and configuration must be tracked in IaC trunk.

The workflow selects exactly one phase: `custody-only` or
`remote-backed-foundation`. `mode=plan` publishes value-free metadata and destroys
its private binary. `mode=apply` prepares a fresh saved binary in one ephemeral
job, publishes its metadata, and waits while a separate `terraform-live-approval`
job obtains actual human review. That protected job emits a source/run/attempt,
phase, configuration and plan-hash-bound receipt. The original runner verifies
native job success and human review, applies only that original binary, and stops.
It cannot silently replan, reuse an old plan-only artifact or continue into a
second phase. The old combined `deploy` mode is refused before cloud operations.

Initial custody apply persists encrypted, versioned recovery state and migrates
it to the new KMS-encrypted locked backend. A distinct foundation dispatch then
requires that secured canonical state and another exact saved-plan review.
Foundation refuses custody updates, deletion or replacement. Original creation
identity is independently checked on the retained KMS key and both buckets, its
successful original producer and protected source; custody keeps those tags while
new foundation resources use the current producer identity.

Approval expires within twenty minutes of preparation; its separately reserved
execution window is at most fifteen further minutes. Both are clipped to original
funding/infrastructure expiry. The watchdog has no AWS credentials; its fixed
helper can cancel only its own source-bound workflow on failure or expiry, using
`actions:write` confined to that job. A timeout or interrupted apply retains all
liabilities and recovery evidence; it never settles costs or claims qualification.
No binary plan, Terraform state or private log is uploaded to GitHub. Normal
application deployment remains the separate OIDC saved-plan workflow above.

A successful custody completion is retained separately from its original creator
under an immutable content-addressed key in the existing encrypted recovery
prefix. If that creator failed, full foundation requires the independently
successful recovery's original native plan, real human approval, admission and
completed apply. The creator stays failed and its tags remain unchanged. Source
compatibility is checked; unrelated or forged recovery metadata is refused.

If custody reached canonical state but completion acknowledgement was lost,
`custody-only` can reconcile that state through another reviewed plan containing
only reads/no-ops, with no imports or physical infrastructure changes. It cannot
retag or maintain custody. Its metadata/state writes retain the same original
scope, funding holds and expiry; they do not settle costs or erase failed runs.

A failed initial custody apply can leave resources. Actual state is written only
to verified private, KMS-encrypted, versioned S3. Metadata-only recovery journals
are also retained there under immutable content-addressed keys with exact object
versions; the recovery prefix refuses overwrites and version deletion. GitHub
artifacts are supplementary copies, so a native rerun cannot remove the sole
recovery record. Supply the original `recovery_run_id`; artifact ID and digest
are optional together. The helper verifies that native producing workflow/run
on trunk, then retrieves and hashes its durable journal. When an artifact is
supplied its exact digest remains mandatory.

Before secure storage exists, recovery reconstructs custody import IDs through
read-only AWS discovery. KMS creation stores original foundation/release source,
config path/digest, provider lock and run ID as atomic creation tags. Buckets
must independently have the same identity plus ManagedBy/ResetBoundary tags;
an untagged name collision or cancellation between bucket creation and tagging
retains explicit unresolved ownership liability and refuses adoption. Incomplete
or ambiguous discovery cannot proceed to IAM/application creation. Original
creation identity survives imported-resource plans and reruns.

Recovery generates constrained Terraform import blocks for custody resources
only. A fixed helper can recover an older identity through a reviewed entry in
`bootstrap/recovery-compatibility.json`: bind old release/foundation SHAs,
unchanged config digest/path, old/new provider locks and the new bootstrap
contract content digest, with a reason. The digest includes helper, Terraform
and workflow sources; its map is excluded to avoid self-reference. No execution
of a broken old helper or manual AWS/import command is required. The default
map is empty. Failed state migration stops bootstrap with explicit liability;
remote custody and creation tags survive runner loss. Later-phase failures
retain partial resource state in the encrypted remote backend.

GitHub configuration is separate: `hosted-pilot-bind.yml` uses the existing
release GitHub token to verify the exact successful foundation producer and
nonsecret bindings digest, then reconcile `github-bindings.json`.
The explicit `release_root_sha` is the producer's immutable release source,
checked out independently and verified against protected trunk ancestry and the
unchanged GitHub desired-state contract. Later unrelated trunk commits do not
invalidate it; the consumer helper executes from its own immutable workflow SHA.
The normal plan/apply jobs share one fixed workflow-run SHA, including job reruns,
so later trunk commits do not change saved-plan identity either. The binding creates
trunk-only pilot environments and workflow variables and preserves any existing
reviewer policy. It needs no new GitHub admin token in IaC. Actual token
permissions must permit workflow dispatch, run/artifact reads and release
environment/variable writes.

Before installation, integrate the foundation workflow patch; review its
tracked bootstrap configuration and scoped provisioning policy; supply the
existing account GitHub OIDC provider ARN, owner ledger allocation and independent
secret references; run its plan/deploy and GitHub binding workflows; and review
actual deployment inputs (unused hostnames inside the delegated zone, explicit
operator CIDRs, compatible immutable component artifacts including both auth
fixes (Console PR456 at a73b0f0181dac325fd5adda102edbb573d578eae), manifest/source digests, owner and cleanup deadline). No live ARNs, users,
installed artifact identity or successful qualification are fabricated here.

Leaving `console_agent_secret_arn`, `console_agent_key_id` and
`console_agent_secret_version_id` null permits explicit foundation/authentication
staging. It injects no agent credential; Console refuses native agent sessions
and the native model remains disabled. This is never demo readiness. A random
secret cannot substitute for a Server-issued, scoped, persistent credential and
its exact canonical actor binding. The final Server image must contain reviewed
workspace/turn authority sources as well as the browser-authentication changes;
the standalone authentication branch does not contain that feature family.

For the campaign, use `campaign/` as the sole application deployment root. Its
`application` block binds the same manifest and Server image to Console and
Cognito through the shared providerless `application/` module; the parent
campaign still owns its network, private controllers and accounting. Standalone
and campaign deployment roots each configure the provider explicitly. The
composed application reuses that network and the original
candidate database, Server resource names and file bucket. Cognito and Console
retain the foundation's exact pilot namespace and runtime permission boundary;
the application receives no controller/model authority. Do not deploy a separate
standalone application for this campaign. The declared state moves preserve
resource ownership, but every saved plan must still be inspected for actual
provider replacements, including the certificate expansion and DNS transition.

Create authentication staging first, enroll the real nominated humans, then run
the existing trusted native bootstrap. Review its actual `issued_native_keys`
receipt into the campaign inputs. The next saved-plan update installs the same
agent ID as the Server workspace member and injects only its original secret
version into Console. Capture the human workspace after this update and verify
independent agent readback. Host configuration does not prove that a supplied
secret really authenticates as the declared key; installed target/credential
observations remain mandatory. Native inference stays disabled until its
separate approved provider/accounting path is connected and qualified.

## Offline verification

The contract workflow validates both roots, runs provider-mocked refusal/custody
tests, verifies the actual rendered proxy command/config against its pinned image
with networking disabled, and runs adversarial saved-plan/refusal checks. Mocks
never call AWS or create infrastructure. They cannot prove actual cloud IAM,
registry authorization, certificates, RBAC, Cognito login or application grants.

### Native credential operation ceilings

Native bootstrap configuration version 2 binds the independently observed native
`targetId`. Before SQL identities or API keys are created, the private task reads
that exact target through the native endpoint and refuses a mismatch. Reviewed
`datasetImportScopes` name individual `target/schema/table` destinations;
`managedProcessIds` currently admits only `analytics.buffer-aggregate-managed`.
These operation grants are separate from `readGrants` and reach only the scoped
agent key. They confer no approval authority and do not replace each native
task's source, caller, workspace, turn or execution checks. Empty operation lists
supply no such authority. Older configuration versions cannot silently acquire
mutation permissions. Actual installed workflows and retained execution evidence
remain required after issuance.

### Explicit Studio restore authority

The optional `native_bootstrap.studioRestoreItemIds` list names the exact items
whose saved-version pointers the agent may propose restoring. Its default is
empty; composition and publication grants never imply restoration permission.
The reviewed bootstrap issues `agent:studio-restore:<item-guid>` only for this
list, retains the requested grants before issuance, and checks the native
effective-permission readback. Wildcards, duplicate IDs and the empty GUID are
rejected. The setup and observer credentials receive no restore grant.

The installed server must implement the guarded native restoration contract
(honua-server#6532). A successful issuance or offline mock run is not a P13
qualification: retain the actual original version/hash and pointer revision,
separate human approval, native receipt and independent observed restoration.

### Installed runtime credential issuance

The optional `native_bootstrap.installedRuntime` object contains `runtimeId` and
`mutationCeilings`. Supply it together with a fourth, dedicated
`secretArns.control` destination. The existing three-key bootstrap keeps its
original behavior when both are absent. Installation requires the authenticated
original interactive funding record, and every issued key keeps that record's
exact expiry. This operation cannot renew funding or reuse an earlier partial
issuance.

The control key receives exactly `admin:read` and
`agent:runtime-control:<runtimeId>`. Only the private controller may receive its
immutable secret version, through `HONUA_EXTERNAL_EXECUTION_CONTROL_KEY`; the
model worker must never receive that credential, the setup key, or their custody
permissions. Each key has a distinct native ID. Native effective-permission
readback must confirm the issued ID and exact grants before completion.

The five Boolean ceilings are `createMap`, `createDashboard`, `compose`,
`proposePublication`, and `proposeRestore`. Each explicitly enabled ceiling
adds its corresponding runtime-scoped permission to the task key. Restoration
is independent of composition/publication. The additional
`datasetImportScopes` and `managedProcessIds` arrays must exactly preserve the
original bootstrap scopes. Existing item-scoped permissions remain unchanged;
none of these permissions conveys human approval.

Every installed task key also carries `agent:runtime-task:<runtimeId>`, including
an import-only installation with all five Studio ceilings disabled. This marker
grants no operation. The native server must use it to require the current
registered task key and exact execution/workspace/turn/generation/capability
binding on all resource paths; omitted headers or missing registration cannot
fall back to static-key authority. Only fixed read-only installation metadata
(target identity and that key's own effective permissions) is available before
registration. The three-key bootstrap without `installedRuntime` is separate.

Credential issuance ends at `registration-pending`, with `qualification: false`.
Installation then follows this order:

1. Authenticate the issued task key against the original target and effective
   permissions, and retain its immutable MCP base secret as inactive.
2. Apply the reviewed worker IaC to obtain the actual fixed task definition,
   task role, image digest and MCP secret version.
3. Use a separate privileged installation operation to register those observed
   slots, the exact issued task/control IDs, original expiry and explicit ceilings
   with the native server. A lost registration acknowledgement uses the original
   registration GET; it does not authorize another mutation.
4. Activate the controller only after the original native registration GET is
   retained and verified. Native actor, owner and credential-version fields come
   from that server; the producer must not reconstruct them from a key ID or a
   secret. A Secrets Manager version is separate from a native credential version.

The optional credential producer is only the first installation step. It does
not itself create the worker infrastructure, register the runtime, dispatch a
Console turn or qualify a live AI journey.

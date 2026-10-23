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

The pool disables public registration and self-service account recovery. No users,
memberships, invitations or application grants are silently created. Real distinct
author/approver subjects and the independently scoped agent still require a
reviewed identity/application provisioning workflow and separate runtime proof.
The two configured Server admin-role slots contain only the approver group.

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

Initial bootstrap keeps plan/apply in one source-reviewed ephemeral job. It
first saves and checks a custody-only plan, applies that exact local plan,
persists an encrypted versioned recovery copy, and migrates state into the new
KMS-encrypted locked backend. No IAM/application phase starts until the remote
state object is verified. The full foundation then uses that remote backend and
another exact local saved plan. Plan-only mode publishes value-free review
metadata; its binary is discarded. No binary plan, Terraform state or private
log is ever uploaded to GitHub. Normal deployment remains the separate OIDC
saved-plan workflow described above.

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

`console_agent_secret_arn = null` permits explicit foundation/authentication
staging. It injects no agent credential; Console refuses native agent sessions
and the native model remains disabled. This is never demo readiness. A random
secret cannot substitute for a Server-issued, scoped, persistent credential and
its exact canonical actor binding. The final Server image must contain reviewed
workspace/turn authority sources as well as the browser-authentication changes;
the standalone authentication branch does not contain that feature family.

## Offline verification

The contract workflow validates both roots, runs provider-mocked refusal/custody
tests, verifies the actual rendered proxy command/config against its pinned image
with networking disabled, and runs adversarial saved-plan/refusal checks. Mocks
never call AWS or create infrastructure. They cannot prove actual cloud IAM,
registry authorization, certificates, RBAC, Cognito login or application grants.

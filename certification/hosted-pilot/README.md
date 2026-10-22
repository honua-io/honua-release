# Private hosted-pilot infrastructure preparation

This root is an offline-reviewed preparation slice, not a usable hosted Console
pilot. It consumes an immutable `aws-ecs` module commit (outputs prerequisite:
[honua-iac PR284](https://github.com/honua-io/honua-iac/pull/284)), isolates a new
Server/RDS/Redis/S3 stack, and defines one private Console task with a dedicated
empty application role and scoped execution-role secret access. Images must be
digest-pinned and accompanied by manifest/source bindings. No credentials or
artifact IDs are supplied in an example deployment configuration.

Console has no ingress rule, listener, target group or public DNS. Production
authentication stays fail-closed; native model and Server provider execution are
disabled. No arbitrary `auth-ready` flag enables exposure. The task's HTTPS
egress does not prove Server connectivity: before enabling the real journey,
bind and test a private/authenticated transport, hostname resolution and the
Server ALB ingress for the actual task/NAT source. Operator CIDRs alone are not
evidence that Console can reach Server.

Only the Server has a proposed HTTPS hostname, restricted to explicitly supplied
operator CIDRs; this root creates no DNS record. The certificate and DNS must
actually match. Operations remain default-deny. No human roles, IdP, model,
publication approval policy, audience origin or user session is fabricated.
The agent secret reference must already identify a separately authorized agent;
the root neither creates its grants nor reads its secret value.
Image syntax validation does not prove registry access: this task has no private
registry `repositoryCredentials`; bind publicly pullable artifacts or ECR images
actually readable by its execution role. The task has no application readiness
health check. ECS running status and circuit-breaker success do not establish
authentication or BFF readiness; Production can boot while refusing operator
login. Independent application preflight remains required.

Offline checks (provider/module downloads during init, no AWS API calls):

```bash
terraform -chdir=certification/hosted-pilot fmt -check -recursive
terraform -chdir=certification/hosted-pilot init -backend=false -input=false
terraform -chdir=certification/hosted-pilot validate
terraform -chdir=certification/hosted-pilot test
```

Tests mock providers and override the Server module; they check this root's
configuration and refusal boundaries, not the module's runtime or actual IAM.
Never interpret them as installed qualification.

Before any coordinated cloud plan/apply, bind the candidate digests and verify
published provenance; copy `backend.tf.example` to ignored `backend.tf` and
replace every placeholder using a dedicated encrypted/locked state backend and
separate backend-access role. The generated bootstrap admin/database/encryption
values make state sensitive even though no existing secret value is fetched.
Supply reviewed inputs in an ignored tfvars file with secret **ARNs** only.
Use short-lived executor credentials; do not retrieve or print secret values.

Next implementation depends on the selected identity contract. Review either a
maintained trusted OIDC proxy plus actual Server BFF authorization-code sign-in,
or a signed ALB identity verifier. Qualify Cognito role projection and actual
endpoint authorization; do not disable issuer/audience checks or promote the
agent to an admin. Then add the private trusted edge ingress, HTTPS operator
route, separate anonymous audience routing with stripped identity headers, and
negative spoofing/operator-route tests. Preserve required Blazor negotiation and
WebSockets. See [the source findings and acceptance sequence](../../docs/foss4g-hosted-candidate.md).
For the identity-only/BFF option the existing role-mapping configuration can
include `Oidc:ClaimsMapping:AdditionalRoleClaimTypes=[cognito:groups]`, subject to
the real identity-claims-mapping entitlement. Configure an actual distinct
approver group in `Oidc:AdminRoles`; the nonadmin proposer still needs explicit
resource grants. Supply actual Generic OIDC client/secret references and the
Server operator-bearer signing configuration, rather than forwarding raw Cognito
access tokens. These deployment bindings remain absent from this root.

Retain only one Console task until restart/session recovery is qualified.
SingleInstance Server plus Redis is not a multinode qualification. `ExpiresAt`
is an ownership tag, not a scheduler: a reviewed teardown/reaper binding is still
required before retention. RDS/ALB deletion protection and nonempty versioned S3
storage deliberately require an explicit cleanup review; this root does not
silently destroy retained data. NAT, ALB, database, Redis, Fargate and log/storage
charges accrue while resources exist. No apply, live inference, demo mutation or
hosted acceptance pass has been performed by this slice.

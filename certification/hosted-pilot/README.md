# Private hosted-pilot infrastructure preparation

This root is an offline-reviewed preparation slice, not a usable hosted Console
pilot. It consumes an immutable `aws-ecs` module commit (network outputs and
browser-authentication secret binding:
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
actually match. Operations remain default-deny. Cognito is the chosen identity
provider; the isolated pool/client definitions below are preparation, not an
active login. No model, publication policy, audience origin or user session is fabricated.
The agent secret reference must already identify a separately authorized agent;
the root neither creates its grants nor reads its secret value.
Image syntax validation does not prove registry access: this task has no private
registry `repositoryCredentials`; bind publicly pullable artifacts or ECR images
actually readable by its execution role. The task has no application readiness
health check. ECS running status and circuit-breaker success do not establish
authentication or BFF readiness; Production can boot while refusing operator
login. Independent application preflight remains required.

Infrastructure creation/configuration must run through versioned IaC and a
checked-in deployment workflow. Read-only CLI inspection is permitted; ad hoc
AWS mutations, Console setup and local manual Terraform apply are excluded.
The current workflow performs offline checks only. The saved-plan apply workflow,
its backend/bootstrap bindings and all remaining runtime resources must be
implemented before installation.

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
published provenance. The deployment workflow must render the reviewed
`backend.tf.example` binding using a dedicated encrypted/locked state backend
and separate backend-access role; it must refuse unresolved placeholders. The generated bootstrap admin/database/encryption
values make state sensitive even though no existing secret value is fetched.
Supply reviewed workflow inputs with secret **ARNs** only; the workflow must
keep any rendered tfvars file private and ephemeral.
Use short-lived executor credentials; do not retrieve or print secret values.

## Cognito identity preparation

`identity.tf` defines a new `${name_prefix}-operators` LITE user pool, isolated
classic hosted-UI domain, separate confidential `console_edge` and `server_bff`
clients, and explicit `${name_prefix}-authors` / `${name_prefix}-approvers` groups.
It disables self-registration and self-service recovery, permits optional TOTP,
and creates no users, memberships, invitations, IAM group roles or automatic
application grants. User-pool deletion protection is enabled. A separately
reviewed human provisioning step must keep proposer and approver subjects distinct.

Both clients allow only authorization-code OAuth and `openid profile email`
scopes. They have separate generated client secrets, token revocation enabled,
15-minute access/ID tokens and eight-hour refresh tokens; direct password/SRP,
implicit and client-credentials flows are not enabled. The hosted login still
maintains its own session cookie; a short token lifetime does not force fresh
credentials at every expiry. Applications must use PKCE S256 and validate actual
issuer/client/nonce/state. See [AWS app-client contracts](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-client-apps.html)
and [AWS managed-login token lifetime caveat](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-the-access-token.html).

Required `identity_redirects` inputs accept only a dedicated HTTPS DNS Console
origin, its edge `/oauth2/callback` and BFF `/admin/auth/callback`, plus exact
`/auth/login` and `/admin` logout destinations. No wildcard, fragment, query,
userinfo, arbitrary port or IP callback is accepted. The Server BFF client's
callback is on the **Console** origin: its existing broker retains the state and
upstream Server cookie privately. Keep API `Public:BaseUrl` on the API origin.
Usable separate-origin sign-in depends on [Server PR 6518](https://github.com/honua-io/honua-server/pull/6518)
and its `Oidc:AdminAuthCallbackUri` option selecting this one exact Console callback for
both authorization and exchange; global public-base rebinding is not a workaround.
The configured callback must actually route to Console through the trusted edge.

Cognito logout uses `client_id` and `logout_uri`, unlike a generic assumed
`post_logout_redirect_uri` flow. Creating allowed destinations does not prove
that Console/edge sign-out clears the hosted Cognito session. Test real logout
and reauthentication explicitly. [AWS logout endpoint](https://docs.aws.amazon.com/cognito/latest/developerguide/logout-endpoint.html)
also does not promise sign-out from external federated identity providers.
Likewise, token revocation does not invalidate tokens already accepted by an
offline signature/expiry verifier; test the actual Server/edge session boundary.
See [AWS revocation limitations](https://docs.aws.amazon.com/cognito/latest/developerguide/token-revocation.html).

Each Cognito client secret is stored in its own Secrets Manager secret/version.
An independent 64-character alphanumeric operator-bearer signing key is generated
separately and stored in another secret. Server `OperatorBearerOptions.cs` uses
UTF-8 bytes with a 32-byte minimum, so no base64 decoding is assumed. Outputs
contain only issuer, client IDs, group names, redirect URLs and individual secret
ARNs. Generated secret values remain sensitive Terraform state; never print them
or copy them to tfvars. The Server module receives only its BFF client and
operator signing secret references, with execution-role access and optional
scoped KMS access through `identity_secrets_kms_key_arn`. Its first task depends
on populated secret versions. The edge consumer remains uninstalled and has no
secret access binding. No runtime credential has been retrieved or exercised.

The intended maintained-proxy path is **identity-only**: forward the verified
stable `sub` and optional verified email, strip incoming/forwarded access-token
and Authorization headers, and let actual Server Sign In/BFF exchange establish
the Server-issued operator bearer. Do not enable `--pass-access-token` for this
path. Server configuration uses `Oidc:Generic` with authority, client ID,
secret environment indirection and `openid profile email`, plus
`Authentication:OperatorBearer` signing configuration. A raw AWS secret-reference
string is not resolved automatically by those application options: the execution
role must inject its value into the configured environment variable.

Existing entitled role mapping can use
`Oidc:ClaimsMapping:AdditionalRoleClaimTypes=[cognito:groups]` and the actual
approver group in `Oidc:AdminRoles`. The identity-claims-mapping entitlement and
nonadmin author resource grants remain required and unqualified. App-client
creation alone proves neither role projection nor any endpoint authorization.
Both configured role indices replace the two appsettings defaults; a compatible
Server must also replace constructor-default arrays during configuration binding.
Existing shared authorization policies still recognize legacy admin aliases;
this is not a global approver-only policy. Keep Cognito group administration
restricted, provision only the intended groups, and demonstrate author refusal
and distinct approver acceptance against actual endpoints.
Do not forward raw Cognito access JWTs or promote the agent to an admin to avoid
these checks. Trusted edge ingress, separate anonymous audience routing with
stripped identity headers, Blazor/WebSocket continuity, actual logout and negative
spoofing/operator-route tests remain installation work. See
[the source findings and acceptance sequence](../../docs/foss4g-hosted-candidate.md).

Retain only one Console task until restart/session recovery is qualified.
SingleInstance Server plus Redis is not a multinode qualification. `ExpiresAt`
is an ownership tag, not a scheduler: a reviewed teardown/reaper binding is still
required before retention. RDS/ALB deletion protection and nonempty versioned S3
storage deliberately require an explicit cleanup review; this root does not
silently destroy retained data. NAT, ALB, database, Redis, Fargate and log/storage
charges accrue while resources exist. No apply, live inference, demo mutation or
hosted acceptance pass has been performed by this slice.

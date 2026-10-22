# FOSS4G hosted candidate preparation

Status: discovery and proposed deployment work, 2026-10-22. No apply, installed Console candidate, or passing hosted AI journey has been produced by this work. Existing RC gates and the bounded pilot scope remain unchanged.

## Observed demo and candidate separation

Read-only observations on 2026-10-22 identified `https://demo.honua.io` as the existing public Server deployment. Its capabilities manifest reported revision `199bce77fe990eb14855831790b07cfbc103d4ba`. Lambda function `honua-demo-demo-honua` in `us-west-2` had live alias version `51`, description `rc1 199bce7 clean`, and image digest `sha256:24bb5279ece9dc55da46a69c4bbed5430a773490fcb11476c5ab615f7305e7d3`. Both `/healthz/live` and `/healthz/ready` returned HTTP 200; `/studio` and `/version.json` returned 404. These observations establish a healthy existing Server, not an installed Console/Studio/native-agent candidate. Reobserve before relying on them.

The candidate needs its own state key, resource prefix, HTTPS origins, data fixtures, storage prefixes, users, workspace and reset boundary. Do not repoint the public demo alias or reset its data. Historical receipts in `honua-demo-infra/runbook/rc1-upgrade-dogfood-20261022.md` describe earlier versions and do not supersede live observations.

## Existing reusable contracts

Source paths below are repository-relative, prefixed by repository name; they identify implementation to review, not deployed provenance.

* `honua-iac/infrastructure/terraform/modules/aws-ecs/{main,variables,outputs,bedrock}.tf` provides Server Fargate tasks, ALB, RDS, optional Redis, secrets references, logs and task-role provider permissions. Its task definition contains one Server container; it has no Console or generic sidecar input. `infrastructure/terraform/examples/aws/main.tf` is the reusable root pattern. `existing_vpc_id`, `existing_vpc_cidr`, `existing_public_subnet_ids` and `existing_private_subnet_ids` allow root-owned networking.
* `honua-console/Dockerfile` consumes the published Console artifact, runs nonroot on port 8080 and includes embedded Studio. `.github/workflows/container-publish.yml` builds, verifies and publishes the image. Bind an actual immutable digest and its successful provenance; a source SHA alone does not prove that image exists.
* Console `/version.json` exposes its commit. `/_studio/honua-studio-artifact.json`, `STUDIO-PROVENANCE.json` and `SDK-PROVENANCE.json` identify the embedded assets. See `honua-console/docs/deployment/BUILD_ARTIFACT.md` and `scripts/vendor-studio-assets.mjs`.
* `honua-console/src/Honua.Console.Web/AgentSessionBff.cs` checks human workspace membership and binds the independent agent credential to the configured Server. GIS model requests use Server `/api/v1/studio/ai/chat` and canonical MCP `/api/mcp`. The optional `HONUA_AGENT_GATEWAY_URL` DevOps extension is not a prerequisite for this hosted GIS path. Do not transplant the SDK local-Docker installer gateway into ECS as a substitute.
* Release `.github/workflows/e2e-cloud-aws{,-cell}.yml` and `e2e/{run_cloud.py,targets/terraform_target.py,reap_cloud.py}` provide isolated ECS/Redis provisioning, readiness, evidence and teardown patterns. They do not currently deploy the required Console/authentication stack or establish a retained multi-day pilot environment.

## Proposed files and bounded implementation

Prepare the following new release-owned root, after its identity and artifact inputs are bound:

| Proposed file | Responsibility |
| --- | --- |
| `certification/hosted-pilot/main.tf` | Pinned reusable Server module, isolated backend/prefix, RDS/Redis/storage and candidate manifest binding |
| `certification/hosted-pilot/console.tf` | One Console Fargate task, private task networking, target group, HTTPS hostname route and logs |
| `certification/hosted-pilot/identity.tf` | Selected trusted authentication boundary and narrowly scoped secret-reference permissions |
| `certification/hosted-pilot/variables.tf` | Validated image digests, domains/certificate references, identity configuration, secret ARNs, TTL and resource bounds |
| `certification/hosted-pilot/outputs.tf` | Nonsecret origins, resource identities and installed-evidence capture inputs |
| `certification/hosted-pilot/README.md` | Exact plan, reviewed apply, preflight, reset and destroy commands for the selected binding |

A shared ALB requires a small reusable-module output extension in `honua-iac/infrastructure/terraform/modules/aws-ecs/outputs.tf`: VPC ID, private subnet IDs, ALB security-group ID and HTTPS listener ARN. Those outputs expose no secret values. The release-owned root can then add the Console target group and hostname rule without adding Console policy to the customer module. Review listener priority and certificate hostname coverage explicitly.

An alternative with no module changes is root-owned networking passed through the existing network inputs plus a separate Console ALB, using the existing cluster-name output. It adds an ALB and associated cost. Select between these only after identity routing is concrete; neither design currently exists as an installed pilot.

Configuration validation, immutable artifact binding, nonsecret output changes, teardown tests and offline plan-contract tests can be implemented without creating cloud resources. Actual apply requires the concrete inputs below and coordinated execution; this is not a request for a policy exception.

## Authentication binding before architecture selection

Console Production expects a trusted edge operator identity and a forwardable Server-valid human access token. It does not itself supply an OIDC-client login. See `honua-console/src/Honua.Console.Web/Auth/{ConsoleAuthentication,ConsoleEdgeAuth}.cs` and `src/Honua.Console.Shell/Security/ConsoleAuthConstants.cs`.

Prefer an already operated trusted OIDC proxy if one can be bound to the pilot issuer and Server audience. Its public endpoint must strip client identity headers, authenticate the user, forward the actual bearer, and be the only network path to Console. Configure the edge shared-secret reference as an additional trust boundary. A proxy is an identity component, not the optional native-agent gateway.

Direct ALB OIDC remains an alternative only with a reviewed signed-identity verifier. Although Console's configurable header names can mechanically map to `x-amzn-oidc-identity` and `x-amzn-oidc-accesstoken`, that mapping alone is insufficient. AWS requires signature verification of `x-amzn-oidc-data`, the expected signer ALB ARN, and extraction of the verified subject. Implement and negatively test that verifier before treating the ALB identity as trusted. See [AWS authenticate users using an Application Load Balancer](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/listener-authenticate-users.html). Private security groups do not replace the documented verification requirement.

A read-only `cognito-idp list-user-pools` inventory in the current account's `us-west-2` returned no pools on 2026-10-22. Demo source contained workload GitHub OIDC configuration, not an application issuer/client binding. This does not prove there is no external IdP or pool in another account/region. No existing human issuer, client, credential or session is assumed.

Server `honua-server/src/Honua.Hosting/Features/Authentication/OidcAuthenticationExtensions.cs` validates token issuer, audience, lifetime and signing key, applies Honua OAuth scope governance, and derives human provenance separately. Bind actual supported issuer discovery, audience, human-session evidence, tenant/role claims and required Honua scopes. A successful ALB login or valid JWT alone does not establish permission. Use actual distinct human proposer and approver principals; keep the agent separately authorized and preserve the governed pending-proposal path.

## Configuration and references, never secret values

### Cognito compatibility findings from inspected source

These are source-level compatibility constraints, not observed live authentication failures. Provider selection remains pending; neither a new Cognito pool nor an existing external issuer is selected here.

* Cognito access tokens normally identify the app client with `client_id`; `aud` is present only when resource binding was requested. Server `honua-server/src/Honua.Hosting/Features/Authentication/OidcAuthenticationExtensions.cs` currently maps a Generic issuer to its configured `ClientId` audience and checks that pairing. A resource URL audience is not automatically accepted through that mapping. Do not disable audience validation. See [AWS access-token claims](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-the-access-token.html).
* Cognito resource-server custom scopes use the resource identifier and scope name separated by `/`. Server `honua-server/src/Honua.Core/Features/Authorization/Domain/OperatorScopeCatalog.cs` recognizes exact `honua.mcp.*` tokens, so a prefixed custom scope is not automatically a Honua grant. See [AWS scopes and resource servers](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-define-resource-servers.html).
* `honua-server/src/Honua.Hosting/Features/Authentication/StudioLifecycleAuthorization.cs` requires a subject plus a recognized strong `amr`, or `sid` plus positive `auth_time`, before deriving bearer human provenance. The documented default Cognito payload's `sub` and `auth_time` alone do not establish that contract. Login success must not be treated as endpoint authorization.

The existing identity-only BFF path is a separate alternative to forwarding raw Cognito access tokens. `honua-console/src/Honua.Console.Web/Auth/ConsoleServerSessionBff.cs` completes a real Server authorization-code sign-in, retains the Server session cookie, and uses `HttpConsoleOperatorBearerExchange` to obtain a Server-issued operator bearer. `honua-console/docs/console-authentication.md` documents preservation of that bearer across subsequent identity-only edge requests. For this path, do **not** require oauth2-proxy `--pass-access-token`: an edge token can override the stored operator bearer. The direct-access-token path instead needs an explicitly compatible token contract; oauth2-proxy `--pass-authorization-header` forwards an ID token and is not an access-token substitute. See [OAuth2 Proxy header options](https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview/).

The BFF alternative still needs endpoint-specific qualification. Server `AdminAuthEndpoints.cs` issues the operator bearer from a live session with validated identity claims; `honua-server/src/Honua.Hosting/Features/Authentication/AdminAuthClaimsProjector.cs` recognizes `roles`/`role`, not automatically `cognito:groups`. `OperatorBearerAuthenticationHandler.cs` does not stamp the bearer human-provenance marker, and `StudioLifecycleAuthorization.cs` does not list the operator-bearer scheme among its interactive schemes. Validate actual nonadmin role/grant projection and required interactive operations before claiming compatibility; preserve distinct human approval and independent agent attribution.

Public audience routing is also an installation contract. `honua-console/src/Honua.Console.Web/Auth/ConsolePublicSurfaces.cs` allowlists `/public`, `/share/public` and `/embed/maps`; the corresponding anonymous pages still enforce public-item access. An authenticated proxy must not make those intended audience journeys require operator login. Prefer a separate public viewer origin with identity headers stripped on every request, including assets and required Blazor connections, and demonstrate denied operator/BFF routes from that origin. If using method/path authentication exemptions instead, bind the exact routes and negatively test encoded paths, negotiation requests and spoofed forwarded headers. Broad skip routes are not evidence of safe anonymous access.

Console nonsecret settings are `ASPNETCORE_ENVIRONMENT=Production`, `HONUA_SERVER_BASE_URL`, matching `HONUA_AGENT_SERVER_BASE_URL`, `HONUA_NATIVE_MODEL_ENABLED=true`, `HONUA_AGENT_RELEASE_ID` and `Studio__EmbedJsStudio=true`. For the trusted proxy option use `Honua__Console__Auth__Mode=EdgeForwarded` and the appropriate `Honua__Console__Auth__EdgeForwarded__{UserHeader,EmailHeader,AccessTokenHeader}` names. Inject `HONUA_AGENT_API_KEY` and `Honua__Console__Auth__EdgeForwarded__SharedSecret` through scoped secret references, never tfvars plaintext, browser configuration, receipts or logs. The agent key must identify the independently authorized agent with explicit workspace membership. Do not substitute `HONUA_ADMIN_API_KEY` or Development login.

Use `aws-ecs` typed Bedrock variables (`enable_bedrock_ai`, model, region, token and timeout bounds). `bedrock.tf` configures both `StudioAiProxy` and `WorkflowGeneration` and scopes task-role model invocation permissions; no static provider key is required for that path. Verify the selected model/inference-profile access and region with a bounded real-model check only after coordination. Record actual provider/model configuration independently of a client label.

Redis requires the module's operation-key-ring certificate secret reference and audit-chain references. Storage, database and native-agent credentials require separate least-privilege task/execution-role permissions. Bind Server image digest, Console image digest, embedded Studio/SDK hashes, IaC revision, release manifest digest and actual provider/model configuration in evidence. Never infer deployment from repository defaults.

Keep one Console task for the first slice. Its operator session stores are process-local; server canonical Redis state does not prove Console cookie/circuit or operator-session continuity across restart. Restart recovery and multinode Console operation need separate qualification. Preserve Blazor negotiation, WebSockets and the actual browser page/session; do not use a refreshed storageState file as proof of sessionStorage continuity.

## Preflight and acceptance sequence

1. Resolve immutable candidate artifacts and provenance against the release manifest; reject mismatched or missing digests. Bind actual domains, certificate references, issuer/client/audience/scopes, real separate human identities and scoped agent principal. Do not guess IDs or credentials.
2. Produce a reviewable Terraform plan in isolated state. Confirm no public-demo resources, broad IAM grants, plaintext secrets or unrelated data are touched. Bound task/database/cache sizing, NAT/ALB/log retention, provider tokens, TTL, reset ownership and always-run teardown. Existing short-lived CI budget assumptions do not cover an idle multi-day pilot.
3. After coordinated apply, collect resource identities and compare observed Server capabilities revision, Console `/version.json`, embedded Studio/SDK provenance and running image digests to the candidate. Verify liveness/readiness and denied unauthenticated/spoofed-edge requests before enabling model traffic.
4. Establish actual human login and Server bearer validation. Check actor, tenant, workspace and feature authorization, rejected wrong issuer/audience/scopes, separate agent attribution, revocation and required pending approval. No synthetic user/session or supplied passing receipt qualifies.
5. Run bounded seeded readbacks and the same canonical Console draft/native-agent journey. Join persisted turn/workspace/draft/version identities with independent canonical GETs and visible map assertions. Preserve context across save/reopen and navigation; verify an authority change prevents stale execution and response loss is reconciled without mutation replay.
6. Have the real human propose in normal Console UI, capture the existing pending proposal read-only, and have the distinct human approve. Verify exact item/version/hash/operation/request joins and an independent audience witness. Direct admin publication does not satisfy the governed path.
7. Save evidence with its installation provenance and bounded scope, then demonstrate isolated reset and teardown. A healthy endpoint, model response or supporting fragment is not full AI-01 through AI-05 acceptance and does not alter RC gates.

Current readiness is deployment preparation only: reusable Server infrastructure and a runnable Console container contract exist; hosted Console/auth bindings, candidate artifacts, isolated installation and live acceptance evidence remain to be established.

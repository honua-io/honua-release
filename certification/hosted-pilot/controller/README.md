# Trusted controller image

`hosted-pilot-controller-image.yml` builds an exact Git archive of its immutable
trunk SHA. The image reconstructs and checks that source tree, records SHA256
file identities, and keeps the source/index readonly. Runtime verification
checks tracked bytes and executable modes without writing the index. A separate
worker image retains its own strict public-file allowlist; this image contains
trusted source and oracles and must never be an agent sandbox.

The foundation root creates the immutable, scanned ECR repository and scopes
the apply role's push permissions to that repository. GitHub binding reconciles
its repository reference; no registry is created by the image workflow. Before
push, the workflow runs source verification and dependency imports with a
readonly filesystem and networking disabled. Every published image has a
nonsecret source/tree/archive/image/run receipt. Deployment must use its digest
and verify the producing workflow identity, never a mutable image tag.

Publication also requires `config_path`, `budget_version_id` and `budget_sha256`
from the original reconciled owner allowance. The immutable budget must bind
this exact source/config and can fund only this producing workflow run; retries
do not mint another allowance. Actual registry expiry is checked before the
budget/build and again before registry authentication/push. Controller images
are limited to 4 GiB. IaC makes all immutable images eligible for service-managed
expiry after fourteen days and untagged artifacts after one day, including pushes
whose acknowledgment was lost. Actual asynchronous deletion is not a settled
storage charge. The source receipt retains image bytes, original funding and
the independently observed expiry policy. Publisher IAM grants no image delete.

The image has no default executable campaign. An explicitly reviewed task must
supply its frozen bundle, authenticated producer metadata and private work
volume. Only the controller/provisioner receives SQL and setup credentials.
The image alone does not establish a working backend or demo readiness.

The RDS trust bundle is vendored from
<https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem> with SHA256
`90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b`.
Remote SQL connections must use `sslmode=verify-full` and the vendored bundle;
no runtime CA download or trust bypass is needed. Dependency wheels are pinned
with hashes for CPython 3.12 / Linux amd64.

The private image also contains Node 22.23.3 and `playwright-core` 1.61.1 with
its canonical Chromium installation. An immutable upstream Playwright OCI image
transports the browser directories produced by that version's canonical
installer. Only the revisions selected by the locked package's `browsers.json`
are copied, including their installation markers; no ambient browser cache or
alternate Chromium version is accepted. Both npm files and the copy program
are part of the runtime contract. The image records the upstream OCI digest,
metadata hash and selected revisions in `browser-source.json`, and derives
`/opt/honua-render/runtime.json` from the installed
package's `browsers.json`; source configuration must use that exact observed
four-part Chromium version. Before publication, the checked image workflow
launches this browser with no network, a readonly root filesystem and a private
work volume, then requires an actual painted PNG. That startup control does not
establish a signed-in deployed Console observation.

Browser cookies and local storage reach only the private controller as one
immutable Secrets Manager version, then the fixed child on stdin. Its child
environment excludes AWS, SQL, native keys and model credentials. Every renderer
fence revalidates the durable lease, original attempt infrastructure/cleanup
reservations and positive admitted controller overhead. The 120-second child
has bounded JSON-line output and a private temporary directory that is removed
on success, failure and fence loss. Browser exceptions and raw stderr are not
retained. Model-facing worker images and configuration do not receive this
runtime or human storage.

Use `capture-console-session` through the ordinary protected preparation
workflow only after actual human sign-in storage and the source-reviewed
production Console deployment exist. Its preparation configuration has
`schemaVersion`, `candidateKey`, original bootstrap `operation`, `endpoint`,
`storageState` (`credentialEnv`, `secretArn`, `versionId`), `consoleDeployment`
(`region`, `clusterArn`, `serviceArn`, `taskDefinitionArn`, `imageUri`,
`edgeImageUri`, `issuer`) and actual native `targetId`. `consoleProfile` binds its
exact `id` and canonical HTTPS root `serverUri` to the same original native
observer endpoint. The fixed storage env is
`HONUA_CONSOLE_BROWSER_STORAGE_STATE`. The campaign root's optional
`human_browser` supplies that same immutable storage reference and exact Console
deployment identities through a saved plan; it never places cookie values in
Terraform. Missing storage remains a blocker.

Capture independently reads the actual stable Console ECS revision, production
EdgeForwarded mode, Cognito issuer, OIDC checks and trusted local edge header
binding. It then reads the actual signed-in Console session and the narrow
GET `/api/studio/v1/agent/target` bridge using those original cookies and the
same observed environment/partition. The native response must positively show
an actual `operator-bearer` human on the exact target. A Console authenticated
flag, development cookie or API-key principal is insufficient. That narrow
Console bridge must itself be deployed and qualified before capture can pass.
The native principal must include its actual validated `subjectId`. The helper
recomputes the Console partition from that subject, the reviewed profile ID and
canonical server URI using .NET UTF-16 string lengths and UTF-8 SHA256. Neither
namespaced `actorId` nor `studioOwnerId` is treated as a raw subject. Both reads
must forbid caching. Only identity projections are retained; the private
CSRF/session body is never stored or hashed.

Review the returned immutable `sessionCustody` reference into the frozen render
observer configuration, along with the same `storageState`, exact `runtime`,
`overheadCoverageDigest`, `schemaVersion = 1` and `timeoutSeconds = 120`. The
ordinary Session render hook invokes the private helper. It independently
replays the retained original model SAVE, immutable native version and journal,
then re-observes the same live human session/deployment before starting the
renderer. Only observed draft/version IDs join the source-frozen render
contract; expected content, features, styles and pixel predicates stay fixed.
The retained report does not establish Cognito qualification until genuine
hosted human custody and all original case oracles pass.

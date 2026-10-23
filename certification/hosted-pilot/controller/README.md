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

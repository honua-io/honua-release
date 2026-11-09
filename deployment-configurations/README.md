# Image-matched deployment configurations

`catalog.v1.json` retains deployment configuration bytes by immutable server
image index reference. Verified child digests are recorded as metadata. Each file has a SHA-256 checksum and each image records the
source revision and rollback mode. Validate the committed catalog with:

```sh
python3 tools/deployment_configuration.py --check
```

Select an image's configuration from a trusted, pinned release-repo checkout:

```sh
python3 tools/deployment_configuration.py \
  --image ghcr.io/honua-io/honua-server@sha256:c8d7aef98761618145725530b6a4e4b25ea4a9ae121da168362f9be4cf5f7d37 \
  --kind compose
```

Selection verifies the catalog and every retained file before printing absolute
paths and rollback metadata. Unknown images, missing deployment types, altered
bytes and paths outside this directory are refused. Checksums detect file drift;
the caller must establish trust in the release-repo revision containing the
catalog. The selector does not deploy services or change release gates.

## Retained rc.1 configuration

`2026.1-rc.1/compose.yaml` is the exact Compose block from the server quickstart
at the source revision recorded in the catalog, identified by the shipped image's
OCI revision label. Its source environment variables and dependency tags are
preserved. This snapshot is historical configuration, not a certified current
install profile. There is no retained Helm configuration for this image.

Rollback to rc.1 is **manual**. The rc.1 executor only selects an image and cannot
restore deployment configuration. Never run that image with the rc.2 native
launcher entrypoint: rc.1 does not contain `/app/honua-entrypoint`. The operator
must restore the matching configuration and image together, supply the rc.1
environment variables through their approved secret process, and perform the
documented database restore and readiness checks. The historical image's
inherited health check also needs a compatible readiness override; the rc.2
compiled probe is unavailable in rc.1.

For rc.2 and later, publish the image-matched Compose/Helm files and checksums
when their immutable image identities are available. A `paired` entry describes
a pair suitable for the release's paired rollback mechanism; the catalog alone
does not implement that mechanism. Do not add placeholders or local build IDs
as release image references. The platform manifest remains the authority for
which component versions ship together.

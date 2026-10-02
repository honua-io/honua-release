These are unedited registry index bytes captured on 2026-10-02 with:

```sh
docker buildx imagetools inspect ghcr.io/honua-io/honua-server@sha256:069f196bfa5c7201223d4d89868934242c4ace8805a6e48c122a88d84fa6eb1a --raw > honua-server/index.json
docker buildx imagetools inspect ghcr.io/honua-io/honua-console@sha256:37685c71c26931a512d9f1e6c4e19ecb270259f1a1e4c5ab4760caeb0450a619 --raw > honua-console/index.json
```

The SHA-256 of each file equals the pinned index digest. Expected architecture
digests in tests come directly from these registry descriptors, independently
of the generator and certifier. The unknown/unknown descriptors are BuildKit
attestations and must not enter the runnable architecture map. This capture
used an empty temporary Docker config for anonymous reads because the host's
Desktop credential helper could not execute inside the lane.

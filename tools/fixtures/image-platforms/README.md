These are unedited registry index bytes. honua-server was re-captured on 2026-10-27 for the
a4bde4b rc re-pin #4; honua-console was re-captured for the c00d16a8 rc.2 re-pin:

```sh
docker buildx imagetools inspect ghcr.io/honua-io/honua-server@sha256:c8d7aef98761618145725530b6a4e4b25ea4a9ae121da168362f9be4cf5f7d37 --raw > honua-server/index.json
docker buildx imagetools inspect ghcr.io/honua-io/honua-console@sha256:b74dfd7ccfd30744e87021b852f287cb81c266b40051ef860314dab33fde2e54 --raw > honua-console/index.json
```

The SHA-256 of each file equals the pinned index digest. Expected architecture
digests in tests come directly from these registry descriptors, independently
of the generator and certifier. The unknown/unknown descriptors are BuildKit
attestations and must not enter the runnable architecture map. This capture
used an empty temporary Docker config for anonymous reads because the host's
Desktop credential helper could not execute inside the lane.

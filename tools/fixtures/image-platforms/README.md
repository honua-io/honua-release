These are unedited registry index bytes. honua-server was re-captured on 2026-10-09 for the
3ecd214 re-pin; honua-console was re-captured for the c652ef22 pin (#486):

```sh
docker buildx imagetools inspect ghcr.io/honua-io/honua-server@sha256:62e312a8210ddf632ec5b903a2759327321966f72cbb9f75c0b2a3d2c1243458 --raw > honua-server/index.json
docker buildx imagetools inspect ghcr.io/honua-io/honua-console@sha256:686b104fc3f106df694782c243423c1c7110d568779abf5829514ab97349a908 --raw > honua-console/index.json
```

The SHA-256 of each file equals the pinned index digest. Expected architecture
digests in tests come directly from these registry descriptors, independently
of the generator and certifier. The unknown/unknown descriptors are BuildKit
attestations and must not enter the runnable architecture map. This capture
used an empty temporary Docker config for anonymous reads because the host's
Desktop credential helper could not execute inside the lane.

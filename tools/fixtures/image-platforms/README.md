These are unedited registry index bytes. honua-server was re-captured on 2026-10-23 for the
8cb074f rc re-pin #3; honua-console was re-captured for the a0c04a68 rc re-pin #2:

```sh
docker buildx imagetools inspect ghcr.io/honua-io/honua-server@sha256:2d7877c4d1cb3b30647fb225893d41b13728f0448997997cdf4bf40fe99be07d --raw > honua-server/index.json
docker buildx imagetools inspect ghcr.io/honua-io/honua-console@sha256:6612319d53ed9d3fab1a62848b6402d4340f57c227296003614ac88fe48de4f3 --raw > honua-console/index.json
```

The SHA-256 of each file equals the pinned index digest. Expected architecture
digests in tests come directly from these registry descriptors, independently
of the generator and certifier. The unknown/unknown descriptors are BuildKit
attestations and must not enter the runnable architecture map. This capture
used an empty temporary Docker config for anonymous reads because the host's
Desktop credential helper could not execute inside the lane.

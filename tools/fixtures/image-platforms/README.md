These are unedited registry index bytes. honua-server was re-captured on 2026-10-22 for the
199bce7 rc re-pin #2; honua-console was re-captured for the a0c04a68 rc re-pin #2:

```sh
docker buildx imagetools inspect ghcr.io/honua-io/honua-server@sha256:f2e54ed3db51dc057fbf01fa261a7d3c81520f1e04441b029f280a35e1853454 --raw > honua-server/index.json
docker buildx imagetools inspect ghcr.io/honua-io/honua-console@sha256:6612319d53ed9d3fab1a62848b6402d4340f57c227296003614ac88fe48de4f3 --raw > honua-console/index.json
```

The SHA-256 of each file equals the pinned index digest. Expected architecture
digests in tests come directly from these registry descriptors, independently
of the generator and certifier. The unknown/unknown descriptors are BuildKit
attestations and must not enter the runnable architecture map. This capture
used an empty temporary Docker config for anonymous reads because the host's
Desktop credential helper could not execute inside the lane.

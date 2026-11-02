These are unedited registry index bytes. honua-server was re-captured on 2026-11-02 for the
daf56db rc.2 cut; honua-console was re-captured for the c63b37fd final rc.2 re-pin:

```sh
docker buildx imagetools inspect ghcr.io/honua-io/honua-server@sha256:0dd350510ce294253981fd69c86cd9016ff5b85905878ec2785c54e2d1591268 --raw > honua-server/index.json
docker buildx imagetools inspect ghcr.io/honua-io/honua-console@sha256:ab453db9b2a468a05e58ebc445569fd2df84908df91967ae8d31417d6e971358 --raw > honua-console/index.json
```

The SHA-256 of each file equals the pinned index digest. Expected architecture
digests in tests come directly from these registry descriptors, independently
of the generator and certifier. The unknown/unknown descriptors are BuildKit
attestations and must not enter the runnable architecture map. This capture
used an empty temporary Docker config for anonymous reads because the host's
Desktop credential helper could not execute inside the lane.

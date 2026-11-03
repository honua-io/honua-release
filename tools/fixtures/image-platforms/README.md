These are unedited registry index bytes. honua-server was re-captured on 2026-11-03 for the
8628668 rc.2 redo pin; honua-console was re-captured for the c63b37fd final rc.2 re-pin:

```sh
docker buildx imagetools inspect ghcr.io/honua-io/honua-server@sha256:24d20a8647f6dafcfc1bef683a31f4dddf6ef4476ebb33456afa8a96b11288da --raw > honua-server/index.json
docker buildx imagetools inspect ghcr.io/honua-io/honua-console@sha256:ab453db9b2a468a05e58ebc445569fd2df84908df91967ae8d31417d6e971358 --raw > honua-console/index.json
```

The SHA-256 of each file equals the pinned index digest. Expected architecture
digests in tests come directly from these registry descriptors, independently
of the generator and certifier. The unknown/unknown descriptors are BuildKit
attestations and must not enter the runnable architecture map. This capture
used an empty temporary Docker config for anonymous reads because the host's
Desktop credential helper could not execute inside the lane.

These are unedited registry index bytes. honua-server was re-captured on 2026-10-13 for the
57782b2 integration candidate; honua-console was re-captured for the c652ef22 pin (#486):

```sh
docker buildx imagetools inspect ghcr.io/honua-io/honua-server@sha256:696f7b19b4eb4e57d341a479c3f77e371f594f34425c994d7ccc89a7b6c1fb38 --raw > honua-server/index.json
docker buildx imagetools inspect ghcr.io/honua-io/honua-console@sha256:686b104fc3f106df694782c243423c1c7110d568779abf5829514ab97349a908 --raw > honua-console/index.json
```

The SHA-256 of each file equals the pinned index digest. Expected architecture
digests in tests come directly from these registry descriptors, independently
of the generator and certifier. The unknown/unknown descriptors are BuildKit
attestations and must not enter the runnable architecture map. This capture
used an empty temporary Docker config for anonymous reads because the host's
Desktop credential helper could not execute inside the lane.

The 57782b2 server index was retrieved anonymously through the registry API and
verified against its content digest and signed nightly producer run 38402129422.
This records image identity; runtime release qualification is still pending.

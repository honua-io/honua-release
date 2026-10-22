These are unedited registry index bytes. honua-server was re-captured on 2026-10-12 for the
0e03bdd re-pin; honua-console was re-captured on 2026-10-22 for the fbe0195d pin (#775):

```sh
docker buildx imagetools inspect ghcr.io/honua-io/honua-server@sha256:dcf3296f17a456a876c23133fae50b5b32628d4ec710e8bdab58f57d2394d750 --raw > honua-server/index.json
docker buildx imagetools inspect ghcr.io/honua-io/honua-console@sha256:ab67fe43afe36069a4012522a451b6abacaede695fe395009d096dd3347d4eff --raw > honua-console/index.json
```

The SHA-256 of each file equals the pinned index digest. Expected architecture
digests in tests come directly from these registry descriptors, independently
of the generator and certifier. The unknown/unknown descriptors are BuildKit
attestations and must not enter the runnable architecture map. This capture
used an empty temporary Docker config for anonymous reads because the host's
Desktop credential helper could not execute inside the lane.

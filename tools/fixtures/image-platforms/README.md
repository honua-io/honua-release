These are unedited registry index bytes. honua-server was re-captured on 2026-10-22 for the
4709145 rc re-pin; honua-console was re-captured for the fbe0195d rc re-pin (release#775):

```sh
docker buildx imagetools inspect ghcr.io/honua-io/honua-server@sha256:041ee4aada686b1e85cfe0f9ff388e0fd66cec6ae3f6cdeec4c6dfe6199efcbf --raw > honua-server/index.json
docker buildx imagetools inspect ghcr.io/honua-io/honua-console@sha256:ab67fe43afe36069a4012522a451b6abacaede695fe395009d096dd3347d4eff --raw > honua-console/index.json
```

The SHA-256 of each file equals the pinned index digest. Expected architecture
digests in tests come directly from these registry descriptors, independently
of the generator and certifier. The unknown/unknown descriptors are BuildKit
attestations and must not enter the runnable architecture map. This capture
used an empty temporary Docker config for anonymous reads because the host's
Desktop credential helper could not execute inside the lane.

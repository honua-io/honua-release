These are unedited registry index bytes for the manifest-pinned images:

```sh
docker buildx imagetools inspect ghcr.io/honua-io/honua-server@sha256:1320e634c1f3b304043f4423780ab76202136c07c5d4bde26c178c457f3796b5 --raw > honua-server/index.json
docker buildx imagetools inspect ghcr.io/honua-io/honua-console@sha256:ab453db9b2a468a05e58ebc445569fd2df84908df91967ae8d31417d6e971358 --raw > honua-console/index.json
```

The SHA-256 of each file equals the pinned index digest. Expected architecture
digests in tests come directly from these registry descriptors, independently
of the generator and certifier. The unknown/unknown descriptors are BuildKit
attestations and must not enter the runnable architecture map. The server fixture
was captured through `resolve_trunk_candidate.Registry.request` after checking
the exact byte digest. The console fixture used anonymous Docker registry reads.

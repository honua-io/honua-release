# Canonical MCP render fixture and scenario

`mcp_render_bootstrap.py` provisions the existing authored polygon with
`regression.seed_sql` and `regression.publish_harness`. It is setup for an owned,
disposable certification candidate. It accepts only a loopback candidate URL and
uses the `honua_test` database on localhost. It records the exact image digest,
source revision, fixture and SQL digests, PostGIS geometry readback, and allocated
service/layer identities. Setup itself earns no client operation credit.

The protected source consumer must authenticate its source fetch and verify the
complete executable dependency set at one immutable protected release revision:
`mcp_render_bootstrap.py`, `regression.py`, `oracles.py`,
`mcp_plan_validation.py`, `render_png.py`, `fixture.v1.json`,
`mcp_render_validation.py`, and `mcp_render_validation.mjs`. Verify every file
before executing setup. The scenario's Python subprocess uses isolated mode and
verifies the helper, PNG oracle and fixture digests before each invocation.

After the candidate's own migrations and normal base seed have completed, the
consumer runs the verified bootstrap:

```sh
python3 -I -B "$RENDER_SOURCE/mcp_render_bootstrap.py" \
  --fixture "$RENDER_SOURCE/fixture.v1.json" \
  --candidate-source-sha "$HONUA_MCP_SERVER_SOURCE_SHA" \
  --candidate-image "$HONUA_MCP_SERVER_IMAGE" \
  --owned-disposable-candidate \
  --receipt certification/artifacts/render-bootstrap.json
```

The database password and candidate API key come only from
`HONUA_CERT_DB_PASSWORD` and `HONUA_MCP_API_KEY`. They are not written to the
receipt. Failed setup retains its phase and safe error type and must stop render
execution; there is no fallback to a discovered or preexisting layer.

The release-owned `createRenderBootstrap` entry point verifies every setup
dependency immediately before invoking that CLI. The consumer bridge delegates
to this entry point instead of owning a second setup implementation.

The release-owned `createRenderValidation` module receives a verified source
directory, immutable source SHA, the three runtime file digests, and the retained
bootstrap path. Its callback invokes `honua_render_map` through the actual
installed official SDK or Inspector. It owns three requests: authored polygon
render, reversed bbox, and an unknown layer within the allocated owned service.
It retains the complete raw results, including partial results after transport
failure. `verifyRenderProof` reruns the pinned oracle against those observations,
the original bootstrap and the candidate source/image identity.
The release-owned scenario independently inspects the running container
immediately before and after each client's authored calls. Both safe runtime
observations are retained and must match the original container ID, image ID,
digest, source revision and start time. Restart or replacement after bootstrap
fails closed. Offline controls use explicit controlled Docker executables;
canonical consumers use the actual running-container inspection.
Mismatching safe fields and image source/digest readback are emitted before
refusal so failed cases retain telemetry too. Inspection failures retain their
phase/error type and any already available container fields without stderr.

Positive judgment checks both image representations, MIME, dimensions, byte
length, bbox and allocated layer before the strict PNG oracle checks pixels
derived from authored coordinates. Negative judgment requires the matching typed
`invalid_argument` or `not_found` refusal and consistent error representations.
Style discovery and preset persistence remain separate operations.
The default `resource_link` response and subsequent artifact fetch are not
exercised by these inline opt-in requests and remain an explicit coverage gap.

The full required pytest floor runs the offline Python and Node controls through
`tools/test_mcp_render_execution.py`. Controlled synthetic PNGs are generated
independently from the authored world coordinates. They are never a canonical
producer input or substitute for a server/client run. Passing these controls
does not qualify the candidate or override any catalog, media, semantic,
requirements or global release gate.

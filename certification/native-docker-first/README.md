# Native Docker first-use attempt

`run.py` coordinates the original one-registration local first use across the published SDK/MCP installer, a real external Claude Code MCP client, the installed Console's native map browser producer, and a separate human approval. It is a live operator harness. Offline tests and a prepared attempt are never qualification evidence. Use a disposable Docker host and a **new** install and attempt directory. The runner retains the stack for inspection and does not tear down anything it did not create.

Supply a resolver-produced candidate `platform-manifest.yaml` containing a coherent published SDK/MCP pair at 0.2.7 or later, the exact public NuGet SDK, and immutable Server and Console images with source SHAs. The committed default manifest is stale and deliberately refused. Supply a clean, committed Console checkout containing `workspace-live/specs/studio-map.live.spec.ts` and `publication-witness.live.spec.ts`; `--console-sha` must be that checkout's exact HEAD. The installed Console's `/version.json` must report the candidate's Console SHA. Do not replace this with a source-built or seeded deployment.

Set `CANDIDATE`, `ATTEMPT`, `INSTALL`, `CONSOLE`, and `CONSOLE_SHA` to private absolute paths/values. The host must have Docker Compose, Node, .NET 10, Playwright Chromium, the real Claude Code client, actual model credentials and network access. The gateway URL must be a private origin reachable from the Console container; choose its host listener explicitly. Keep the independent bridge key in a protected host environment variable, for example `HONUA_NATIVE_BRIDGE_KEY`. The runner never records its value.

```bash
python3 certification/native-docker-first/run.py start \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" --install-dir "$INSTALL" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA"
python3 certification/native-docker-first/run.py install \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA" \
  --gateway-url http://host.docker.internal:7863 --gateway-listen-host 0.0.0.0 \
  --gateway-key-env HONUA_NATIVE_BRIDGE_KEY \
  --model-kind bedrock --model-name "$REAL_MODEL" --model-region us-west-2
```

The external client calls `honua_agent_inspect_local`, `honua_agent_install_local(confirm:true)` once, and status on its single published `honua-agent-mcp` registration. Its transcript is private. An uncertain installer outcome is terminal for this attempt until inspected manually; the runner will not replay the mutation. `install` starts the published native HTTP gateway as an owned process and records its PID. Check the private receipt and gateway log. The installer returns `preview-requires-human-pairing`, which is not a passed browser stage.

Complete actual human Console pairing/sign-in and capture a private Playwright storage-state file through the normal browser flow. This runner does not mint or synthesize one. Use the committed GeoJSON fixture `certification/terminal-journey/fixtures/journey-source.geojson` or another real GeoJSON with renderable, varied attributes. Then run:

```bash
python3 certification/native-docker-first/run.py browser \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA" \
  --operator-state "$PRIVATE_OPERATOR_STATE" --input-file "$GEOJSON" \
  --server-origin http://127.0.0.1:8080 --origin http://127.0.0.1:8081 \
  --route /native-first-use-map
```

The runner uses the exact manifest's public .NET SDK to create/test one managed PostGIS connection with the installer's separate workspace agent key. The Console browser then imports that file, publishes a service, observes its actual layer and feature count, asks the real native model to create the map, verifies canonical draft/readback and rendered features, makes visual/native edits, saves/reopens the same version, and uses the human Console control to submit publication. `publication-handoff.json` retains the exact operation/proposal/request/item/version/content identities. No model or runner action approves the proposal.

A **different human** approves the exact proposal in the installed Console inbox. The approval must be observed in the server's canonical proposal record; do not edit the handoff, reuse the operator's approval identity, or infer success from a chat message. After approval:

```bash
python3 certification/native-docker-first/run.py witness \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA" \
  --operator-state "$PRIVATE_OPERATOR_STATE" --origin http://127.0.0.1:8081 \
  --server-origin http://127.0.0.1:8080
```

`witness` is read-only and can be repeated while approval is pending. It uses the paired human session to read the proposal and the installer's separate `admin:read` workspace key to read the exact operation handle from the local server; the key is never sent to the browser page or written into a receipt. The handle must bind the original proposal, publication request, item and version. It then checks the exact immutable publish request, saved content hash, and an unauthenticated public route read for the original map/content hash. A proposal response without a canonical request ID blocks; no latest-list matching or resubmission substitutes it. A pass is written only after all of these reads agree. Private storage state, install `.env`, model transcript, and browser logs remain outside version control. There is no cloud apply, release promotion, or claim that this one smoke qualifies the broader P24/252-case pack or human task evaluation.

# Native Docker first-use attempt

`run.py` coordinates a joined native Console integration attempt across the published SDK/MCP installer, a real external Claude Code MCP client, the installed Console's native map browser producer, and a separate human approval. It is a live operator harness. Offline tests and a prepared attempt are never qualification evidence. Use a disposable Docker host and a **new** install and attempt directory. The runner retains the stack for inspection and does not tear down anything it did not create.

This is **not yet** the original one external-agent conversation through connection, import, service, map, and publish. The runner retains the external client's session ID and has `agent-task`, `agent-submit`, `agent-observe-import` and `agent-service` continuations for the real browser-file turn, prepared import, physical output, and connection/service/query. These stages still need actual execution against published candidates. The separate `browser` joined smoke uses the published .NET SDK and human Console UI for its connection/import/service, and a separate real native model turn creates its map. A fully matched joined receipt says `joined-native-console-integration-pass` and `oneExternalAgentConversationQualified:false`. The external client must still make and submit the same map through canonical tools and receipts, then join those to distinct human approval and audience reads. Those external map/publication stages are not yet implemented; no existing stage should be described as their completion.

Supply a resolver-produced candidate `platform-manifest.yaml` containing published SDK/MCP pins verified by the canonical client resolver, the exact public NuGet SDK, and immutable Server and Console images with source SHAs. The committed default manifest is stale and must not be used as a current candidate. The installed MCP must advertise the turn-binding capability, and the runner must observe a successful bind to the selected turn. Supply a clean, committed Console checkout containing `workspace-live/specs/studio-map.live.spec.ts` and `publication-witness.live.spec.ts`; `--console-sha` must be that checkout's exact HEAD. The installed Console's `/version.json` must report the candidate's Console SHA. Do not replace this with a source-built or seeded deployment.

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

The external client returns actual results for `honua_agent_inspect_local`, `honua_agent_install_local(confirm:true)` once, and status on its single published `honua-agent-mcp` registration. Its transcript is private. An uncertain installer outcome is terminal for this attempt until inspected manually; the runner will not replay the mutation. `install` records the running owned Compose container IDs, immutable image references and image IDs, release identity, installer connector origin, Console version SHA, and exact local Server/Console origins. Later stages refuse a replaced stack or origin. It starts the published native HTTP gateway as an owned process and records its PID. Check the private receipt and gateway log. The installer returns `preview-requires-human-pairing`, which is not a passed browser stage.

For the external-client continuation, the paired human selects the real file in Console's `/operate/resources/import/task` picker and sends a message in that same workspace conversation. Read the actual new `workspaceId` and `turnId` from the private Console context/conversation; do not invent a turn or copy file bytes into the prompt. Then run:

```bash
python3 certification/native-docker-first/run.py agent-task \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA" \
  --workspace-id "$WORKSPACE_ID" --turn-id "$HUMAN_FILE_TURN_ID" \
  --input-file "$GEOJSON"
```

This resumes the **same external-client conversation** with its single pinned `honua` MCP registration/configuration. The CLI reconnects in a new process, while the recorded session, config, artifact and installed target bindings must remain the same. The client must actually read the conversation and immutable human turn, use the connector's supported `honua_agent_connect({workspaceId,turnId})` to bind that authorized turn, and create exactly one browser-file import task with matching name, size, SHA-256 and selection ID. A pre-binding connector refuses this stage; an input mismatch or lost task reply blocks without replay. The human then opens that task in Console, uploads the originally selected file through the browser SDK, and prepares it for agent submission. The runner records the task as a partial stage, never an imported table or published service.

After the real human upload and preparation, resume that same external client again:

```bash
python3 certification/native-docker-first/run.py agent-submit \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA"
```

The resumed client reconnects to the exact original turn, reads its current authority and prepared submission receipt, calls `honua_workspace_submit_dataset_import` once with those fingerprints, then reads the actual accepted durable job. The runner checks the exact task/version/hash, turn, authority, operation, job and lease identities from tool results. A started or blocked phase is never replayed; an uncertain outcome requires separate read-only reconciliation. An accepted job is not proof that its physical import succeeded, a service exists, or a map was published.

Read the original import's progress through the same external-client session:

```bash
python3 certification/native-docker-first/run.py agent-observe-import \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA"
```

This read-only phase can be repeated while the job is pending. It verifies the same installed target, registration bytes, external-client session, authorized turn, original task, operation, job and lease. Only an actual `succeeded` job with a confirmed physical table/schema, completion receipt and nonzero feature count passes this stage. Requested destination names and accepted or running jobs cannot substitute for physical output. Failed or cancelled imports block; this phase never submits another import. Each observation keeps its own private transcript. A successful observation still does not qualify connection, service, map or publication workflows.

After that import succeeds, the human stays on the original import task and sends this **new actual Console message**: "Create a service from this imported dataset." Use its real turn ID in the same workspace. This fixed evaluator prompt witnesses the requested action in the harness; it is not a product restriction on free-form prompts. The turn must retain the original dataset-import task ID, revision and content hash:

```bash
python3 certification/native-docker-first/run.py agent-service \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA" \
  --workspace-id "$WORKSPACE_ID" --turn-id "$HUMAN_SERVICE_TURN_ID"
```

The same external client reads that conversation and turn, binds it, creates one PostGIS connection using the installer's `env:HONUA_LOCAL_DB_PASSWORD` reference, tests the connection, and publishes the exact physical table recorded by the original import. The reference resolves inside the local Server; no password or connection string is placed in a prompt. The client then describes and counts the published layer through canonical MCP tools. The runner joins original **structured MCP results** from the private client transcript, including matching schema/table, connection, service/layer IDs, Studio source ID and imported feature count. Model prose and query summaries cannot qualify the stage. A pending approval, denied operation or uncertain response blocks without approval impersonation or mutation replay. This phase does not change the service access policy or qualify audience access. It retains `oneExternalAgentConversationQualified:false`: map authoring, visual edits, save/reopen, publication and audience verification through this same external conversation remain required. The following `browser` phase remains a separate joined smoke and does not consume this service receipt.

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

`witness` is read-only and can be repeated while approval is pending. It uses the paired human session to read the proposal and the installer's separate `admin:read` workspace key to read the exact operation handle from the local server; the key is never sent to the browser page or written into a receipt. The handle must bind the original proposal, publication request, item and version. It also requires the nonempty submitting human actor captured at submission and a nonempty, different resolving human actor. It then checks the exact immutable publish request, saved content hash, and an unauthenticated public route read for the original map/content hash. A proposal response without a canonical request ID blocks; no latest-list matching or resubmission substitutes it. A joined integration pass is written only after all of these reads agree. Private storage state, install `.env`, model transcript, and browser logs remain outside version control. There is no cloud apply, release promotion, or claim that this one smoke qualifies the broader P24/252-case pack or human task evaluation.

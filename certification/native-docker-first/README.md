# Native Docker first-use attempt

`run.py` coordinates a joined native Console integration attempt across the published SDK/MCP installer, a real external Claude Code MCP client, the installed Console's native map browser producer, and a separate human approval. It is a live operator harness. Offline tests and a prepared attempt are never qualification evidence. Use a disposable Docker host and a **new** install and attempt directory. The runner retains the stack for inspection and does not tear down anything it did not create.

The runner has two explicit install modes. `external-agent` retains one Claude client session and leaves the Server native model and gateway disabled; real human Console turns are saved for that external client to continue. `joined-native` retains the historical browser smoke with a separate configured native model and gateway. These modes produce separate attempts and cannot be mixed. The `external-visual` map workflow now includes map composition, a real human visual edit and fresh prompt, same-client final style/version/publication proposal, and a read-only approval/audience witness. **No live attempt has qualified these new stages against accepted published artifacts.** The joined browser smoke still reports `oneExternalAgentConversationQualified:false` and cannot substitute for the external path.

Supply a resolver-produced candidate `platform-manifest.yaml` containing published SDK/MCP pins verified by the canonical client resolver, the exact public NuGet SDK, and immutable Server and Console images with source SHAs. The committed default manifest is stale and must not be used as a current candidate. The installed MCP must advertise the turn-binding capability, and the runner must observe a successful bind to the selected turn. Supply a clean, committed Console checkout containing `workspace-live/specs/studio-map.live.spec.ts` and `publication-witness.live.spec.ts`; `--console-sha` must be that checkout's exact HEAD. The installed Console's `/version.json` must report the candidate's Console SHA. Do not replace this with a source-built or seeded deployment.

Choose the map workflow when creating the attempt: `human-draft` retains trunk's human-owned draft and actual human publication proposal; `external-visual` retains agent-led creation, a human visual edit, an agent publication proposal, and separate source-sharing approval. Both preserve the original external session, and their stage receipts cannot be mixed. They remain separate guided development probes; neither qualifies the complete AI pilot corpus.

Set `CANDIDATE`, `ATTEMPT`, `INSTALL`, `CONSOLE`, and `CONSOLE_SHA` to private absolute paths/values. The host must have Docker Compose, Node, .NET 10, Playwright Chromium, the real Claude Code client, its actual model credentials, and network access. Only the separate `joined-native` smoke needs an additional Server-side model and native gateway. Its gateway URL must be a private origin reachable from the Console container; keep its independent bridge key in a protected host environment variable. The runner never records that value.

```bash
python3 certification/native-docker-first/run.py start --conversation-mode external-agent \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" --install-dir "$INSTALL" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA"
python3 certification/native-docker-first/run.py install \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA"
```

The external client returns actual results for `honua_agent_inspect_local`, `honua_agent_install_local(confirm:true)` once, and status on its single published `honua-agent-mcp` registration. Its transcript is private. An uncertain installer outcome is terminal for this attempt until inspected manually; the runner will not replay the mutation. `install` records the running owned Compose container IDs, immutable image references and image IDs, release identity, installer connector origin, Console version SHA, exact local Server/Console origins, and verifies the installed model/gateway mode. Later stages refuse a replaced stack or origin. External mode does not start a native gateway or configure a second model. The installer returns `preview-requires-human-pairing`, which is not a passed browser stage. For the separate historical `joined-native` smoke, start a new attempt with `--conversation-mode joined-native` and supply the gateway URL/listen host/key reference and real model kind/name at install as before.

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

The same external client reads that conversation and turn, binds it, creates one PostGIS connection using the installer's `env:HONUA_LOCAL_DB_PASSWORD` reference, tests the connection, and publishes the exact physical table recorded by the original import. The reference resolves inside the local Server; no password or connection string is placed in a prompt. The client describes the published service by name, then uses the description's actual canonical service ID to count the same layer; the service name and ID can differ. The runner joins original **structured MCP results** from the private client transcript, including matching schema/table, connection, service/layer IDs, Studio source ID and imported feature count. Model prose and query summaries cannot qualify the stage. A pending approval, denied operation or uncertain response blocks without approval impersonation or mutation replay. This phase does not change the service access policy or qualify audience access. It retains `oneExternalAgentConversationQualified:false`: map authoring, visual edits, save/reopen, publication and audience verification through this same external conversation remain required. The following `browser` phase remains a separate joined smoke and does not consume this service receipt.

## Human-owned draft workflow: canonical map, human publication and audience

This is selected by `--map-workflow human-draft` at `start` (the default).

After `agent-service` succeeds, keep the same paired workspace. In the real
Console Studio UI create a new empty map, retaining that human-owned canonical
draft as the workspace task. Send this **actual human Console message**:

> Add this service to the map I am editing, style it blue, save a version, and reopen that version for continued editing. Do not publish it.

Use that new actual turn ID. The user prompt has no internal IDs; this remains
a **guided development probe** because the controller continuation supplies a
specific bounded tool sequence and observed source reference. Natural deictic
acceptance is the separate Console interaction lane, not this runner.

```bash
python3 certification/native-docker-first/run.py agent-map \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA" \
  --workspace-id "$WORKSPACE_ID" --turn-id "$HUMAN_MAP_TURN_ID"
```

This resumes the recorded external client `--resume` conversation with the exact
original MCP config/executable and installed target. It reads the human captured
map task, adds the imported service source to that existing draft, refines only
its local color, explicitly saves a version, reads that saved identity/hash,
reopens it and attaches the reopened draft to the same canonical workspace.
It also persists a bounded assistant handoff into the same Console conversation;
that text is never a success oracle. No agent-owned substitute map, publication
or approval tool is allowed. Every dependent call must observe the prior
successful original **structured MCP result**. Saved/reopened mutation handles
must be completed, with current generations and exact immutable identities.

`honua_studio_get_version` currently returns only `versionId` and `contentHash`;
the controller therefore independently GETs the full original draft, exact saved
version and reopened draft from the installed server. All must retain the same
source, refinement and canonical body hash. Unrelated human controls, widgets,
render settings and existing source/style entries must remain unchanged. These
reads do not yet prove pixels were rendered.

An unknown client mutation result is terminal: do not run `agent-map` again. If
all MCP results were fully joined but only the independent HTTP read failed,
`agent-observe-map` can repeat those original read-only observations without
starting a model or replaying a mutation:

```bash
python3 certification/native-docker-first/run.py agent-observe-map \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA"
```

The **actual original human** must now open the exact reopened map in normal
Console Studio, review the existing saved version, choose the intended route and
public audience, and use the ordinary publication control. The human—not the
runner or model—performs this action under their own permissions. The desired
flow must produce a real **AwaitingApproval** proposal. A full-admin immediate
publication is not this governed journey and cannot qualify it.

Keep that **same original browser tab/document** open on `/studio` after the
real human proposal, with the original workspace/view and reopened draft still
selected. Supply its explicitly enabled **loopback-only** Chromium CDP endpoint.
The harness attaches read-only to that existing browser; it does not launch a
browser, navigate, create a page/context, import storage or reconnect a workspace.
A fresh Playwright `storageState` is insufficient: the workspace/view identity
and publication journal use `sessionStorage`, which `storageState` does not
preserve. Copy the exact pending proposal ID, then capture the existing tab:

```bash
python3 certification/native-docker-first/run.py agent-publication \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA" \
  --human-browser-cdp http://127.0.0.1:9222 --origin http://127.0.0.1:8081 \
  --route /external-first-use-map --proposal-id "$ACTUAL_PENDING_PROPOSAL_ID"
```

This phase is **entirely read-only**. `external_map_publication.mjs` uses
`chromium.connectOverCDP` and selects exactly one already-open same-origin
`/studio` page matching the original workspace, view, target, tenant and reopened
draft. It never connects a new workspace, creates/reviews/submits/retries
publication, changes a draft or approves. It performs only observation/evaluation
and canonical GETs; it injects no network routes that could break InteractiveServer
negotiation. It does not close the human browser, contexts or pages: the dedicated
observer process exits to disconnect its CDP transport without a browser-close
command. Missing or multiple matching tabs block and require the human to select
the actual original tab; no sessionStorage/localStorage is synthesized.

The capture requires the installed Studio draft to equal the external client's
reopened draft, exact canonical draft/version/source readback, rendered features
from that source and live blue paint. It selects the human Console's persisted
publication record by the supplied **exact** proposal/item/version/hash/route,
requires the real human-authorized canonical proposal to remain unresolved, and
checks its `requestedBy`, lack of agent proposer, and exact structured plan Diff
(`itemId`, `versionId`, `contentHash`, `route`, `visibility`). The controller then
independently reads the original operation handle and requires the same proposal
and `studio.content.create-publication-request` operation in `RequiresApproval`.
A direct-published/completed operation, another version or a fabricated proposal
cannot satisfy those checks. Capture failures may be retried because no mutation
is performed. Outputs are create-only and the final handoff is never accepted
from an operator-supplied passing receipt.

The current server deliberately creates its publication-request row only after
approval actuation. A legitimate pending response may contain only the operation
and proposal IDs, with no request ID. Capture binds their exact canonical pending
status and proposal diff to the saved item/version/hash; it does not invent a
request ID. `requestIdentityVerified:false` records that no canonical request has
yet been observed. If the journal already has a request ID, final qualification
must match it. The trusted read-only witness derives the final request ID from
the same canonical operation and joins the request and anonymous published route
after separate approval. The command ID is observed from the human Console journal,
not misrepresented as an independently queryable server command contract. The
private `external-publication-handoff.json` is an awaiting-approval reference,
not a passing publication receipt.

A **different real human** must approve the exact proposal in the installed
Console inbox. Then resume the original external client and independent witness:

```bash
python3 certification/native-docker-first/run.py agent-audience \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA" \
  --operator-state "$PRIVATE_OPERATOR_STATE" --origin http://127.0.0.1:8081 \
  --server-origin http://127.0.0.1:8080
```

This read-only phase may be repeated while approval is pending or after success.
Each attempt obtains a fresh canonical proposal/operation/request/anonymous-route
witness through the existing browser witness (it never accepts a supplied witness
file), then resumes the **same** external conversation with read-only map/version
MCP tools. The public audience is observed independently by the controller,
not inferred from the model's text or its privileged session. The original
requestedBy and nonempty distinct resolvedBy identities must agree with the
canonical record. Every earlier external stage must already have passed.

Only those actual live observations can write
`same-external-agent-with-human-governance-pass` and
`oneExternalAgentConversationQualified:true`, narrowly scoped to this guided
local Docker chain with explicit human governance. No checked-in tests or
prepared attempt sets those statuses. The separate browser smoke below retains
its original `joined-native-console-integration-pass` semantics. Broader bounded
migration/dashboard/operations/GP and natural conversation qualification remains
tracked by the canonical FOSS4G pilot profile.

## Agent-led creation and visual-edit workflow

Start a separate attempt with `--map-workflow external-visual` and `--conversation-mode external-agent`. The persisted selection cannot change after start. Missing ordinary-human/new-item authority or published tools blocks execution; no API-key or administrator substitution qualifies the managed-human RC path.

For this external-only map path, the human sends a new normal Console message in the same workspace, exactly “Create and style a map from the service I just published.” The runner reads its actual turn ID and resumes the same Claude session:

```bash
python3 certification/native-docker-first/run.py agent-map \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA" \
  --workspace-id "$WORKSPACE_ID" --turn-id "$HUMAN_MAP_TURN_ID" \
  --route /maps/native-first-use
```

The agent verifies the original service's canonical ID, published name, source and count, composes and styles its map, validates, saves and reopens a version, then attaches that exact reopened draft to the shared workspace. The runner checks the installed Server's actual anonymous FeatureServer metadata and feature-count query responses are both 401/403 before map authoring, final proposal and the later sharing request; a pre-existing public service blocks. The original source remains private through map authoring and approval. The agent deliberately stops before proposing publication. A real Console browser phase must show rendered features, use the embedded visual editor to choose a field and value from the source, save its thematic color, and submit a fresh human prompt on that same draft. Run that phase with the originating paired human's normal browser storage state:

```bash
python3 certification/native-docker-first/run.py agent-map-visual \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA" \
  --operator-state "$PRIVATE_OPERATOR_STATE" --origin http://127.0.0.1:8081
```

The visual phase refuses a Server native model, because sending the normal Console message in that configuration would invoke a second model. Its browser script uses actual UI controls, never a direct draft write or synthetic turn. It records the exact human-edited body digest, generation, field, color expression and new turn. The same external agent then reads that turn and changes only the requested opacity while preserving the human color edit; it validates, saves, reopens, attaches the final version, and opens the exact public publication proposal:

```bash
python3 certification/native-docker-first/run.py agent-map-finish \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA"
```

A separate actual human approves the retained map proposal in the installed Console inbox. Only after its real operation and publication request succeed, the originating paired human sends a **new** normal Console turn that explicitly asks to expose the original source service for anonymous read only:

```bash
python3 certification/native-docker-first/run.py agent-map-share-request \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA" \
  --operator-state "$PRIVATE_OPERATOR_STATE" \
  --origin http://127.0.0.1:8081 --server-origin http://127.0.0.1:8080
```

The original external client binds that new human turn, re-reads the exact original service, and invokes the canonical service policy MCP tool with anonymous read enabled and anonymous write disabled. The map approval does **not** approve this separate service policy. If the policy returns a durable `RequiresApproval` handle, a different actual human must resolve that second proposal through the installed inbox before audience verification; do not replay an uncertain policy mutation:

```bash
python3 certification/native-docker-first/run.py agent-map-share \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA"
```

The originating human then runs the read-only audience witness:

```bash
python3 certification/native-docker-first/run.py agent-map-audience \
  --manifest "$CANDIDATE" --attempt "$ATTEMPT" \
  --console-root "$CONSOLE" --console-sha "$CONSOLE_SHA" \
  --operator-state "$PRIVATE_OPERATOR_STATE" \
  --origin http://127.0.0.1:8081 --server-origin http://127.0.0.1:8080
```

It first re-reads the exact service policy operation and, when required, its separately approved proposal; anonymous requests are not issued while that approval is pending. It then joins the real map proposal/operation/request/item/version/content identities, distinct approver, authenticated Studio preservation of the final map, and the original layer's anonymous metadata and feature count. A separate browser context without operator state must load the exact published artifact through the Console public viewer, receive successful partition-bound source data, and render actual features in the read-only Studio SDK view. Only this complete live join may set `oneExternalAgentConversationQualified:true`; source and offline tests are never that evidence. The following `browser` phase remains a separate joined smoke and does not consume this service receipt. Further UX-008 visual edit, undo/redo, stale/view-switch and thematic error/conversion journeys still require separate live qualification.

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

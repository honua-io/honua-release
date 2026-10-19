# Authored map-package creation and fresh resource readback

The release-owned scenario in `mcp_map_package_validation.mjs` owns actual
installed-client process launch and wire observation. It keeps the canonical producer's
executable scenario and pure oracle outside the standard repository.

The consumer authenticates one protected immutable release commit containing
the complete existing eight-file render fixture/setup source set plus
`mcp_map_package_validation.py`, `mcp_map_package_validation.mjs` and
`mcp_candidate_wire.mjs`, `mcp_fixed_client_launcher.mjs`,
`mcp_fixed_sdk_runner.mjs` and `mcp_fixed_client_manifest.v1.json` (fourteen files). It checks
all bytes before module import, setup and each client invocation. This scenario
reuses the genuine seed/publish bootstrap and independently read PostGIS polygon,
canonical service/public-layer identity and actual source/image/container
binding; it cannot substitute a guessed fixture service.

`createMapPackageValidation` receives the verified directory, full source SHA,
eight runtime hashes (`mcp_map_package_validation.py`, `mcp_render_validation.py`,
`render_png.py`, `fixture.v1.json`, `mcp_candidate_wire.mjs`,
`mcp_fixed_client_launcher.mjs`, `mcp_fixed_sdk_runner.mjs`,
`mcp_fixed_client_manifest.v1.json`) and retained bootstrap path. Its
`mapPackageScenarios(record, identity)` calls actual
`honua_create_map_package` with explicit authored bindings, selector IDs, filter,
metadata and the polygon's world-coordinate envelope. It also sends a reversed
view and duplicate binding and requires typed invalid-argument refusals. A
separate fresh anonymous creation call must reach the actual tools/call wire
request and return typed unauthenticated refusal without data.

The returned package must retain all authored selectors and geometry, normalize
integer EPSG4326, supply server-owned format/draft discriminators, and carry a
matching package ID/URI and aware creation timestamp. Template/style/theme IDs
are selector references only: this proves neither registry resolution nor style
application.

The identity supplies the installed `node_modules` root, canonical client
selector/version, exact candidate binding and private expected credential; it
cannot provide an HTTP callback or client diagnostics. The fixed release launcher
checks the named root package/version and every package file against
`mcp_fixed_client_manifest.v1.json` before and after each invocation. The manifest
is derived from the SDK 1.32.1 and Inspector 2.10.1 npm tarballs only after SHA512
integrity authentication. It launches `process.execPath` with either the fixed
release-owned SDK runner or the package's verified public Inspector launcher,
which imports its verified CLI delegate in that process. Each invocation gets a
new child and session. SDK arguments/credential travel on private stdin; Inspector
uses an empty private home/catalog/secret store and a restricted environment.
Raw argv and credential/session headers are never included in receipts.

The parent records its actual child PID, exit code/signal, timeout and original
bounded stdout/stderr. Its launch receipt binds those observations to the fixed
manifest/launcher/SDK-runner hashes, verified package file set, endpoint, method,
arguments and authority mode. The pure oracle checks the receipt and initialized
client identity against those protected source files and compares the output to
original server wire bytes. Package tampering, a merely claimed client version,
an alternate callback, missing launch observations or changed child output cannot
supply a valid invocation through this producer.

This is a trusted producer boundary, not cryptographic process attestation:
JSON or a wire clientInfo string alone cannot prove execution. Qualification
requires the protected producer/workflow/source/artifact provenance, maintained
`npm ci` lock/integrity authentication for transitive dependencies, and the
retained installed-client run. Offline controlled package/manifest fixtures are
explicitly nonproduction source and cannot replace the protected real manifest.
The real manifest rejects their alternate HTTP entry bytes before launch.

The release-owned transparent observer listens on a fresh IPv4 loopback port
and forwards actual client traffic only to `http://127.0.0.1:8080/mcp`. It
retains original bounded JSON/SSE request/response bytes and their hashes,
exact JSON-RPC IDs, method and URI/arguments, fresh initialization and session
hashes, and credential presence plus salted scrypt fingerprint. Raw API keys, Authorization, cookies and
session headers are never retained. One random salt per journey permits
expected/actual credential comparison without retaining the credential or a fast
password hash. Anonymous calls must have none of these
credentials; all seven sessions must be distinct, including the creator and
fresh readers. Before/after actual container identity checks bind that fixed
upstream to the owned candidate.

Anonymous `tools/call` creation must return the canonical tool result with
`isError: true`, `status: error`, `code: unauthenticated`, and
`requiresReauthentication: true`, without package identity or creation data.
Its text and structured representations must agree. This differs from the
JSON-RPC refusal used by `resources/read`; neither can be substituted for the
other or for local Inspector discovery failure. Refusing creation fields in a
denied result does not prove absence of hidden persisted side effects; an
independent state census or readback is a separate qualification obligation.

The pure oracle correlates public client output with the original server wire
response. Inspector 2.10.1 replaces numeric JSON-RPC error code/data with its
own CLI diagnostic. Its original stderr is retained separately and must
correlate with the response; the typed refusal is explicitly **server wire
observer evidence**, not a claim that the CLI exposes those discarded fields.
The pinned 2.10.1 public error handler uses exit 1 with `error` for generic RPC
errors and may use exit 3 with `auth_required` for authentication. Exit 2 means
`no_app` and cannot prove a server resource refusal.
A local error string, assumed HTTP status, copied tool result or synthesized
typed error cannot supply proof. The authored reads require:

- The newly returned URI resolves the same draft with no deployment edges.
- An uncreated sibling URI returns `-32002` / `not_found` without data.
- The created URI without credentials returns `-32000` / `unauthenticated` with
  `requiresReauthentication: true` and no data.

Each journey inspects the actual running container immediately before and after
client calls. Full and partial original observations, including mismatching
runtime telemetry, are retained as `.map-package.raw.json`. The pure Python
oracle rejudges the original requests, selectors, geometry, readbacks, refusals
and runtime identity before either governed row receives a proof. Missing or
fabricated proof cannot select scenario-generation mode during rejudgment.

Draft persistence has an explicit boundary: current `InMemoryPackageDraftStore`
is bounded pre-publish scratch on the creating replica. Restart/another replica
can return not-found; this slice does not claim durable save, promotion,
published deployment or live-model composition. Restart behavior, if exercised,
must be reported separately. The resource currently projects lifecycle/identity
only; it does not read back the full saved composition body. Authored property
checks therefore concern the created package output, while the independent
fresh resource read proves that its identifier was recorded and resolves.

The controlled Python/Node fixtures are offline controls only. The producer may
never use their responses or executable Docker fixture. Both actual clients,
all 82 governed rows, catalog/version/requirements policy and global behavior
gates remain required. The current catalog's anonymous-public resource policy
and selected client versions differ from this authenticated package journey
and the installed client cohort; this slice does not change or waive those
global failures. A historical-image operation run adds adapter evidence
only and cannot qualify the current release candidate.

The public Inspector CLI currently refreshes discovery before tools/call. No
supported anonymous-readable workflow view in the inspected server includes
`create_map_package`; the reserved full view requires authentication. A local
`tool_not_found` without the actual anonymous tools/call observation is therefore
an unresolved qualification/design failure under release issue #695, never
server refusal evidence. Completed earlier resource observations and the actual
CLI diagnostics remain retained when this last probe fails.

To reproduce the committed client manifest, obtain the exact npm archives with
`npm pack @modelcontextprotocol/sdk@1.32.1` and
`npm pack @modelcontextprotocol/inspector@2.10.1`, then run
`python3 tools/generate_mcp_fixed_client_manifest.py --sdk <sdk.tgz> --inspector <inspector.tgz>`.
The generator authenticates both SHA512 values before reading package metadata
and file bytes; its stdout must equal the committed manifest. These package
versions describe the installed consumer under test and do not change or satisfy
the frozen catalog's separate cohort-selection gate.

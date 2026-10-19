# Authored map-package creation and fresh resource readback

The release-owned scenario in `mcp_map_package_validation.mjs` delegates actual
requests to installed-client callbacks. It keeps the canonical producer's
executable scenario and pure oracle outside the standard repository.

The consumer authenticates one protected immutable release commit containing
the complete existing eight-file render fixture/setup source set plus
`mcp_map_package_validation.py`, `mcp_map_package_validation.mjs` and
`mcp_candidate_wire.mjs` (eleven files). It checks
all bytes before module import, setup and each client invocation. This scenario
reuses the genuine seed/publish bootstrap and independently read PostGIS polygon,
canonical service/public-layer identity and actual source/image/container
binding; it cannot substitute a guessed fixture service.

`createMapPackageValidation` receives the verified directory, full source SHA,
five runtime hashes (`mcp_map_package_validation.py`, `mcp_render_validation.py`,
`render_png.py`, `fixture.v1.json`, `mcp_candidate_wire.mjs`) and retained bootstrap path. Its
`mapPackageScenarios(call, readFresh, record, identity)` calls actual
`honua_create_map_package` with explicit authored bindings, selector IDs, filter,
metadata and the polygon's world-coordinate envelope. It also sends a reversed
view and duplicate binding and requires typed invalid-argument refusals.

The returned package must retain all authored selectors and geometry, normalize
integer EPSG4326, supply server-owned format/draft discriminators, and carry a
matching package ID/URI and aware creation timestamp. Template/style/theme IDs
are selector references only: this proves neither registry resolution nor style
application.

Both `call(tool, arguments, {endpoint})` and
`readFresh(uri, {authenticated, endpoint})` MUST create a new session using the
real installed SDK, or a new public Inspector CLI process, at the supplied
observer endpoint for every call. The identity includes the canonical client
name, installed version and private expected credential. Each callback returns
actual invocation diagnostics: SDK public output/error, PID, URL and public
session hash; or Inspector PID, URL, original stdout/stderr, exit code/signal
and timeout state. It never substitutes an alternate HTTP client.

The release-owned transparent observer listens on a fresh IPv4 loopback port
and forwards actual client traffic only to `http://127.0.0.1:8080/mcp`. It
retains original bounded JSON/SSE request/response bytes and their hashes,
exact JSON-RPC IDs, method and URI/arguments, fresh initialization and session
hashes, and credential presence plus salted scrypt fingerprint. Raw API keys, Authorization, cookies and
session headers are never retained. One random salt per journey permits
expected/actual credential comparison without retaining the credential or a fast
password hash. Anonymous calls must have none of these
credentials; all six sessions must be distinct, including the creator and
fresh readers. Before/after actual container identity checks bind that fixed
upstream to the owned candidate.

The pure oracle correlates public client output with the original server wire
response. Inspector 2.10.1 replaces numeric JSON-RPC error code/data with its
own CLI diagnostic. Its original stderr is retained separately and must
correlate with the response; the typed refusal is explicitly **server wire
observer evidence**, not a claim that the CLI exposes those discarded fields.
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

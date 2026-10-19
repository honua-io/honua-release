# Canonical style client proof

This producer covers `get_style` and `apply_style_preset` through both actual
installed canonical clients. Its four implementation paths do not qualify the
unchanged 82-row catalog. Per-operation authorization, the authenticated client
cohort, global behavior and exact-candidate replay remain required.

The independent fixture consists of two authored opaque polygon fill styles.
Setup creates them through `POST /ogc/styles` with a bounded `X-Style-Id`, retains
the actual 201 body and Location, and never directly inserts a style association.
The existing genuine area seed/publication supplies its allocated canonical
service, public layer index, physical storage layer and connection identity.

Source contract checked at server `3c5250084957fae0187aa1933509c7e2d2a9cc82`:

| Source | Expected observation |
| --- | --- |
| `OgcStylesEndpoints.CreateStyle` | POST accepts canonical MapLibre JSON; 201 `id` and escaped Location are the created identity. |
| `GetStyleTool` | `honua_get_style` lists catalog entries, resolves an exact styleId or a public service/layer's primary association, and inlines only the selected encoding. Large text can be a prose summary; structuredContent remains authoritative and any JSON text must agree. |
| `ApplyStylePresetTool` | `honua_apply_style_preset` accepts canonical serviceId/public layerId/styleId/dryRun. Completed output carries the same canonical IDs, actual operation handle, applied/dryRun and styleVersion. Approval/denial/failure never counts as an application. Warning means reconciliation is pending. |
| `StylePresetExecutor` | Association commit is authoritative; graph reconciliation follows it. A successful response is insufficient without independent graph and catalog agreement. |
| `PostgresStyleCatalog.AssociateLayerAsync` | Physical layer association has one ordinal-zero primary; promoting a different style demotes old primaries. Repeating the primary preserves associations. |
| `MetadataV2StyleResourceFactory` | A bounded ASCII styleId maps to graph resource `style-<styleId>` with canonical mapbox-style body. |
| `RenderMapTool` / `AppliedStyleRenderSupport` | Output names applied styleId and styleRendering=applied; independently derived PNG interior samples must have the authored opaque fill color, with transparent exterior samples. |
| migration042 | `honua.styles` and `honua.layer_style_refs` are durable tables; physical layer ID is distinct from the service's public index. |

Historical server `3ecd214c1ce20e96ad4911b6f8447055dee9089d` has the same
apply/read/persistence contract, but omits the later optional `durable` output.
An omitted durable field is unknown operation-instance persistence, and `durable=false`
explicitly identifies the supported Redis-off direct handle whose status cannot be
read back. This field does not describe catalog association persistence. The proof
retains the actual disclosure for dry-run/apply/replay without inferring a durable
handle. Association durability is established only by the independent committed
state, fresh read/render and restart observations. See `McpApplyStylePresetOutput.Durable`
and `RedisOffDirectStylePresetHostTests` at the source revision above. No selected
image has yet established this new journey.

The sequence retains setup, initial state, catalog/read, dry run, unchanged state,
apply, independent committed state, fresh layer read and colored render, replay,
unknown style/encoding/target with state census, anonymous invocations for each
operation with state census, exact owned-container restart, final independent
state and fresh client read/render. Every invocation uses a new actual client
process/session. The public Inspector must discover the real supported setup view
without credentials; local tool-not-found cannot stand in for a server refusal.

The catalog census includes every catalog record, including both authored entries,
existing associated styles and unrelated entries. Every referenced graph style must have exactly one resource object whose
name, version and canonical MapLibre body match its independently observed catalog
row, including a primary demoted to secondary by the new application. The full
catalog record census (title/description/body/drawing metadata/version/timestamps/
revision attribution/summary) must remain unchanged from initial state through
apply. Pre-existing refs must survive; nonprimary ordinals remain unchanged and
an old ordinal-zero primary moves to the previous maximum ordinal plus one.
Neither authored style may be associated before the tested application. The first
application must advance the graph revision strictly because it introduces that
new association; no exact +1 is assumed. Same or regressed revisions are refused
even when every later checkpoint coherently repeats that invalid revision.

The raw census is validated for multiplicity and binding before any ordering
normalization. Catalog record order is not semantic. Ref and graph sequences
must preserve nondecreasing ordinals and the sole primary; only permutations
within the same nonzero ordinal are equivalent because the production catalog
locale-orders those ties. Comparisons normalize these ties without dropping any
identity, ordinal, catalog field, graph body or revision. Mixed-case/punctuation
and equal-secondary-ordinal controls reject different-ordinal swaps and missing,
duplicated or changed entries.

All SQL reads of catalog, associations, metadata mapping/revision and authored
area rows including full GeoJSON geometry use one statement/MVCC snapshot. The independent rectangle oracle permits ring start/orientation changes only, with no extra vertices, holes or same-bbox distortions. Every negative compares full relevant
state before/after; checking only response fields does not prove nonmutation.
Read-only, dry-run, refused, anonymous and restart checkpoints require exact committed state and revision equality against the preceding census. Successful replay alone may advance graph revision because the current executor always reconciles the graph; its complete catalog, association, graph projection and feature state must remain equal. The observer reads only the owned fixture database with an isolated psql
environment. Restart is permitted only for the explicitly owned disposable
candidate after source/image/full-container identity inspection.

Original wire bytes, package/launcher identity, child outcome, session hashes and
runtime observations remain under the trusted protected-source/workflow/artifact
boundary. JSON does not independently attest a process, socket or database.
Diagnostics must contain no raw credential or session headers. Partial refusal
and unsupported prerequisites remain raw failures rather than fabricated proof.

Refs release705, release652 and release695. Analysis/GP execution, genuine
publish_result artifacts, client-model planning and cancellation remain separate
obligations; fixture planning output earns no real-planning credit here.

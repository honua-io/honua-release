# Canonical MCP location execution

This producer covers six existing rows: `geocode_address`,
`geocode_addresses`, and `solve_route`, each through the installed official
MCP SDK and Inspector. It adds no rows and qualifies none through source tests.

The independent expected values describe the two authored addresses in server
`tests/seed/sdk-protocol-v1/geocoder.csv` and the reviewed 9-vertex/12-edge
network in `docker/routing/init-routing.sql`. Their source hashes are recorded
in the location fixture contract; the frozen core fixture stays unchanged.
Candidate provisioning must independently bind the actual main database,
extensions, migrations, imported rows, effective configuration and immutable
server image. A source hash alone does not attest deployed setup.

Geocoding explicitly selects the local provider, checks both address strings,
longitude/latitude and EPSG:4326, and enforces the requested one-candidate limit.
The local provider supports EPSG:4326 only. Batch geocoding checks ordered,
trimmed inputs, a duplicate input, a real unmatched address, per-item failures,
and exact success/failure counts. It refuses omitted entries and external or
mock provider output.
Single-address calls also check included, excluded and mixed-case multiple
country restrictions before the one-result bound; the batch exclusion must
retain four clean per-item failures. An accepted but ignored `countryCodes`
parameter is a qualification failure, even if the other positive calls work.

Routing requires the pgRouting provider and three ordered stops along the
reviewed lattice. The oracle checks four real legal edges, the intermediate
stop, both endpoints, four unit minutes and consistent directions. Length must
be within 1% of an independent spherical calculation; this checks gross unit
or length errors, not high-precision ellipsoidal equivalence. Separate positive
calls exercise EPSG:3857 input and output. Invalid counts, missing ordinates,
invalid CRS and geocode limits require typed refusals.

Each operation is attempted independently through the fixed installed client
launcher, with actual initialize/invoke wire bytes, fresh-session and private
credential forwarding checks. Anonymous calls require an actual typed server
refusal; a local Inspector catalog refusal alone cannot stand in. One founder-
approved exception (D3, 2026.1) applies on the MCP Inspector lane only and only
to `honua_geocode_addresses` and `honua_solve_route`: the candidate observer
records the anonymous default-view `tools/list` walk through the canonical
Inspector, every page is a complete delivered 200 JSON-RPC result on the fresh
anonymous session, the tool is absent from the union of listed names, no
`tools/call` was sent, and the Inspector exited with exactly code 5 and stderr
`tool_not_found` naming that tool. The proof then reports
`anonymousRefusalEvidence: inspector-anonymous-unlisted`; otherwise it is
`server-refusal`. If either tool is listed, Inspector must send `tools/call` and
the server's actual refusal is required. The SDK lane and every other tool
(including `honua_geocode_address`, `honua_create_map_package` and
`honua_create_app_package`) still require the server's actual refusal. Candidate runtime
inspection brackets every operation. Incomplete observations, missing setup,
missing entitlements, client substitution and source drift remain non-pass.
No edition policy or licensing bypass is introduced.

The thin consumer must fetch protected source/dependency hashes, invoke these
paths through both clients and rejudge their retained observations for the
exact operation under the protected authorization policy. Actual candidate
execution is still required by release #123/#652. Offline controlled processes
and synthetic oracle vectors prove the test boundary only; they are never
canonical-client or candidate certification evidence.

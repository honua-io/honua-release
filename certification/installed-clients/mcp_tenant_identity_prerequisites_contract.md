# Owned tenant identity prerequisites

This source is a prerequisite for the original deployment/workspace proof in
Refs #729. It does not qualify any of the original four operations or eight
client rows, and does not replace the complete tenant/owner/role proof. All
82 candidate rows, original policy and frozen release bindings remain unchanged.

## Existing callers

`admin` and `limited-read` keep their existing permissions and public receipt
shape. `verify_receipt(receipt, candidate_runtime, profile)` retains its terminal
normal-revocation requirement. No tenant argument adds any field to the launcher,
SDK input, retained request or wire output. The existing protected bundles remain
immutable; consumers select a new protected dependency closure explicitly.

## Exact workspace execution

The additional `workspace-reader` profile requires `service_id`, independently
derived by the complete producer from its actual owned published-layer bootstrap.
Its only permissions are `read:*` and `execute:{service_id}/geometry.area`.
Constructor and pure verifier accept `service_id=`; the CLI accepts `--service-id`.
Other profiles refuse that option. Wildcard, path, empty and unrecognized service
values refuse. A read grant alone is not execution authority or an own-workspace
positive, and the profile cannot be substituted for a tenant/owner observation.

## Explicit tenant transport and tested posture

The launcher and wire observer accept only the named optional `tenantId` field,
which maps to the existing normal `X-Honua-Tenant` product header for SDK request
initialization and the Inspector's public multiple-header CLI option. Anonymous
calls cannot request it. The observer verifies every initialize, invoke,
discovery and cleanup request, refuses unrequested or wrong tenant context and
known alias/extra Honua authority headers, and forwards the actual header.
The Python oracle requires the same authored tenant on original wire records
and the fixed client launch. The header is not by itself effective-tenant proof.

`mcp_deployment_identity_configuration.py` declares the owned fixture posture:
MultiTenancy enabled, default tenant `public`, all three explicit admin roles
(`admin`, `multi_tenant_admin`, `platform_admin`) and Studio end-user authorization
enabled. This is explicit test authorization configuration, not an unchanged
production default. The primary tenant binds the authored 12-hex journey nonce.
A fixed read-only Docker inspection projects only these safe environment values;
unreviewed or aliased values in either authorization namespace refuse. The
original declaration/hash and exact container/image readback are rejudged. The
whole environment and its credentials are never retained. This observation does
not claim that a principal belongs to a tenant: actual captured security context,
resource stores and positive/denied reads remain mandatory in #729.

Normal admins retain cross-tenant bypass. The intended denied caller is a normal
managed `workspace-reader` with the non-bypass `scoped-api-key` role. Its own
Deployment and Workspace reads must succeed before its reads of the primary
caller's resources can establish isolation. Distinct fingerprints alone cannot
establish distinct principals, owners or tenants.

## Normal role fixture

`ManagedRoleFixture` uses only the normal fixed-loopback role API: census,
creation, exact permission update, role/permission readback and deletion. It
refuses any pre-existing `scoped-api-key` role. The fresh role has only
`Deployment/Read/*` and `Workspace/Read/*`; it grants neither admin nor
scoped-admin-read bypass. The marker binds the owned container and configuration
hash. Independent read-only PostgreSQL observations retain original bytes and
exact role ID/name/marker, non-built-in status, grants and tombstone state.
Ownership is checked before any permission PUT or DELETE, including malformed
creation results. Unknown or foreign ownership never authorizes cleanup.

The caller must retain `fixture.receipt` in its durable nonsecret output on every
exit, including exceptions, and invoke owned cleanup in its normal `finally`
path when an owned ID was established. Never serialize the fixture instance or
private credential. Failures remain in the receipt even if a later owned cleanup
retry succeeds; the pure final verifier refuses such receipts. A partial or
ambiguous creation does not grant permission to delete an unverified ID, and
cannot produce a passing final receipt. Actual producer integration and durable
partial-exit handling remain required in #729.

`verify_role_receipt` rejudges the original six normal API exchanges and four
independent database snapshots, exact tested configuration/candidate, final
owned cleanup and retained failures. It is not a boolean authority adapter.
The SQL service controls use explicitly controlled minimal DDL to test the real
read query, immutable ownership refusals, tombstone projection and read-only
transaction enforcement. They do not prove migrations, actual role API execution
against a candidate, or any deployment/publication lifecycle.

## Delivery and remaining work

Offline controls use clearly labeled alternate SDK/Inspector modules only in
temporary control manifests. Production package manifests and hashes are
unchanged. Offline positives exercise both fixed child paths and original Python
wire/launch adjudication; mutations cover missing/wrong/unrequested tenant,
aliases, cleanup, malformed options, ownership before mutation, original bytes,
configuration and retained failed cleanup. Real SQL query controls are separate.
None are installed-client or candidate receipts.

After this prerequisite lands normally, #738 must immediately integrate actual
managed creation/effective-permission/Redis identity receipts for both callers,
role/configuration observations, real owned publication/workspace positives,
effective tenant and distinct owner bindings, foreign denials, anonymous/fresh
sessions and restart/readback. Its final producer and thin consumer must require
all semantic and identity proofs together. Missing normal fixtures remain
NONPASS; this prerequisite cannot close #729.

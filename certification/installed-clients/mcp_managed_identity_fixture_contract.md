# Normal managed identity fixture contract

This shared fixture serves the original MCP producers. It creates only the normal
`admin` or `read:*` managed-key profile through the product API, reads effective
permissions, independently reads the actual Redis key record and authentication
indexes, and revokes the owned key through the normal API. It grants no release
or cloud authority and does not qualify a primary journey.

The runner supplies one 32-hex `credential_salt` to this helper and the actual
wire observer. The public `identity.credentialFingerprint` must equal the actual
wire fingerprint. A bootstrap password has no managed key ID and cannot replace
this binding. The canonical actor, Studio owner and Workspace owner are derived
separately from the actual managed key ID using the corresponding server rules.

`ManagedIdentityFixture.create()` writes one exclusive mode600 private journal;
key plaintext and the admin credential never enter the public receipt. The
normal create response is retained only by hash because it contains a one-time
secret. Effective-permissions and revoke responses retain their original
nonsecret bytes. The independent managed record retains its original bytes,
including the server's SHA256 of a high-entropy generated key, never plaintext.
The public receipt records the exact owned container/source/image tuple.

Every revoke first independently verifies the key ID, unique owned name, exact
profile permissions and, when known, private-key hash. All ownership checks
precede every revoke mutation, including partial-create census recovery. A
foreign, malformed or ambiguous record is not cleanup authority. Failed or
ambiguous cleanup remains explicit in a durable receipt. A cleanup retry may
remove an owned credential but cannot erase failure history or grant proof.

The CLI supports `create` and `revoke`, explicit profile/private and receipt paths,
shared salt, exact source/image, and owned disposable candidate acknowledgment.
Its admin credential comes only from `HONUA_MCP_ADMIN_API_KEY`. Consumers read
transient key material privately, execute their actual named-client requests,
then revoke before final proof admission. `verify_receipt(receipt,
candidate_runtime, profile)` rejudges the original API/store bytes and complete
active-to-revoked lifecycle and returns the validated identity.

The helper does not infer tenant context or prove normal data-read permission.
The consumer must positively exercise the actual `read:*` authority before using
an Ops denial as evidence. Tenant/owner isolation additionally requires the
normal effective tenant/configuration observations, real owned-resource reads,
and the intended foreign-resource denials. Different key fingerprints alone do
not prove different tenants or owners. Missing prerequisites remain nonpassing.

Offline API/store controls and workflow-owned Redis controls are source evidence,
not installed-client, fresh-candidate, runtime lifecycle or tenant qualification.
The original 82 MCP rows and their required facets remain unchanged.

Refs honua-io/honua-release#695 (actual authority/cohort qualification remains).
Refs honua-io/honua-release#727 (Ops findings/events producer integration remains).
Refs honua-io/honua-release#728 (grounding/status producer integration remains).
Refs honua-io/honua-release#729 (deployment/workspace tenant integration remains).

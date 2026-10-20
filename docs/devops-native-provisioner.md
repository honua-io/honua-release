# Published native DevOps provisioning prerequisite

Owning source slice: release #751. Original acceptance remains release #129 and
DevOps #183: all six live AWS fault cells, durable approved cleanup, independent
resource fate, and the unchanged #123 terminal journey remain required.

`tools/devops_native_artifact.py` consumes a separately reviewed expected native
identity: fixed repository, immutable published tag, exact source SHA, RID,
release and asset IDs, and original archive SHA256. It authenticates the normal
release/tag metadata, archive/checksum and the publisher's normal provenance
with the official `gh attestation verify` signature/certificate verifier. It
never accepts caller-supplied verification JSON as authority. A different normal
authenticated provenance route requires an explicit implementation/review; this
source slice introduces no independent certification or publication gate.

The expected identity is an execution input, **not** a manifest component or an
adoption proposal. This tool does not select a version, publish, tag, mint,
change credentials, or mark a native cohort admitted. Without an actual approved
cohort, results remain unqualified. The published `v2026.1.0` source `905444e1`
predates DevOps #225/#226/#227; observing that binary is historical source/protocol
evidence only and cannot qualify those corrections.

Actual read subprocess stdout/stderr and downloads are streamed into bounded files; over-limit or timed-out owned read subprocesses stop with retained partial streams. No native/cloud actuator runs in these controls.

Archive staging uses a request-owned private directory on real disk. Only finite
regular files/directories are copied; paths (including portable Windows normalization hazards), duplicate members, links, special
files, every directory/root entry, zero directory sizes, bounds and native RID headers are checked. Archive, provenance, original
registry metadata, failures and the complete installed-file seal remain retained.

`tools/devops_native_mcp.mjs` invokes that fixed intake helper itself before
launching the binary; a user-written seal cannot bypass intake. It uses the
existing installed official MCP SDK and lock from
`certification/sources/geospatial-mcp`. Before import, it compares the complete installed SDK file/directory closure to the normal npm tarball authenticated by that unchanged lock's SHA512 integrity; same-version modified/extra/missing JavaScript and resolution outside the exact package are refused. It rejudges those original tarball bytes and installed files after the session. It uses fixed `--mcp`, private working/home/temp
directories and an explicit read-only environment. No ambient GitHub/AWS/admin
credentials, Terraform checkout or external command are forwarded. It performs
real initialize and exhaustive `tools/list`. An optional fixed read-only request
requires an independently supplied exact tool-schema digest. Missing expected
schema is a refusal; the tested runtime never manufactures its expected catalog.
The full tools metadata is compared when a digest is supplied.

The current fixed requests are `describe_environment` and
`provision_infrastructure(action=inspect-cleanup)`. Inspection IDs must be the canonical `urn:honua:provisioning:` plus32 ASCII hex digits produced by DevOps; an arbitrary short label is refused. Source mode is plan with
Observe tier. The API base points at closed loopback port 1, so this isolated
source probe cannot be an installation or readiness proof. Plan/apply/destroy,
install handoff and its verification are explicitly refused by this first
slice. There is no caller `approved=true` escape. Actual candidate/IaC/fixture/
auth/cut, short-lived identity, backend/state and one-use signed approval joins
must be implemented and authenticated by the continuing producer before any
actuation is enabled. Canonical DevOps/IaC approval, exact-plan, execution,
backend and handoff receipts remain unchanged; this probe replaces none of them.

The cancellation helper uses an actual observed outbound tools/call request ID
and awaits the official SDK's public standard notification. Its pure controls
prove routing only. Actual matching/wrong-ID behavior, owned executor cessation
and independent cloud state must still be observed in the original six scenarios.
Local waiter cancellation, notification acknowledgement, and process startup
alone cannot establish that proof. Stock .NET client-token interoperability
remains independently open in DevOps #228.

To invoke the source probe, an operator supplies a private request JSON with
`expectedFile`, `outputDirectory`, `expectedTools` and `readRequest` (null for
initialize/discovery only), then runs the product-independent proof adapter:

```text
node tools/devops_native_mcp.mjs /absolute/private/request.json
```

All output remains `candidateQualified:false`. Missing provenance, unpublished
or mismatched bytes, missing schema/context, incomplete discovery and cleanup or
file drift retain public failure observations and return nonzero. Pure archive
and routing fixtures are source controls, never actual published-install proof.
A real historical published binary may be observed separately with explicit
source identity, without granting candidate or six-cell credit.

The next required integration is the complete six-cell producer/judge: ordinary
approved destroy; cancellation after an apply resource exists; real provider
failure during apply; cancellation during destroy; transient destroy failure
plus restart/directory loss and reconciliation without overlapping executors;
and an explicitly retained external resource. Join original plan/approval/claim/
receipt bytes, remote backend/state lineage/serial, exact candidate and short-lived
identity, provider-native inventory (unknown read is nonpassing), verified handoff
and unchanged #123 operation/approval/publication/URL identities. This source
slice does not complete #129, #183, the AWS journey, or any MCP candidate row.

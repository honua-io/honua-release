# SDK minimum server derivation

Applies to release #233, target version model section 9 and compatibility ledger
section 9.3, including the 2026-09-04 release-integrity review.

For a given SDK artifact, the minimum server version is the maximum of the
server introduction versions of every capability it requires, as declared by
the protocol/capability manifests that artifact consumes. Pin each manifest's
repository, commit, path, canonical JSON SHA-256, content, and required capability
IDs in the release lock. Optional features negotiated at runtime do not raise
the base floor; document their separate required capabilities.

An API/protocol version, SDK version, calendar platform label, current server
assembly version, or successful test against one recent server is not evidence
of the earliest server that implements a capability. Publishers must supply
`minimumServerVersion`, `versionModel: semver`, and an introduction evidence URI
and digest for each required capability. Missing introduction evidence means
**unqualified**, never a guessed numeric baseline. Legacy CalVer identities need
an explicit publisher mapping before they can be compared with SemVer floors.

The component's `serverCompatibility` lock entry contains `manifests`, the derived
`minimumServerVersion`, and `declarations`. Each declaration pins its source
revision, path, byte SHA-256, and declared `minimumServerVersion`. Runtime constants,
package metadata, public API snapshots, and documentation must agree. Validate
the bytes at each pinned source before accepting the declaration into the lock.
`verify_sdk_baseline_sources.py` reads the manifest at its repository/commit/path
and compares its canonical JSON with the locked content and digest. It also
checks each declaration's byte SHA-256 at the artifact source revision. The live
release train runs this verification before artifact certification; the strict
table check runs it as well. Missing sources and mismatched bytes fail closed.
This verifies source identity, not the semantics of arbitrary SDK source code:
SDK-owned declaration generation and drift tests remain necessary to prove the
runtime constants and consumed capability set match the declared requirement.
An artifact declaration must bind the artifact's source revision, not just a newer
working component revision. SDK release CI must also check generated constants
against its consumed manifest before publishing.

This rule applies independently to JavaScript, .NET, Python, and MCP. The
JavaScript repository owns the `@honua/mcp-server` package; `geospatial-mcp` owns
the protocol specification. MCP's Honua attachment consumes both the SDK and MCP
requirements. Standalone third-party protocol clients have no Honua server floor.

The minimum establishes a declared requirement, not support for every later
server. Runtime checks still validate protocol majors, required capabilities,
and release channels. Only immutable receipts for exact server/client artifacts
establish certification. A generic upgrade rollback receipt does not prove that
the previous application can read the migrated schema or that database rollback
is safe; those are distinct acceptance conditions.

## Current qualification blocker

As of 2026-09-04, the pinned manifests do not contain capability introduction
versions and the platform lock generator still reports unresolved release inputs.
The current declarations conflict: JavaScript `1.0.0`, .NET `0.1.0`, Python
`1.0.0` with a separate hidden `2026.3.0` CalVer floor. MCP has no independent
numeric baseline declaration. Replacing these with one chosen number would not
implement the derivation rule.

Before #233 can close, protocol publishers must bind introduction evidence, each
SDK repository must correct and gate its own declarations in a linked PR, and
the release cut must pin those published artifacts and consumed manifests.
The generated table remains explicitly unqualified until then.

## Commands

Generate an honest draft with `python tools/generate_platform_lock.py --output
docs/platform-lock.v1.draft.yaml`. Its nonzero exit reports unresolved release
inputs; the draft is not a signed or certified release lock.

Generate the table with `python tools/generate_compatibility_table.py
docs/platform-lock.v1.draft.yaml`. Use `--check-output` to verify deterministic
documentation, and **`--check`** to require every declared baseline to equal the
derived lock floor. The latter fails on absent manifests as well as disagreements.
It also verifies source bytes through the GitHub contents API at the pinned
commit, using the existing `gh` authentication. It never changes authentication.
For offline verification, pass `--source-root /path/to/sources`, with Git
repositories at `sources/OWNER/REPO` containing the pinned commits. The checker
uses Git objects, so dirty working files and newer branch heads cannot substitute
for the pinned source. The standalone equivalent is
`python tools/verify_sdk_baseline_sources.py platform-lock.json [--source-root /path/to/sources]`.
`--check-output` verifies documentation freshness only and performs no source fetch.

The committed Markdown is a documentation source; the site's import/publish
wiring must be completed before claiming that the customer website consumes it.

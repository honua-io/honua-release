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

## First release: the introduction floor a publisher cannot declare

The rule above assumes an earlier server exists to name. `honua-io/honua-server` has never
published one. On 2026-09-07 its tag list, its release list, and its `refs/tags` namespace were
all empty, recorded in `certification/sources/server-publication-history.v1.json` at
`honua-server` `98414e8c`. Regenerate and re-check that receipt with

```sh
python3 tools/server_publication_history.py collect
python3 tools/server_publication_history.py verify
```

So "the publisher must supply a capability introduction version" cannot be satisfied for 2026.1
by any evidence that exists: there is no prior version for any capability to point at. For a
capability shipped in a publisher's first release the earliest server implementing it *is* that
release, and a manifest may say so by declaring

```json
"capability.id": {
  "versionModel": "semver",
  "introductionModel": "first-release",
  "evidence": {"uri": "<blob URL of the locked publication-history receipt>", "sha256": "sha256:..."}
}
```

That is a derivation, not a guess, and it fails closed on every side:

- the lock must pin the publication-history receipt (`components.honua-server.publicationHistory`
  with a repository path, HTTPS URI, SHA-256 and `maxAgeDays`) and the receipt's committed bytes
  must match it. Both fields are defined in `schemas/platform-lock.v1.schema.json`, which sets
  `additionalProperties: false`, so a lock carrying them validates and a malformed pin does not;
- the receipt must enumerate `tags`, `releases` and `git/refs/tags` completely and find nothing.
  Each source must be the exact `https://api.github.com/repos/honua-io/honua-server/...`
  collection — the verifier does not fetch these URLs, so a plausible URL on another host is not
  an enumeration — and each count must be a genuine nonnegative integer, because a string or
  `null` count would otherwise sum to zero and read as emptiness. One ref of any shape — a
  prerelease, a nightly, a chart tag — withdraws the model and sends every capability back to
  per-capability introduction evidence;
- the enumeration must be inside the pinned `maxAgeDays` at the time it is checked, **and** the
  gate re-enumerates the publisher's namespaces live and qualifies on that reading. Emptiness
  proven once is not emptiness at the cut, and a bound on staleness is not a proof either: a
  server published one day after a day-old receipt is well inside any sane bound. Only the live
  reading can withdraw the model, so an offline (`--source-root`) run cannot establish the
  premise at all;
- the capability's `evidence` must cite that exact receipt URI and digest, so a manifest cannot
  claim the model against a receipt nobody locked;
- the lock must name the first release (`components.honua-server.releaseVersion`), and that
  version must equal the released version of a locked `honua-server` artifact — a `releaseVersion`
  beside a differently versioned artifact would publish a floor for a server nobody can install.
  The lock names no release today, so nothing resolves;
- a capability may not carry both the model and a different number.

The model raises the floor with the release: whatever SemVer the first server release is issued
under becomes the floor for every capability introduced by it, and the component floor stays the
maximum across all of its required capabilities. It never lowers a floor an earlier server
genuinely established, because there is no earlier server.

## Current qualification blocker

As of 2026-09-04, the pinned manifests do not contain capability introduction
versions and the platform lock generator still reports unresolved release inputs.
The current declarations conflict: JavaScript `1.0.0`, .NET `0.1.0`, Python
`1.0.0` with a separate hidden `2026.3.0` CalVer floor. MCP has no independent
numeric baseline declaration. Replacing these with one chosen number would not
implement the derivation rule.

Before #233 can close, protocol publishers must bind introduction evidence — for
2026.1 that means the first-release model above, since no earlier server exists —
each SDK repository must correct and gate its own declarations in a linked PR, and
the release cut must pin those published artifacts and consumed manifests.
The generated table remains explicitly unqualified until then. The conflicting
declarations cannot be reconciled by choosing among `1.0.0`, `0.1.0` and `2026.3.0`:
none of them is the first server release, because the first server release does not
exist yet. Naming it is part of cutting the candidate, not of declaring a floor.

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

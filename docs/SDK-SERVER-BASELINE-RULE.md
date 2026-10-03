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
published one. On 2026-09-26 its tag list, its release list, and its `refs/tags` namespace were
all empty, recorded in `certification/sources/server-publication-history.v1.json` at
`honua-server` `b817f60d`. Regenerate and re-check that receipt with

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
- the lock must name the first release, and that version must equal the released version of a
  locked `honua-server` artifact — a version beside a differently versioned artifact would publish
  a floor for a server nobody can install. The first release is the lock's platform version
  (R22): `platform.id` `honua-2026.1-rc.3` names `2026.1.0-rc.3`, the version every imaged
  component takes. A `components.honua-server.releaseVersion`, where one is declared, takes
  precedence. A lock whose server image still carries the `pre-release` sentinel names no shipped
  release, so nothing resolves;
- a capability may not carry both the model and a different number.

The model raises the floor with the release: whatever SemVer the first server release is issued
under becomes the floor for every capability introduced by it, and the component floor stays the
maximum across all of its required capabilities. It never lowers a floor an earlier server
genuinely established, because there is no earlier server.

## The SDK capability baseline file (honua-release#231 WI-5)

Each SDK repository (`honua-sdk-js`, `honua-sdk-dotnet`, `honua-sdk-python`, `geospatial-mcp`)
commits `release/sdk-capability-baseline.json` at its root.
[`schemas/sdk-capability-baseline.v1.schema.json`](../schemas/sdk-capability-baseline.v1.schema.json)
defines it. This is the .NET file:

```json
{
  "format": "honua.sdk-capability-baseline/v1",
  "component": "honua-sdk-dotnet",
  "minimumServerVersion": "first-release",
  "requiredCapabilities": [
    "discovery.capability-manifest",
    "serve.geoservices-featureserver",
    "serve.ogc-api-features"
  ],
  "capabilities": {
    "discovery.capability-manifest": {
      "versionModel": "semver",
      "introductionModel": "first-release",
      "evidence": {
        "uri": "https://github.com/honua-io/honua-release/blob/0dd9b7a37ab4ee0dd02c17632e3de9e9eeeeddbd/certification/sources/server-publication-history.v1.json",
        "sha256": "sha256:3069fde14a32cc579e4ee92cbe7e86bd88a14c1405df94457e1393c63fe092d1"
      }
    },
    "serve.geoservices-featureserver": {
      "versionModel": "semver",
      "introductionModel": "first-release",
      "evidence": {
        "uri": "https://github.com/honua-io/honua-release/blob/0dd9b7a37ab4ee0dd02c17632e3de9e9eeeeddbd/certification/sources/server-publication-history.v1.json",
        "sha256": "sha256:3069fde14a32cc579e4ee92cbe7e86bd88a14c1405df94457e1393c63fe092d1"
      }
    },
    "serve.ogc-api-features": {
      "versionModel": "semver",
      "introductionModel": "first-release",
      "evidence": {
        "uri": "https://github.com/honua-io/honua-release/blob/0dd9b7a37ab4ee0dd02c17632e3de9e9eeeeddbd/certification/sources/server-publication-history.v1.json",
        "sha256": "sha256:3069fde14a32cc579e4ee92cbe7e86bd88a14c1405df94457e1393c63fe092d1"
      }
    }
  }
}
```

The exact file for each of the four repositories is in
[`tools/fixtures/sdk-capability-baselines/`](../tools/fixtures/sdk-capability-baselines/).

- `component` is the platform-manifest key. A file copied from another SDK is refused.
- `requiredCapabilities` are keys of the honua-server capability vocabulary,
  `docs/gis/data/capability-keys.v1.json`, that the SDK cannot work without. Optional features
  negotiated at runtime are not listed. The resolver reads that vocabulary at the **selected
  honua-server sha** and refuses a baseline that names a key the candidate server does not
  advertise.
- `capabilities` holds exactly one introduction entry per required capability: either a numeric
  `minimumServerVersion` that an earlier released server established, or
  `introductionModel: first-release`. In both cases the entry needs `versionModel: semver` and
  `evidence` (`uri` + `sha256`). First-release evidence must cite the publication-history
  receipt that the lock pins (above).
- `minimumServerVersion` is the maximum over the required capabilities. Because no earlier
  server exists, that maximum is `first-release` as soon as one required capability uses the
  model. The resolver refuses any other value.

### How the resolver reads it

`tools/resolve_trunk_candidate.py` reads the file through the contents API. It never reads it at
the SDK's trunk head. It reads it at the SDK's **published** source revision: the
`artifactSourceRevision` of the verified primary package (#412, R25), plus the `sourceSha` of
every other published package from the same repository (for example `@honua/mcp-server` beside
`@honua/sdk-js`), because the lock's declarations must cover every shipped artifact revision.
Each revision becomes one `serverCompatibility.manifests[]` entry. That entry holds the source,
the canonical-JSON digest, the content and `requiredCapabilities`. Each revision also becomes one
`declarations[]` entry with the byte SHA-256 and the declared floor. A `serverCompatibility`
carried in the manifest by hand never survives. The night refuses, naming the SDK, when the
file is missing at any of those revisions, when it does not match the schema, names another
component, is internally inconsistent or names an unadvertised capability, or when revisions
disagree on the floor.

`first-release` stays literal in the lock. `tools/sdk_baselines.py` resolves it against the same
lock, to the platform version, under every rule above. The validator, the generator and the
compatibility table therefore all print the concrete floor (`2026.1.0-rc.3`). The generator
checks SDK floors only after every component is built, so the order of the manifest's rows
cannot decide whether a floor resolves.

**A baseline exists only at a published revision.** Committing the file to an SDK's trunk
changes nothing until that SDK publishes a package built from a commit that contains it, and
the `clientArtifacts` pin is moved to that publication (R25). Until then the resolver reads the
old published revision, finds no file, and refuses.

## Current qualification blocker

As of 2026-09-26, the pinned manifests do not contain capability introduction
versions and the platform lock generator still reports unresolved release inputs.
`components.honua-server.version` is still the `pre-release` sentinel. The pinned
declarations still conflict: JavaScript `d7cec2d5` declares `1.0.0`, .NET
`6ba49ec3` declares `0.1.0`, and Python `40ecf731` declares `1.0.0` plus a hidden
`2026.3.0` CalVer floor. MCP `d5a09d13` has no introduction floors. JavaScript,
.NET, and MCP trunk heads match those pins. Python trunk `28d90152` (honua-sdk-python#224,
`2e91603f`) drops the CalVer floor and keeps `1.0.0`, but that revision is not the
pinned artifact. Replacing these with one chosen number would not implement the
derivation rule.

As of 2026-10-03 (WI-5), the resolver reads the baseline file. None of the four repositories
carries it at a published revision yet, so every night refuses on the four SDK rows until each
repository commits its file, republishes, and moves its pin. After that, the generator still
refuses until two facts are present in the lock. The first is a `honua-server` image whose
version is the platform version (WI-2/R22). The second is the publication-history receipt pinned
as `components.honua-server.publicationHistory`.

WI-2 (R22) produces the first fact. When the nightly stamps its label, `mint_nightly_lock.py
--stamp` gives honua-server, honua-console and the Helm chart `artifactVersion` set to the label's
platform version (`platform_version.artifact_version`: `2026.1-rc.N` is `2026.1.0-rc.N`, GA
`2026.1.0` is `2026.1.0`). It also sets honua-server's `releaseVersion` to that version. A component
is stamped only when its digest, artifact source revision and per-architecture digests (for a
chart, the package checksum) are bound. The resolver drops any version carried forward from another
night, and drops a chart digest whose `artifactSourceRevision` is not the sha selected tonight,
because the chart package is not re-resolved the way an image is. The generator and
`validate_platform.py` refuse a platform version without those bound facts. Promotion does not
rebuild the bytes: `finalize_release.finalize_manifest` restamps the same bound identity to the GA
platform version (`2026.1.0`) when it rewrites `platformRelease` to the base label. Release notes
render `artifactVersion` when it is present. An exact candidate whose imaged identity is already
bound must carry that stamp, and a manual freeze fails closed when the generator refuses an
artifact version. SDK, gRPC and MCP rows keep their own semver.

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

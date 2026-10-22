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
  prerelease, a version tag, a chart tag, any GitHub release — withdraws the model and sends
  every capability back to per-capability introduction evidence. The single exception is the
  release program's own dispatch anchors (below);
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

**Dispatch anchors are not publications.** Since the 2026-09-26 receipt the nightly creates
`nightly-candidate-<sha7>` and `nightly-candidate/<sha40>` tags on honua-server so that gated
workflows (nightly-container-build, capacity-soak-candidate, the DR drill, the attestation signer
identity) can run at a non-head trunk commit. Such a tag's name is the commit it points at; it
carries no version, so no capability can be introduced "in" it and no floor can name it. The
images those runs build are the same nightlies that already existed untagged. The receipt
therefore lists each anchor in `dispatchAnchors` with the commit the tags API resolves it to, and
`server_publication_history.verify` accepts an anchor only when its name is exactly one of those
two forms and its hex is a prefix of that commit. Both tag namespaces must be fully accounted for
by the anchors, any GitHub release (even on an anchor) is a publication, and every other tag
still withdraws the model. Without this, the program's own nightly would have silently withdrawn
the premise every SDK floor rests on: on 2026-10-18 honua-server carried 14 anchors and nothing
else, and the strict check refused all four SDKs for it.

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

## Runtime floor constants

An SDK also enforces its floor at run time, by comparing the server's reported
`serverVersion` with a constant. That constant is a declaration like any other and must agree
with `release/sdk-capability-baseline.json`:

- a numeric `minimumServerVersion` is used as is;
- `first-release` is the least SemVer identity of the first server release line, `2026.1.0-0`.
  R22 makes the first release `2026.1.0` and its candidates `2026.1.0-rc.N`; every one of them,
  and every later server, compares at or above `2026.1.0-0`, while builds that predate the
  release line's version identity (the .NET default `1.0.0`, `0.0.0`) compare below it. An SDK
  is published before any candidate is cut, so it cannot name `rc.N`; `-0` is the SemVer floor
  of the whole line, not a guess at a candidate.

Each SDK owns a drift test that reads its baseline file and fails when the runtime constant,
the public API snapshot or a published compatibility document disagrees with this mapping.
`1.0.0` (JavaScript, Python) and `0.1.0` (.NET) were not derived from any capability. Each SDK
replaces its constant with the mapped value in its own repository, on branch
`fix/233-runtime-server-floor` of honua-sdk-js, honua-sdk-dotnet and honua-sdk-python (linked
from #233). geospatial-mcp and `@honua/mcp-server` enforce no numeric server floor, so their
baseline file is their only declaration. Builds from before honua-server#4693 gave the server a
version identity report `1.0.0+<sha>`; they are not release identities, and the SDKs now refuse
them.

## Current state (2026-10-18)

All four repositories commit `release/sdk-capability-baseline.json` with
`minimumServerVersion: first-release` (honua-sdk-js#2016, honua-sdk-dotnet#427,
honua-sdk-python#290, geospatial-mcp#99), and their published packages carry it. The nightly
resolver binds them: candidate run 39136507018 (2026-10-18, platform `2026.1-rc.3`, honua-server
`c19f29d1`, `releaseVersion` `2026.1.0-rc.3`) pins a consumed manifest and a declaration for every
published revision of every SDK. Two facts still kept the strict check red on that candidate:

1. no manifest pinned `components.honua-server.publicationHistory`, so every first-release
   capability was unqualified. `platform-manifest.yaml` now pins the committed receipt whose
   bytes every SDK baseline cites (`sha256:3069fde1…`), with `maxAgeDays: 90`;
2. the live re-enumeration found the 14 nightly dispatch anchors and withdrew the model. The
   anchor rule above classifies them.

With both, `generate_compatibility_table.py --check` passes on that candidate's lock: every SDK
resolves to `2026.1.0-rc.3`, the declarations agree, and the pinned sources and the live
publication history verify. The committed draft (`docs/platform-lock.v1.draft.yaml`) is
generated from the committed manifest, whose SDK pins predate the baseline files, so the
committed table stays explicitly unqualified for the SDK rows until the manifest is re-pinned
to a candidate. That is the expected state of a draft, not a disagreement.

The upgrade-edge section of the table states the only edge the lock can establish: the first
release is a fresh install, because the pinned and live-verified publication history proves no
earlier server exists. Every other edge — from a nightly or earlier candidate, or to the next
release — stays explicitly unqualified until it has separate application-rollback,
database-rollback and backup-restore receipts.

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

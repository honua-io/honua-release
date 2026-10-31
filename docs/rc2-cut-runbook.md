# 2026.1-rc.2: freeze the platform lock

Candidate cut: 2026-11-01 06:00 UTC. The release session owns candidate publication,
manifest changes for the server, gate dispatch, and release dispatch. The commands
below read GitHub/registries and write local files; they publish and dispatch nothing.

The `platform_lock` input to `rc-release.yml` is `sha256:<64 lowercase hex>` of the
**exact platform-lock.json bytes used by the candidate's gates**. The workflow has
no manifest-digest fallback: `tools/rc_gate_window.py:resolve_candidate` requires an
explicit lock digest or a lock file. Hashing platform-manifest.yaml does not produce
the required identity. Preserve the frozen lock; reformatting JSON changes its hash.

## Inputs the release session must finish first

Use a clean honua-release worktree at the reviewed cut revision, with Python 3.12,
Docker/buildx, and an authenticated current GitHub CLI. Install the tool dependencies:

```bash
python3 -m venv out/rc2-venv
out/rc2-venv/bin/pip install -r tools/requirements-nightly.txt
export PATH="$PWD/out/rc2-venv/bin:$PATH"
```

Choose a full honua-server candidate SHA and publish its immutable image through
the server's normal process. The frozen manifest must bind that SHA to the actual
image index digest, artifact source revision, architecture digests, migration
journal and component versions. Carry the candidate's real runtime catalog/OKF,
SDK baselines, fixture revisions and deployment qualification in the frozen
manifest/matrix. Do not change only the source SHA while retaining another
candidate's artifact or evidence. All generator refusals must be resolved.

Issue [#482](https://github.com/honua-io/honua-release/issues/482) tracks resolver
and candidate qualification. Its current entry point is
`tools/resolve_trunk_candidate.py --dry-run`; it selects qualifying commits across
repositories, and **does not accept a server-SHA override**. A resolver-selected
server must equal the chosen candidate before its outputs can be used. The
`--prepare-deployments` mode explicitly emits unadmitted inputs and is not a
release lock. Do not substitute those outputs for qualified cut inputs.

`tools/generate_platform_lock.py --qualification` is the same local JSON generator
used by `release-train.yml`. It writes canonical, unsigned qualification bytes and
returns nonzero if required pre-gate facts are absent. Post-gate SBOM/provenance
and notes are explicitly pending in this mode. `tools/platform_lock_bundle.py
--qualification` independently re-derives and binds those bytes to the frozen
manifest/matrix. Neither command certifies a release; the cut's gates still must
pass with this digest. Final signed minting belongs to the release session and
can produce a different, final release-lock digest; keep both identities distinct.

## Exact local cut commands

Run this entire block in bash from the clean honua-release cut worktree. Set
`CANDIDATE_SHA` to the chosen full SHA and `CUT_INPUTS` to the directory containing
the completed candidate's frozen platform-manifest.yaml and compatibility-matrix.yaml.
Use a fresh `CUT_OUT` directory for each candidate. This block never edits inputs.

```bash
set -euo pipefail
export CANDIDATE_SHA=6f477f6176fb9612ef869205a7110e719ff8f492
export CUT_INPUTS="$PWD"
export CUT_OUT="$PWD/out/rc2-$CANDIDATE_SHA"
mkdir -p "$CUT_OUT"
test ! -e "$CUT_OUT/platform-lock.json"

# Refuse stale server pins before generating or hashing anything.
python3 - <<'PY'
import os, re
from pathlib import Path
import yaml
sha = os.environ['CANDIDATE_SHA']
if not re.fullmatch(r'[0-9a-f]{40}', sha):
    raise SystemExit('BLOCKED: CANDIDATE_SHA must be 40 lowercase hex characters')
m = yaml.safe_load((Path(os.environ['CUT_INPUTS']) / 'platform-manifest.yaml').read_text())
s = m['components']['honua-server']
if s.get('sha') != sha or s.get('artifactSourceRevision') != sha:
    raise SystemExit(f"BLOCKED: requested {sha}; manifest server sha={s.get('sha')}, "
                     f"artifactSourceRevision={s.get('artifactSourceRevision')}")
print(f'PASS: frozen server source and artifact revision both equal {sha}')
PY

python3 tools/generate_platform_lock.py \
  --manifest "$CUT_INPUTS/platform-manifest.yaml" \
  --matrix "$CUT_INPUTS/compatibility-matrix.yaml" \
  --output "$CUT_OUT/platform-lock.partial.json" --qualification
python3 tools/platform_lock_bundle.py "$CUT_OUT/platform-lock.partial.json" \
  --manifest "$CUT_INPUTS/platform-manifest.yaml" \
  --matrix "$CUT_INPUTS/compatibility-matrix.yaml" \
  --label 2026.1-rc.2 --out-dir "$CUT_OUT/bound" --qualification
cmp "$CUT_OUT/platform-lock.partial.json" "$CUT_OUT/bound/platform-lock.json"
mv "$CUT_OUT/platform-lock.partial.json" "$CUT_OUT/platform-lock.json"
export PLATFORM_LOCK="sha256:$(sha256sum "$CUT_OUT/platform-lock.json" | cut -d' ' -f1)"
printf 'platform_lock=%s\n' "$PLATFORM_LOCK"
sha256sum "$CUT_OUT/platform-lock.json"
```

`set -e` is mandatory: a generator may leave a useful partial JSON file while
returning failure. Never hash or submit that file as an admitted lock. Run these
commands in a script or a dedicated shell so an error stops the sequence.

The release session passes the printed `platform_lock` unchanged to rc-release,
along with the matching server SHA and image tag, after running the candidate's
gates with the same frozen inputs. Do not regenerate between gate execution and
admission. The candidate-bound receipts must name this exact digest.

## Local rehearsal, 2026-10-31 UTC

Requested source: `6f477f6176fb9612ef869205a7110e719ff8f492` (trunk at assignment).
No server image was built or published and no workflow was dispatched.

The cut block was executed against the current committed inputs. It correctly
stopped at the source binding check (exit 1); those inputs still describe rc.1:

```text
BLOCKED: requested 6f477f6176fb9612ef869205a7110e719ff8f492; manifest server sha=a4bde4b8c5ee8cc3dec05ad8b3d4f84470c1ea6e, artifactSourceRevision=None
```

A separate
read-only registry check found that the requested candidate tag was absent:

```text
ERROR: ghcr.io/honua-io/honua-server:nightly-6f477f6: not found
```

The generator was also exercised on a local diagnostic copy with the requested
server source SHA and the previous server artifact identity removed. The partial
file is retained only for diagnosis, never as a gate input. Its exact output and
remaining fields are recorded below. The release session must publish the chosen
candidate and complete its frozen declarations before the successful cut command
can print a usable `platform_lock`.

```text
BLOCKED: wrote candidate-platform-lock.partial.json; 24 release field(s) remain unresolved:
AT-CUT: 18 deferred field(s) still block signing
- [AT-CUT] $.components.honua-server.migrationJournalSha256: exact declared migration set is not bound
- [DECISION] $.components.honua-server.artifacts: no artifact coordinate is declared
- [AT-CUT] $.components.honua-console.contractVersions: not declared
- [AT-CUT] $.components.honua-console.schemaVersions: not declared
- [PUBLISH] $.components.honua-console.artifacts[0].version: source snapshot/pre-release is not a released artifact version
- [AT-CUT] $.components.honua-sdk-dotnet.schemaVersions: not declared
- [AT-CUT] $.components.honua-sdk-python.schemaVersions: not declared
- [AT-CUT] $.components.geospatial-grpc.schemaVersions: not declared
- [AT-CUT] $.components.geospatial-mcp.contractVersions: not declared
- [AT-CUT] $.components.geospatial-mcp.schemaVersions: not declared
- [AT-CUT] $.components.honua-iac.contractVersions: not declared
- [AT-CUT] $.components.honua-iac.schemaVersions: not declared
- [AT-CUT] $.components.honua-helm.contractVersions: not declared
- [AT-CUT] $.components.honua-helm.schemaVersions: not declared
- [AT-CUT] $.components.honua-mobile.contractVersions: not declared
- [AT-CUT] $.components.honua-mobile.schemaVersions: not declared
- [AT-CUT] $.components.honua-collect.contractVersions: not declared
- [AT-CUT] $.components.honua-collect.schemaVersions: not declared
- [PUBLISH] $.components.honua-sdk-js.serverCompatibility: unqualified: no consumed protocol/capability manifest is pinned
- [PUBLISH] $.components.honua-sdk-dotnet.serverCompatibility: unqualified: no consumed protocol/capability manifest is pinned
- [PUBLISH] $.components.honua-sdk-python.serverCompatibility: unqualified: no consumed protocol/capability manifest is pinned
- [PUBLISH] $.components.geospatial-mcp.serverCompatibility: unqualified: no consumed protocol/capability manifest is pinned
- [AT-CUT] $.contentDigests.catalog: catalog digest is not declared
- [AT-CUT] $.contentDigests.okf: OKF digest is not declared
exit=1
```

No usable platform_lock digest was produced. These are unresolved release inputs,
not permission to bypass the source, generator or bundle checks.

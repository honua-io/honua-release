# Published SDK diagnostic

Manually dispatch `gate-executable-docs.yml` with `sdk_publication_replay=true`
to verify the exact published SDK family, independent MCP, and scaffold
selected by the reviewed `platform-manifest.yaml`, against its retained server image. The diagnostic runs in an independent job;
it does not replace the normal release gate or supply its workflow output.

`prepare.py` checks stable release tags and verifies registry bytes and signed
split-package provenance with the canonical artifact verifier. It writes a
throwaway manifest under the runner temporary directory. Canonical release and
customer manifests remain unchanged; customer rebinding still waits for the
rc.3 mint under owner decision 19. Versions come from the reviewed client rows;
stable release tag names are derived from those versions. Every SDK split must
share the root SDK version and source. Independent MCP and scaffold cuts remain
unchanged. Registry/tag drift fails rather than silently refreshing a pin.

For a future SDK cut, `prepare.py --sdk-version <exact-stable-version> --output
<fresh-directory>` performs the same full public-byte and provenance verification
for a temporary SDK-family override. Its receipt explicitly labels the override
as unqualified preparation evidence. It never selects a newer MCP or scaffold,
modifies canonical files, or authorizes replay/promotion. Review the verified
platform-only changes in a separate PR before replay: `run.py` requires the
entire candidate manifest to equal the reviewed manifest, including exact source,
integrity, independent client pins, and server identity. Customer history remains
reserved for the supported mint path.

An SDK release updates the scaffold README's SDK-version markers in the source
checkout, but does not automatically release the independent scaffold package.
The executable-docs inventory reads that README from the scaffold's own published
`sourceSha`. If those instructions name the previous SDK cut, first complete the
SDK seal/publication, then release the reviewed scaffold-only documentation patch
through Release Please and verify its actual public bytes before advancing its
platform pin. Do not read the scaffold README from the newer SDK source instead.
For SDK 0.1.16, scaffold 0.1.8's published instructions still explicitly select SDK
0.1.15, so a managed scaffold patch is a separate prerequisite for the fresh
five-document replay. This explicit documented override is distinct from the
scaffold's promoted channel and certified 0.1.14 fallback; a documentation release
does not certify or advance that fallback.

`run.py` checks public default peer resolution, the exact documented scaffold
and dev commands, the full installed-client matrix, and the five SDK-owned
getting-started documents at the published source revision. Existing blocked
cells remain blocked and every failing gate retains its failure. The separately
installed MCP certifier version does not imply that the SDK generates that pin.

Curated receipts, the temporary manifest, and the public consumer lock are uploaded.
An installed-client exit code of zero can include declared blocked cells; its
JSON report, not the process exit code alone, determines each cell outcome. Raw logs and consumer workspaces remain on
the ephemeral runner. Each stack has its own Compose project and volumes, with
cleanup in both canonical runners and an always-run workflow step. Results are
diagnostic evidence, not a certified release lock or installer qualification.

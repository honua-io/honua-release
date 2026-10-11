# Published SDK diagnostic

Manually dispatch `gate-executable-docs.yml` with `sdk_publication_replay=true`
on this branch to verify published SDK 0.1.15, MCP 0.1.16, and scaffold 0.1.8
against the retained server image. The diagnostic runs in an independent job;
it does not replace the normal release gate or supply its workflow output.

`prepare.py` checks stable release tags and verifies registry bytes and signed
split-package provenance with the canonical artifact verifier. It writes a
throwaway manifest under the runner temporary directory. Canonical release and
customer manifests remain unchanged; customer rebinding still waits for the
rc.3 mint under owner decision 19.

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

# Installed-client certification

This gate creates clean consumer environments and installs the exact public package bytes in
`platform-manifest.yaml`. It never builds a checkout, accepts a floating version, substitutes a
local server image, or omits a matrix cell. The receipt records every pass and non-pass with the
release, package integrity, source SHA, immutable server image, fixture/config/auth revisions,
operation, target, and durable CI evidence URI.

The JS npm, MCP npm, and PyPI cells are executable now. Both npm lanes download the registry
tarball and recompute its SHA-512 before installation; lockfile metadata alone is not accepted as
byte proof. The MCP lane co-installs the independently byte-verified manifest-pinned JS SDK (its
declared peer), then verifies and executes every declared package binary. The
NuGet cell is deliberately materialized as a failing
blocked result until [honua-release#57](https://github.com/honua-io/honua-release/issues/57) makes
the package independently installable from its release registry. A release-mode run therefore
cannot pass early with only two ecosystems.

The live driver reuses the repository's one-server/one-PostgreSQL candidate harness and immutable
`e2e/harness/seed` fixture. Static input validation and exact package-byte installation can be run
without a server:

```sh
python certification/installed-clients/run.py --validate-only \
  --evidence-uri "https://github.com/honua-io/honua-release/actions/runs/$RUN_ID"
```

Final end-to-end authorization-profile coverage remains dependent on the server proof tracked by
[honua-server#3475](https://github.com/honua-io/honua-server/issues/3475); this repository does not
modify or simulate that server behavior.

The MCP cell is a live `initialize` plus `tools/list` exchange through the package's installed
`honua-mcp-proxy` executable. The current pinned `0.1.4-beta.0` bytes exit silently when launched
through npm's binary shim, so the cell remains a required, explicit failure until a corrected
artifact is pinned. The harness does not launch the module through a source or resolved-path
fallback to turn that packaging defect green.

Use `--live` for the certification run. It boots the manifest image by digest once with the
candidate PostgreSQL service, applies the shared seed once, then runs both installed SDK probes
against that same target before teardown. Omitting `--live` is an install-integrity preflight and
cannot be used as release evidence.

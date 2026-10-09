# Executable docs runner

See [the executable docs guide](../../docs/EXECUTABLE-DOCS.md) for inventory, prerequisites and local execution.

`<!-- doc-run: blocked owner/repo#123 -->` requires a linked issue and still runs the block.
Only a witnessed failure of that block’s command becomes `blocked`. Runner exceptions,
container/infrastructure errors, output assertions and installed-package audit failures stay `fail`.
A linked blocked marker on an unsupported language fails instead of being silently unexecuted.
An unlabelled command fence still runs as shell; only a witnessed command failure becomes `blocked`.
A passing command records `staleBlockedMarker` so authors can remove the marker.

Readiness attributes combine with the issue marker:

```markdown
<!-- doc-run: blocked owner/repo#123 ready-url="http://localhost:3000/" -->
```

`ready-log="exact ready line"` also works. Once readiness succeeds it stays observed through
shutdown at the end of the serve window.

Document precedence is `fail > needs-input > blocked > pass`. Any blocked document keeps the
nightly gate `blocked` and red; there is no waiver. Documented `jq`, `python` and `sudo`
prerequisites use the reader container’s tool installation mechanism.

A getting-started page that names the release channel (`ghcr.io/honua-io/honua-server:2026.1-rc`, or
`:2026.1` at GA; honua-io/honua-server#5738) is executed against the candidate behind that channel:
`vars/honua-server-docs-get-started-quickstart.json` substitutes both tags with `{candidate.image}`, the
`image@digest` derived from `platform-manifest.yaml` `components.honua-server` exactly as the boot uses it.
Only the executed code changes; the published page and the report's `command` keep the channel tag. A
variables value that cannot be rendered (no candidate image) fails the document instead of running the
floating tag, the legacy hand-copied `@sha256:` form runs unchanged, and `boots-candidate-image` still fails
whenever the stack ran any image other than the candidate digest.

Run `python3 -m pytest certification/executable-docs -q` for offline regressions and
`python3 certification/executable-docs/docker_regressions.py` for live Docker checks.
Runtime evidence under root `out/` is ignored and must not be committed.

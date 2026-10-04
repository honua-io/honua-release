# Executable docs runner

See [the executable docs guide](../../docs/EXECUTABLE-DOCS.md) for inventory, prerequisites and local execution.

`<!-- doc-run: blocked owner/repo#123 -->` requires a linked issue and still runs the block.
Only a witnessed failure of that block’s command becomes `blocked`. Runner exceptions,
container/infrastructure errors, output assertions and installed-package audit failures stay `fail`.
A linked blocked marker on an unsupported language fails instead of being silently unexecuted.
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

Run `python3 -m pytest certification/executable-docs -q` for offline regressions and
`python3 certification/executable-docs/docker_regressions.py` for live Docker checks.
Runtime evidence under root `out/` is ignored and must not be committed.

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

JavaScript and TypeScript snippets use one Node interpreter per declared reader context.
Lexical declarations, imported clients and top-level `await` survive into later blocks;
each block runs once, including blocks that write something before throwing. A failed
block retains its actual error and is never retried with earlier blocks. Separate contexts
have separate interpreters, working directories and environments. An uncaught error, timeout or worker exit
loses that context permanently for the run: the runner kills its owned container, refuses
to restart it and reports failure. This also stops child processes started by that context.

The reader REPL adapts leading static imports (side-effect, default, named and namespace)
to dynamic imports. Unsupported import syntax fails before evaluation. CommonJS `require`
resolves from the reader's current directory. TypeScript uses the pinned Node runtime's
type transformation; the separate explicit typecheck path still uses the configured compiler.
Every requested static export must exist as an own module export before binding; an
existing export whose value is `undefined` remains valid. Leading dynamic imports retain
native parsing, including whitespace and comments before the argument.
Strict evaluation and removal of REPL-only built-in globals keep undeclared names and
missing imports as actual errors.
Injected environment keys removed by a later block restore the container baseline,
including the candidate registry after temporary source-build admission. Unrelated
interpreter environment mutations survive.
Completion uses Node's `beforeExit` event with only the worker's input pipe temporarily
unreferenced. Referenced async work and its callbacks must finish before the block passes;
late errors remain errors even after evaluation returns. Unsettled top-level await at idle
fails and stops the context. Referenced long-running work remains subject to the caller's
timeout. No quiet-period sleep is used as a completion oracle.
Completion records use atomically published files with a unique request identity on the
shared reader workdir. They never share stdout with snippet output. Actual descriptors
1 and 2 point to each block's capture files, including native writes and inherited child
stdio; stale, partial or wrong-block records fail closed. On failure the worker publishes
the error and exits before another event-loop turn, retaining microtask effects that a
native Node ESM script also performs before failing.
The live Docker regression retains `artifacts/executable-docs/node-context-rehearsal.json`
with source hashes, the pinned runtime image, actual Node version and owned-cleanup controls.
These offline controls do not establish candidate execution or change any required row's status.


## Immutable source builds

Installed consumer blocks continue using the candidate registry guard. Three SDK source documents
opt in to a separate source-build boundary: the README, quickstart and MCP README. Only their
source clone/fixture blocks and explicit `doc-run: checkout` blocks receive that admission.
The runner derives an exact closure from the same immutable source revision as the document:
block hashes, both npm lock hashes, and every Honua package path, name, version and tarball SRI.
The MCP `file:..` link is bound to that checkout's package metadata and actual symlink target.

A documented moving clone is followed by an explicit fetch and checkout of the document revision
before its unchanged `npm ci` command. The report records this source-binding operation and labels
these results `immutable-source-build`; they are source-build evidence, not published consumer
identity proof. Existing checkouts must match the exact source SHA, and locks are verified before
and after each admitted block. Source builds therefore cannot silently use today's trunk.

Source tarballs have a separate, content-addressed registry namespace. It admits only exact lock
entries and verifies their hashes, without range expansion. The temporary registry environment is
restored after each block, including failures. The candidate namespace and runtime package audit
stay strict. Installed inventories retain every package path and nested version; copies cannot
hide behind a name-only dictionary. The retained document admission and block inventories make
source build dependencies distinguishable from packages actually installed by a runtime reader.

After a source checkout has passed verification, later shell commands and deferred snippets may
still observe its installed dependencies. Those observations receive a separate
`sourceDependencyAudit`: the same source SHA and locks are verified before and after execution,
and every package inside that checkout must match the exact admitted path and link inventory.
This does not grant those blocks access to the source registry or change their documented
commands. Packages outside the checkout remain subject to the candidate version audit.

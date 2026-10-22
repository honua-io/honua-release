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

Run `python3 -m pytest certification/executable-docs -q` for offline regressions.
The live Docker controls are `python3 certification/executable-docs/docker_regressions.py`
and `python3 -m pytest certification/executable-docs/install_healthcheck_docker.py -q`.
Runtime evidence under root `out/` is ignored and must not be committed.

JavaScript and TypeScript snippets use one Node interpreter per declared reader context.
Lexical declarations, imported clients and top-level `await` survive into later blocks;
each block runs once, including blocks that write something before throwing. A failed
block retains its actual error and is never retried with earlier blocks. Separate contexts
have separate interpreters, working directories and environments. An uncaught error, timeout or worker exit
loses that context permanently for the run: the runner kills its owned container, refuses
to restart it and reports failure. This also stops child processes started by that context.

The reader links static imports (side-effect, default, named and namespace)
through one native ESM bridge before evaluating any dependency. Named and default
references read the bridge's live exports across later blocks and retained closures.
Node's module cache prevents replaying shared dependencies. The runner bootstraps
TypeScript 5.9.3 once from its committed integrity-locked npm package using the owned
Node/npm toolchain and mounts the compiler read-only into each Node context. The
bootstrap has a separate bounded timeout and a version/hash receipt; failure stops
the run before document execution. Unsupported import syntax fails before evaluation. CommonJS `require`
resolves from the reader's current directory. TypeScript uses the pinned Node runtime's
type transformation; the separate explicit typecheck path still uses the configured compiler.
Native linking checks every requested static export before any dependency body runs;
an existing export whose value is `undefined` remains valid. Dynamic imports retain
native parsing, including whitespace and comments before the argument.
Snippet-body `import.meta` uses native Node module metadata anchored to the snippet's
file, including `url`, `filename`, `dirname`, `main` and `resolve`.
Strict evaluation and removal of REPL-only built-in globals keep undeclared names and
missing imports as actual errors.
Injected environment keys removed by a later block restore the container baseline,
including the candidate registry after temporary source-build admission. Unrelated
interpreter environment mutations survive.
Blocks selected by the existing CommonJS `require(...)` rule retain their `.cjs` filename
and get a fresh owned `module.exports` object and initial `exports` alias per block.
Replacing `module.exports` does not rebind `exports`; saved lexical references survive
while later blocks receive their own module object. ESM blocks do not gain those globals.
Completion uses Node's `beforeExit` lifecycle with only the owned harness input/output
pipes excluded from liveness. Reader listeners run in native order under the current
block's capture scope. After their nextTick/Promise jobs drain, an owned event-loop
checkpoint checks the stable public `process.getActiveResourcesInfo()` API. Referenced
reader work waits for the next natural `beforeExit`; cleanup checkpoints do not manufacture
extra reader events. No resource types are ignored. Referenced async work must finish before the block passes;
late errors remain errors even after evaluation returns. Unsettled top-level await at idle
fails and stops the context. Referenced long-running work remains subject to the caller's
timeout. No quiet-period sleep is used as a completion oracle.
Before publishing completion, the reader cancels its owned unreferenced timers/immediates
and closes owned unreferenced sockets, listening servers, message ports and `fs.watch`
watchers through their public lifecycle APIs. Refreshed timers acquire the refreshing
block's ownership. Capture remains active while close callbacks drain; a cleanup error
or work that cannot drain within the original timeout fails and stops that context.
An idle SDK pool connection is closed without removing its listeners; the installed
public SDK must reconnect when the same client is used by a later block. There are no
trusted-dependency exemptions. Unsupported owned unreferenced child processes, workers,
stat watchers and promise-watch iterators fail explicitly before completion. A bound UDP
socket that the block has unreferenced fails the same way. A referenced bound socket is
not idle: it holds the event loop until the caller timeout.
An owned UDP socket that is still unbound after `beforeExit` listeners completes the way
native Node does, including an unbound socket the block has unreferenced. Native exit
does not run its close callback, and neither does this completion. `process.getActiveResourcesInfo()`
can still list `UDPWrap` for that inactive handle. The runner drops that socket's own
event-loop ref through the public `unref()` API after listeners run. It does not subtract
resource types from the list, and it does not unref a socket that `bind`, `send` or `connect`
has already requested. Arbitrary native-addon schedulers and the full Node Module API are
not established by this slice.
This is bounded resource cleanup, not native process-exit fidelity: cleanup may invoke
user close callbacks that native process exit would not invoke. The paired native-oracle
control `test_cleanup_callbacks_are_captured_before_pass_with_explicit_native_difference`
records that difference. No governed document or acceptance row is waived by it.
The final `process.exitCode` is checked at completion, with Node's native 8-bit exit status
retained; a nonzero status fails and loses the context. Each block starts with a fresh exit status.
Completion records use atomically published files with a unique request identity on the
shared reader workdir. They never share stdout with snippet output. Actual descriptors
1 and 2 point to each block's capture files, including native writes and inherited child
stdio; stale, partial or wrong-block records fail closed. On caught failure the worker
publishes the error and exits before another event-loop turn, retaining microtask effects
that a native Node ESM script also performs before failing. A native fatal exit loses
the interpreter with its captured stderr and fails the original request before any
success is published.

This bounded reader does not implement every Node module feature. Unsupported module
exports and arbitrary native-addon schedulers fail the affected block; the gate does
not convert such failures into a pass or skip. Native-oracle controls cover live bindings,
metadata, once-only execution and the stated resource limitations.
The live Docker regression retains `artifacts/executable-docs/node-context-rehearsal.json`
with source hashes, the pinned runtime image, actual Node version and owned-cleanup controls.
Its separate `node-resource-rehearsal.json` binds the public SDK pool control to the
manifest's package version, integrity and source revision, while native socket fixtures
exercise delivery after completion. These controls do not establish candidate execution
or change any required row's status.


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

### Python environment inventory

After Python blocks, the runner inventories Honua distribution metadata with the
runtime's original interpreter in isolated mode. It inspects the reader's user
site explicitly and discovers virtualenvs under the bounded document workspace,
HOME and container `/tmp`, including environments that were never activated.
Literal interpreter paths known to the execution wrappers are retained and must
be inspectable. The probe reads metadata without executing virtualenv programs
or their startup hooks. Reports retain environment/site-package paths alongside
all simultaneous installed versions. Missing environments, malformed Honua
metadata, escaping site-package/metadata links and the directory bound refuse
qualification. Normal virtualenv interpreter links to system Python remain valid.

Installed Honua versions must match the governed package cohort in every audited
environment. Every direct_url.json origin recorded by pip is refused (including local-directory,
VCS, direct-archive and unknown origin shapes)
even when their version matches; direct wheel URLs and local wheel files must not
bypass the guarded package index. This metadata audit does not prove installed
wheel-byte identity; the registry download guard separately binds admitted wheel
hashes. These controls do not update the cohort or turn historical document
failures into passes.

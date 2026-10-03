# Executable docs

The promise is that **a new user can follow the published docs against the release**. `gate-docs`
checks what the docs *claim*. `gate-executable-docs` runs what the docs tell a reader to run. Every
fenced block in the public getting-started documents runs against the release candidate, nightly,
and the gate is red when one fails.

- Documents and revisions: [`certification/executable-docs/sources.json`](../certification/executable-docs/sources.json)
- Inventory of every block (committed): [`certification/executable-docs/inventory.json`](../certification/executable-docs/inventory.json)
- Runner: [`certification/executable-docs/run.py`](../certification/executable-docs/run.py)
- Workflow: [`.github/workflows/gate-executable-docs.yml`](../.github/workflows/gate-executable-docs.yml)
  (nightly, `workflow_dispatch`, and called by `release-train.yml` as the `executable-docs` gate)
- Tracking: [honua-release#379](https://github.com/honua-io/honua-release/issues/379),
  [#381](https://github.com/honua-io/honua-release/issues/381)

## What runs, and against what

**Documents.** `sources.json` lists the getting-started documents a new user follows: the SDK
`README.md`/`INSTALL.md`/quickstarts (JS, Python, .NET), the `@honua/mcp-server` and
`create-honua-app` READMEs, the server's *Get started* and SDK getting-started pages, this repo's
`docs/INSTALL-2026.1.md` and `docs/CUSTOMER-INSTALL.md`, the honua-samples READMEs and the site's
getting-started pages. A document leaves this list only through the `outOfScope` list, which gives
the reason. That list currently holds the native Linux and Windows package pages, which need a host
init system or Windows, so a container cannot stand in for the clean machine.

**Revisions.** Each document is read at the revision a reader actually gets:

| rule | revision |
|---|---|
| `clientArtifact` | `platform-manifest.yaml` `clientArtifacts.<key>.sourceSha`: the commit the published npm/PyPI/NuGet package and its README were built from |
| `component` | `components.<key>.sha`: the pinned component (the server docs) |
| `npmPublished` | `gitHead` of the package's npm `latest` (a package the manifest does not pin, such as `create-honua-app`) |
| `defaultBranch` | the default-branch head at run time: what the public site and samples serve today |
| `checkout` | this repository's checkout: the release docs that ship with the candidate |

**Candidate.** The runner boots `components.honua-server` `image@digest` through
`e2e/harness/boot.sh`, the same local-Docker stack that `e2e-local-docker` uses (licensing disabled,
asserted by `e2e/licensing.py`), and seeds it with `e2e/harness/seed`. A `HONUA_SERVER_IMAGE`
override that differs from the manifest is refused.

**Clean machines.** Each document (or each `session` of documents that continue one another, such
as the server quickstart → first dataset → first map) gets fresh containers from the digest-pinned
`runtimes` in `sources.json` (node, python, dotnet). The working directory, shell variables and the
current directory carry from one block to the next, as they would in the reader's terminal. Python
blocks share one interpreter per document, like a REPL or notebook. Documents marked `docker`
(those that start their own stack) get the Docker CLI from the pinned `docker:cli` image and run
first, before the candidate holds port 8080. The `boots-candidate-image` check then fails a document
whose stack ran a server image other than the candidate. A README inside a repository (`checkout`)
runs from a clone of that repository at the document's revision. When a document states a
prerequisite tool beyond its runtime ("Python with `honua-admin`, and Node.js with `npx`"),
`prerequisites` in `sources.json` records it with the citation. The pinned node runtime's
`node`/`npm`/`npx` are mounted into the session's other containers, and `jq` is installed from the
image's Debian archive. A tool that a document uses but does not list (for example `jq` in
`INSTALL-2026.1.md`) is not provided: the clean machine does not have it, and that is a finding.

**Only pinned packages.** npm, pip and NuGet in those containers point at the registry guard
(`registry_guard.py`). Third-party packages pass through. A Honua package (`@honua/*`, `@honua-io/*`,
`create-honua*`, `honua*` on npm; `honua*` on PyPI; `Honua.*` on NuGet) is visible only at the
version `clientArtifacts` pins, plus the dependency closure that a pinned package itself declares
(reported as `guardAdmittedClosure`). A doc that installs an old scope, a beta or an unpinned package
fails at install, and the report lists the refusal under `guardRefusals`.

## Results

Per block: `pass`, `fail` (with the stdout and stderr tails), or `needs-input`. Blocks that are not
meant to run are reported as `not-run` with their intent.

- **Oracle.** The exit code. Python blocks pass when they raise no exception. When the document
  shows the output (a following `text`/`output`/`json` block introduced by "prints", "returns",
  "output"…, a `console` transcript, or a `<!-- doc-run: output -->` block), that output is also
  asserted. Each shown line must appear in order, with digits and whitespace normalized; `...` elides.
  For JSON output, the shown object's keys must be present.
- **Continuations.** A C# or JS/TS block that fails only because it uses a name from an earlier
  block (`CS0103`, `ReferenceError`, `TS2304`) is re-run appended to the document's earlier passing
  blocks of that language, and the result is marked `+continues-earlier-blocks`.
- **Long-running commands.** `npm run dev`, `docker compose up` without `-d` and the like pass if
  they are still serving after 60 s, at which point the runner stops them.
- **`doc-test=compile`.** Fences that the SDK repos already mark compile-only are typechecked with
  the pinned TypeScript after the document's install steps have run.
- **needs-input.** The block reads an environment variable that nothing earlier sets, or contains a
  placeholder (`<your-api-key>`, `YOUR_TOKEN`, `https://your-server.example.com`), and the
  document's variables file has no value for it. The document gives the reader no way to run the
  block.

Document status is `fail` when any block or check fails, `needs-input` when a block needs a value,
and `pass` otherwise. The gate (`overall_status`) is `fail` when any document fails, `blocked` when
any document needs input, and `pass` only when every declared document runs. There is no waiver
list.

## Variables files

Some blocks need a value that the document tells the reader to supply. It might be the server URL,
an API key or a file path. That value comes from `certification/executable-docs/vars/<doc-id>.json`.
Every entry must cite where the document tells the reader about it, so the file can only hold
values the document already asks for:

```json
{
  "env": {
    "HONUA_API_KEY": {
      "value": "{candidate.apiKey}",
      "documentedAt": "README.md:181 — \"Mint a key with HONUA_ADMIN_PASSWORD on your server\""
    }
  },
  "substitute": {
    "https://your-honua-server.com": {
      "value": "{candidate.baseUrl}",
      "documentedAt": "README.md:179 — \"Admin needs a server of your own\""
    }
  }
}
```

Values may reference `{candidate.baseUrl}` (`http://localhost:8080`), `{candidate.grpcAddress}`, `{candidate.apiKey}`,
`{candidate.adminPassword}`, `{candidate.mcpUrl}`, `{candidate.image}`, `{session.appDir}` (the directory the session started in) and `{fixture.featureService}`/`{fixture.featureLayerId}` (a layer the candidate seed publishes, for "point this at one of your own layers"). `<doc-id>` is the
document's `id` in `inventory.json`.

## Marking a block in a document

Authors control intent in the document itself, with an HTML comment on the line(s) directly above
the fence (only blank lines may separate them). The comment is invisible when rendered:

```markdown
<!-- doc-run: skip reason="needs a browser with WebGL; covered by the maplibre-quickstart sample" -->
```

| marker | meaning |
|---|---|
| `<!-- doc-run: skip reason="..." -->` | Deliberately illustrative. The block is not run, and the inventory records it as `excluded` with the reason. **A skip without a reason is ignored and the block still runs.** |
| `<!-- doc-run: file=path/name.ext -->` | Save the block as this file (relative to the reader's current directory). Later blocks run it. If none mentions it, a runnable file is also run. |
| `<!-- doc-run: output -->` | This block is the expected output of the preceding runnable block. |
| `<!-- doc-run: expect-fail -->` | The command is meant to fail (for example, a validator shown rejecting a bad input): a non-zero exit passes, and exit 0 fails. Prose that says "deliberately broken" or "to see it catch/fail/reject" right above the block means the same. |
| `<!-- doc-run: teardown -->` | This block stops what the reader started. It runs when the reader is done, after the last document of the session. A shell block made only of `docker compose down/stop/rm` lines is treated this way without a marker. |
| `<!-- doc-run: run -->` | Run this block even though no heuristic says so (for example, an unlabelled fence). |
| `<!-- doc-run: checkout -->` / `checkout=sub/dir` | The block is run from a clone of the document's repository at the document's revision (optionally from `sub/dir`). Use it for contributor steps such as `npm install && npm run demo` in a repository README. |

The SDK repos' existing fence attributes are honoured: ` ```ts doc-test=skip reason="..." ` is an
exclusion with a reason, and ` ```ts doc-test=compile ` is typecheck-only. Without a marker the
runner infers intent. shell, python, js/ts, C# and http fences run. A fence after prose that names a
file ("save it as `compose.yaml`") is saved as that file. A `text`/`json` fence after "prints" or
"output" is expected output. PowerShell/cmd fences are recorded as Windows alternatives. Other
languages are illustrative. `intentSource` in the inventory shows whether intent came from a
marker, a fence attribute or the heuristic.

Skipping a block that fails is not a fix. The skip reason is published in the inventory and the
report. The right fix is a document a reader can follow.

## Running it locally

```sh
pip install "pyyaml>=6.0" pytest
python -m pytest certification/executable-docs -q            # offline self-tests

# regenerate / check the committed inventory (reads the docs at their release revisions)
GH_TOKEN=$(gh auth token) python certification/executable-docs/inventory.py --write
GH_TOKEN=$(gh auth token) python certification/executable-docs/inventory.py --check

# full run: boots the candidate on :8080, needs Docker and ~15 GB for the runtime images
GH_TOKEN=$(gh auth token) python certification/executable-docs/run.py --boot \
  --evidence-uri local --output artifacts/executable-docs/report.json \
  --summary artifacts/executable-docs/summary.md
```

On a host where something else already holds port 8080, boot the candidate under its own compose
project and port, then let the client documents share the candidate server's network namespace, so
`localhost:8080` inside the doc containers is the candidate:

```sh
export COMPOSE_PROJECT_NAME=honua-execdocs E2E_SERVER_PORT=18180 E2E_BASE=http://localhost:18180 \
       HONUA_PUBLIC_BASE_URL=http://localhost:8080
HONUA_SERVER_IMAGE="$(python3 -c 'import yaml;s=yaml.safe_load(open("platform-manifest.yaml"))["components"]["honua-server"];print(s["image"].split("@")[0]+"@"+s["digest"])')" \
  bash e2e/harness/boot.sh up && bash e2e/harness/seed/seed.sh
python certification/executable-docs/run.py --network candidate --evidence-uri local \
  --only honua-sdk-python-readme          # one document (repeat --only; a session name runs the session)
```

Documents marked `docker` always use the host network, because they publish their own ports.

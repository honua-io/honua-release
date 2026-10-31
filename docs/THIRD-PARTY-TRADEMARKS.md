# Third-party naming policy

Status: policy for every honua-io repository (honua-release#425, release plan honua-release#376).

Operator ruling, 2026-10-03: some third-party terms are unavoidable as exact GeoServices REST identifiers;
minimize every other use.

Ruling R30 (honua-release#376): desktop-client certification detail stays in the private certification
repository. Rulings R32 to R37 (honua-release#425) settle the vocabularies, the certification labels and
the site-root alias.

This document defines the identifier classes, what is never allowed, and how to request an exception.
`tools/check_vendor_terms.py` applies it to a checkout.
The `gate-vendor-terms` workflow fails a pull request that adds an avoidable use or carries any
confidential use. This is an engineering control; guidance from counsel takes precedence over it.

## Covered terminology

The classifier checks a controlled vocabulary of third-party company and product names, case-insensitively
and at whole-word boundaries. The exact patterns live in `tools/check_vendor_terms.py`; this page does not
duplicate them. Confidential entries are kept out of public documentation. This policy is an internal
engineering rule, not legal advice.

## The classes

| Class | What | Gate |
| --- | --- | --- |
| `spec` | GeoServices REST Specification 1.0 identifiers, used verbatim | allowed |
| `spec-interop` | later GeoServices REST wire values, each cited (R36) | allowed, reported separately |
| `nominative` | legacy attributed labels that still name a product | remove |
| `confidential` | desktop-client certification detail (R30) | always fails |
| `avoidable` | everything else | fails above the baseline |

### 1. `spec`: identifiers the specification requires, used verbatim

These are wire-format identifiers defined by the GeoServices REST Specification, Version 1.0
(white paper J-9948, September 2010, the version submitted to the OGC). Examples are
`esriGeometryPoint`, `esriFieldTypeOID`, `esriSpatialRelIntersects` and `esriJobSucceeded`. A client
sends or expects these exact strings, so we use them unchanged wherever they appear: request parsing,
response serialisation, fixtures and API docs.

The vocabulary is
[`tools/vendor-terms/geoservices-identifiers.v1.json`](../tools/vendor-terms/geoservices-identifiers.v1.json),
which `tools/vendor-terms/generate_geoservices_identifiers.py` generates from the pinned specification
PDF: every vendor-prefixed identifier the 1.0 text uses (146), each tagged with the numbered section it
comes from. Nothing else is in it.

Only an exact identifier listed in the specification vocabulary counts as `spec`. Honua type names and
URL aliases do not inherit that exception. The specification does not define URL roots.

### 2. `spec-interop`: later GeoServices REST wire values (R36)

The interop vocabulary covers later GeoServices REST values beyond version 1.0. Clients of later protocol
revisions send or expect some identifiers that the 1.0 text does not list, for example the newer
`fields[].type` members such as `esriFieldTypeGUID`, `jobStatus` values and the `esriSRUnit_*`
constants. Honua emits them for interoperability. They are in the versioned interoperability vocabulary.
Each entry names its family, the field it fills, the 1.0 section that defines that field, and the protocol
reference page that documents it (`reference`). `listedVerbatim` records whether that page spelled the
identifier out when it was checked. The classifier reports these identifiers as `spec-interop`, separately
from `spec`.

The vocabulary grows only with a cited entry. The classifier refuses an entry without a family, a field
and an authoritative protocol reference, and it refuses the whole file with it. An identifier without a
citation is an allowlist request, not interop.

The same file lists similar-looking identifiers that are not wire values. They remain avoidable and
should be corrected or renamed.

### 3. `nominative`: legacy labels to remove

Use protocol-neutral wording to describe compatibility, such as the protocol name, client category and
version. Do not name a third-party product:

- The statement identifies the protocol or client category and version without a third-party product
  name. Nothing about a path or directory changes this requirement.
- Do not claim endorsement, sponsorship, affiliation or partnership.
- Headings and titles use Honua and protocol-neutral terms.

Legacy product-name matches are reported for migration; attribution does not make a product name neutral.
Table cells, prose, and JSON/YAML labels follow the same rule.

### 4. `confidential`: desktop-client certification detail (R30)

Test recipes, fixtures, replay inputs, runner and licence configuration, and tool-specific code for
desktop-client compatibility live only in the private certification repository. Public repositories
reference those results by evidence identifier (bundle digest, cell id, pass or fail), and state
compatibility using protocol-neutral terms. The lint finds this detail anywhere in a public repository: in
code, config, tests, data, prose and paths.

| Category | What it matches |
| --- | --- |
| `desktop-scripting` | the desktop client's Python scripting module, anywhere, including imports and identifiers built on it |
| `desktop-file` | desktop project files, toolbox files and Python toolbox files, by extension |
| `runner-detail` | licence-manager hosts and variables, the desktop client's install paths and Python environment, and CI runner labels that carry a mark |
| `desktop-client` | third-party desktop product names |

Third-party desktop product names are avoided in all public material. Technical protocol identifiers
remain permitted only when the specification requires their exact spelling.

A confidential use always fails the gate. It is never baselined (the baseline counts avoidable uses
only), and it is never allowlisted (an allowlist entry does not apply to it). The way out is to move the
detail to the private repository or to remove it.

A repository that still carries confidential uses from before R30 lists them in a dated
`tools/vendor-terms/confidential-known.<repo>.json` ledger. The ledger records the date and the issues
that burn the uses down. It is separate from the baseline and may only shrink. The ledger never turns
the gate green. The reusable `gate-vendor-terms` workflow fails on every listed use and gives the ledger
as the reason. The repository's own validate run reports listed uses as warnings and fails on anything
the ledger does not list. honua-release's own ledger is burned down by honua-release#427 and
honua-server#5408.

### 5. `avoidable`: everything else

Everything outside the specification vocabulary is avoidable: our identifiers (classes, methods,
variables, enum members, constants), repository, package, module and namespace names, file and directory
names, test names, fixture and lane labels, comments, log and error messages, docs and marketing copy,
endorsement claims, and third-party product names. Use neutral terms such as
"GeoServices REST", "feature service", "desktop GIS client" or "reference client".

## Compatibility statements

Compatibility statements use protocol names, neutral client categories and tested versions. Do not add
third-party product names or boilerplate attribution that repeats them.

## Never allowed

- A mark inside a Honua product, package, repository, namespace, module, assembly, class, file, directory,
  test or fixture name. New
  names never carry a mark. Existing ones are the burn-down backlog in the inventory.
- Product or feature names derived from a third-party brand.
- Marketing copy, headings, titles, taglines or social posts that lead with a mark or use it as the hook.
- Third-party logos, icons or trade dress anywhere in Honua material.
- Any claim of endorsement, certification by a third party, partnership, sponsorship or affiliation.
- Desktop-client certification detail in a public repository (R30): see `confidential` above.
- Using a mark as a noun or verb for a generic concept.

## The classifier and the lint

```bash
# per-repo report: counts by class, every avoidable hit with path:line (JSON + Markdown)
python3 tools/check_vendor_terms.py scan --root ../honua-server --repo honua-server \
  --git-ref origin/trunk --json out.json --markdown out.md

# lint: red on a confidential use, or when any file has more avoidable hits than
# tools/vendor-terms/baseline.<repo>.json allows (--fail-on-known-confidential: red on ledger-listed uses too)
python3 tools/check_vendor_terms.py lint --root . --repo honua-release

# after a burn-down: lock the lower counts in
python3 tools/check_vendor_terms.py baseline --root ../honua-server --repo honua-server --git-ref origin/trunk
python3 tools/check_vendor_terms.py confidential-known --root . --repo honua-release \
  --as-of 2026-10-03 --burn-down honua-io/honua-release#427 --burn-down honua-io/honua-server#5408
```

- Baselines are per file. A new avoidable use in any file fails the lint, even when another file got
  cleaner.
- A baseline and a confidential-known ledger may only shrink. `check-baselines` runs in honua-release
  `validate` against the base ref. It fails when a total grows or differs from the sum of its entries,
  when any entry grows, or when any entry is new. Counts cannot be redistributed across files, not even
  by a rename: a renamed file carries its avoidable uses only once they are gone.
- Baselines and ledgers never name a confidential path, and the commands that write them refuse to.
- The committed inventory reports list avoidable hits only. Confidential hits appear as counts by
  category, and avoidable hits under a confidential path are counted but not listed.
- Lockfiles, source maps, binary files and bundled build output (`.js` or `.css` averaging more than
  1,000 characters a line) are skipped, and the report counts them. Other text files are scanned from
  tracked files and untracked files that are not ignored, or from the blobs of `--git-ref`. The scanner
  checks HTML entities, percent and common source escapes, and Unicode normalization; it fails closed on
  encoded or Unicode-containing lines over 1,000,000 characters, normalized lines over 1,000,000
  characters, or more than eight nested decoding passes. To bound matching work, a line with a term
  also fails above 512 term matches or with a matching token longer than 4,096 characters. On longer
  changed lines it may report a raw and decoded occurrence separately. A symlink's own path is classified;
  its target is not followed.
- Other repositories adopt the gate by calling the reusable workflow, once their baseline is committed
  here:

```yaml
jobs:
  vendor-terms:
    uses: honua-io/honua-release/.github/workflows/gate-vendor-terms.yml@<sha>
    with:
      tools_ref: <the same sha>
```

`tools_ref` is required and must be a full commit sha, never a branch such as `trunk`. The classifier,
vocabularies, allowlist, baselines and ledgers a caller runs therefore cannot change under an
already-pinned check.

### Install the event and audit callers

The reviewable examples in [`docs/examples/vendor-guard/`](examples/vendor-guard/)
provide separate per-repository caller workflows for event detection and the
scheduled REST audit. Their full SHA pins the reviewed reusable workflows and
trusted scanner code; **the pinned commit must first be available on GitHub**.
Update both pins together to a reviewed, reachable full commit SHA before
installation. Copy the examples to `.github/workflows/` on each target default
branch and verify Actions, workflow access policies, token read permissions, and
actual event delivery there. Installing these examples is a per-repository
change; they do not configure an organization ruleset or install themselves.

The event caller detects text only after publication and cannot block or remove
it. The audit caller scans only content its repository token can read; scheduled
runs cover one of 16 review shards per day. Check run outcomes and rerun every
missing shard with `workflow_dispatch` and `review_shard` in the range 0–15.
Disabled Actions, missed schedules, request budgets, repository visibility,
unsupported events and out-of-scope surfaces prevent a clean-run claim from
implying organization-wide coverage. The separate delta workflow is needed for
code and PR metadata checks; these callers do not enforce a branch ruleset.

### Guard newly changed content

`guard-vendor-event.yml` inspects issue titles and descriptions, issue comments,
pull request reviews and inline review comments, and commit comments when the
corresponding GitHub event is delivered. It reports only a generic result and
does not print the submitted text. Invalid or oversized payloads fail the check.
Specification and interoperability exceptions follow the same classifier used
for pull request metadata.
The event scanner also checks text joined across HTML markup and common inline
Markdown formatting. It is not a complete GitHub Markdown renderer, so unusual
rendering and encoding forms can evade or falsely trigger this detection.
The vocabulary includes a visual-workflow product name that can also occur as a
generic programming identifier. A lexical match is an audit signal, not a legal
conclusion. Code-delta findings can use the documented exception process where
appropriate; pull-request metadata and event checks do not apply that allowlist,
so a generic match there still requires manual review or rewording.

This is **post-publication detection**: the text is already visible when the
event runs. It does not prevent or delete a submission, and a failed comment
check does not block a pull request merge. Each repository needs a caller
workflow with the relevant event triggers and a pinned `workflow_call` reference;
the reusable workflow alone does not install organization-wide coverage. Events
missed while Actions is disabled or unavailable require a separate audit.
Commit-comment edits do not produce a supported event, and most activity made
with a workflow's repository token does not trigger another workflow. Periodic
API reconciliation is needed to detect those changes; this workflow does not
provide that reconciliation.

For a read-only REST sweep of currently visible GitHub content, run:

```bash
python3 tools/vendor_terms_github_audit.py --org honua-io --max-requests 1000
```

Use `--repo owner/name` to scope a run to one repository. The audit enumerates
issues, pull requests, conversation comments, inline review comments, commit
comments, and each pull request's review bodies. Its JSON lines contain item
URLs and per-surface coverage, never submitted text. Exit 1 means findings;
exit 2 means a request, payload, permission, or request-budget failure left the
sweep incomplete. Failed requests are not retried automatically. Results cover
only repositories and content visible to the authenticated `gh` account; compare
the repository inventory with an organization-owner inventory. Pagination is not
an atomic snapshot, so repeat or reconcile a sweep after concurrent changes.
Discussions and replies, release notes, git history, wikis, attachments, deleted
content, and hidden repositories are outside this REST sweep.
`audit-vendor-github.yml` schedules this sweep for its repository and can
be called by a pinned workflow in another repository. It is not an
organization-wide schedule: each repository needs its own scheduled caller and
read access to its issues, pull requests, and contents. To fit large repositories
within token limits, daily runs inspect one of 16 deterministic pull-request
review shards; other listed REST surfaces are inspected each run. A single
sharded run is not a complete review-body audit: all 16 successful shards are
needed, and missed days must be rerun. The command accepts `--review-shards`
and `--review-shard` to rerun a specific shard; without them it scans all reviews.
The scheduled workflow also accepts an optional `review_shard` input for a
manual or reusable-workflow rerun. A run that reports findings but no incomplete
endpoints scanned its selected shard; a failed job alone does not distinguish
findings from incomplete coverage, so inspect its summary record.
The 1,000-request budget,
GitHub rate limits, disabled Actions, and token permissions can leave a run
incomplete; its failure must not be interpreted as a clean audit.

`guard-vendor-delta.yml` is a reusable pull request and merge queue check. It compares the
candidate against the PR merge base (or merge queue base) without a committed baseline, classifies changed text in
its full file context, and rejects new avoidable, confidential and nominative uses. The
specification and interoperability vocabularies and approved exceptions still apply. New path
components are checked as well. Existing findings on unchanged lines do not block the check.
The check also examines HTML entities, percent encoding, and common source-code escapes, including nested
combinations up to a bounded depth; deeper encodings fail closed. An additional Unicode
normalization pass checks compatibility characters and ignores invisible format characters and
variation selectors. Unicode-containing lines over 100,000 characters and normalized changes
over 16 Mi characters fail closed to bound normalization memory use.
It cannot prove
what dynamically constructed strings or every language-specific encoding will produce at runtime.
Escape scanning is conservative: raw strings and nested escapes can be flagged even when the
language displays an escape literally or evaluates fewer decoding layers. Review an approved
exception when that occurs. Embedded names are checked conservatively; the overlap between a
preceding `e` and a capitalized `Srid` is exempted. An all-lowercase spatial-reference name can
still be ambiguous and may need an approved exception.

The check also inspects pull request titles and descriptions when a pull request event runs.
GitHub ruleset workflows ignore event activity filters, so a title or description edit after a
passing check may not rerun it. The check fails closed if either revision, a changed submodule,
Git LFS pointers, or changed text in a supported source file cannot be inspected. Changed blobs
over 64 MiB, more than 1,000,000 changed lines per file, candidate text over 16 MiB, tokens over 4,096 characters, and inputs with excessive inspection work fail closed to bound
resource use. Recognized binary assets are checked for plainly encoded terms, but compressed and
visual content is not inspected. Other binary content fails closed. It does not inspect issue
titles, comments, or review text. These require a separate
event-driven integration; a branch ruleset alone cannot prevent their submission. Install and
verify the workflow in the target repositories before making its status mandatory; a required
check that never runs would block merges.

```bash
python3 tools/vendor_terms_delta.py --root ../honua-server --repo honua-server \
  --base <target-commit> --head <candidate-commit>
```

## Requesting an exception

Add an entry to [`tools/vendor-terms/allowlist.json`](../tools/vendor-terms/allowlist.json) in a
honua-release pull request:

```json
{
  "repo": "honua-server",
  "paths": ["src/Honua.Server/GeoServices/Legacy/**"],
  "tokens": ["VendorShim"],
  "reason": "why this use cannot be renamed or attributed",
  "owner": "@github-handle accountable for revisiting it",
  "issue": "honua-io/<repo>#<n>"
}
```

`repo`, `paths`, `reason` and `owner` are required. The classifier refuses an entry that lacks any of
them. It also refuses a wildcard entry: `repo` must name one repository, and no path may be wildcards
alone, such as `**` or `**/*`. `tokens` narrows the entry to specific tokens. Excepted hits stay
`avoidable` in the report and are marked `[excepted]`, but they do not count against the lint. An
allowlist entry never applies to a `confidential` hit. An exception is reviewed like any other
release-control change and is removed when its reason no longer holds.

Optional `identifiers` narrows an exception further to exact, case-sensitive enclosing lexical
identifiers, using letters, digits, `@`, `.`, `_`, `/` and `-`. Use this for published package
coordinates and exact tarball filenames that cannot change without selecting different registry
bytes. It does not cover substrings, prose marks, other paths, endorsement claims or confidential
hits. Versioned filenames must be reviewed again when their version changes.

### Legacy site-root alias (R35)

honua-server keeps a legacy site-root alias for client compatibility. `/rest` is the canonical root. The
allowlist entry is limited to the two route tables that register that alias. The compatibility matrix lists
it as a compatibility feature.

## Inventory

Reports per repository and commit are in `tools/vendor-terms/reports/<repo>.<sha>.md`. The summary, and
the renaming decisions that need an operator ruling, are on honua-release#425.

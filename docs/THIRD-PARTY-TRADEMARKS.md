# Third-party trademarks: Esri and ArcGIS terms

Status: policy for every honua-io repository (honua-release#425, release plan honua-release#376).

Operator ruling, 2026-10-03: some Esri and ArcGIS terms are unavoidable because they are part of the
GeoServices REST specification, but we minimise every other use for legal reasons.

Ruling R30 (honua-release#376): desktop-client certification detail stays in the private certification
repository. Rulings R32 to R37 (honua-release#425) settle the vocabularies, the certification labels and
the site-root alias.

This document defines the classes of use, the attribution that compatibility statements need, what is
never allowed, and how to request an exception. `tools/check_vendor_terms.py` applies it to a checkout.
The `gate-vendor-terms` workflow fails a pull request that adds an avoidable use or carries any
confidential use. This is an engineering control; guidance from counsel takes precedence over it.

## Marks covered

`Esri`, `ArcGIS`, and Esri product names, including the desktop client, ArcGIS Online, ArcGIS Enterprise,
Portal for ArcGIS, ArcGIS Server, ArcGIS Desktop, ArcGIS Maps SDKs (including the former ArcGIS API for
JavaScript and ArcGIS Runtime), ArcGIS Living Atlas, Esri Leaflet, ArcMap, ArcCatalog, the desktop
client's Python scripting module, ArcObjects, ArcSDE and ArcIMS. Matching is case-insensitive and looks at
whole words, so `EsriFeatureLayer`, `esri_path` and `ARCGIS_URL` all count, but `SourceSrid` does not.
The exact patterns are in `tools/check_vendor_terms.py`. This page does not spell out the confidential
ones, because the lint applies to this page too.

Esri publishes its own terms for its marks on its
[copyright and trademarks page](https://www.esri.com/en-us/legal/copyright-proprietary-rights). This
policy links to those terms rather than restating them.

## The classes

| Class | What | Gate |
| --- | --- | --- |
| `spec` | GeoServices REST Specification 1.0 identifiers, used verbatim | allowed |
| `spec-interop` | later ArcGIS REST wire values, each cited (R36) | allowed, reported separately |
| `nominative` | attributed compatibility statements and stamped labels | allowed |
| `confidential` | desktop-client certification detail (R30) | always fails |
| `avoidable` | everything else | fails above the baseline |

### 1. `spec`: identifiers the specification requires, used verbatim

These are wire-format identifiers defined by the
[GeoServices REST Specification, Version 1.0](https://www.esri.com/~/media/files/pdfs/library/whitepapers/pdfs/geoservices-rest-spec.pdf)
(white paper J-9948, September 2010, the version submitted to the OGC). Examples are
`esriGeometryPoint`, `esriFieldTypeOID`, `esriSpatialRelIntersects` and `esriJobSucceeded`. A client
sends or expects these exact strings, so we use them unchanged wherever they appear: request parsing,
response serialisation, fixtures and API docs.

The vocabulary is
[`tools/vendor-terms/geoservices-identifiers.v1.json`](../tools/vendor-terms/geoservices-identifiers.v1.json),
which `tools/vendor-terms/generate_geoservices_identifiers.py` generates from the pinned specification
PDF: every `esri*` identifier the 1.0 text uses (146), each tagged with the numbered section it comes
from. Nothing else is in it.

Only the exact identifier counts as `spec`. Our own types named after it, such as `EsriGeometryType` or
`EsriFieldTypeMapper`, are avoidable. The specification defines no JSON key and no URL path segment that
contains a mark. In particular, the `/arcgis/rest/services` URL root is a naming convention for server
instances, not a specification requirement (see the site-root alias below).

### 2. `spec-interop`: later ArcGIS REST wire values (R36)

The interop vocabulary is later ArcGIS REST, not GeoServices REST 1.0. Clients of later ArcGIS REST
services send or expect some identifiers that the 1.0 text does not list, for example the newer
`fields[].type` members such as `esriFieldTypeGUID`, `jobStatus` values and the `esriSRUnit_*`
constants. Honua emits them for interoperability. They are in
[`tools/vendor-terms/arcgis-rest-interop.v1.json`](../tools/vendor-terms/arcgis-rest-interop.v1.json).
Each entry names its family, the field it fills, the 1.0 section that defines that field, and the ArcGIS
REST reference page that documents it (`reference`). `listedVerbatim` records whether that page spelled the
identifier out when it was checked. The classifier reports these identifiers as `spec-interop`, separately
from `spec`.

The vocabulary grows only with a cited entry. The classifier refuses an entry without a family, a field
and a `developers.arcgis.com` reference, and it refuses the whole file with it. An identifier without a
citation is an allowlist request, not interop.

The same file lists identifiers that are not wire values. They stay avoidable under a category that says
what to do about them:

- `avoidable/non-wire-identifier`: `esriGeometryNull`, `esriSpatialRelWithinDistance` and
  `esriSpatialRelBeyondDistance`. Rename them.
- `avoidable/wrong-wire-value`: `esriMosaicByAttribute`. The wire value is `esriMosaicAttribute`, so
  correct it.

### 3. `nominative`: compatibility statements and labels with attribution

We may name an Esri product to say truthfully that Honua works with it. Examples are "Honua works with
ArcGIS Pro 3.3", "tested with ArcGIS Maps SDK for .NET 200.x", a compatibility-matrix row, or a client
label in certification data. These uses are allowed when all of the following hold:

- The same file carries the attribution below. Nothing else makes a use nominative: not a path, a file
  name or a directory such as `matrix/` or `compatibility/`.
- The mark names the product as plain text, with no logo and no stylisation, and only as much of it as
  identifies the product.
- The line claims no endorsement, sponsorship, affiliation or partnership. The lint checks every line
  for this before anything else. A line with words such as "endorsed by", "sponsored by", "affiliated
  with", "official", "certified by", "approved by", "partner" or "powered by" is
  `avoidable/endorsement-claim`, even in an attributed file. The only exception is the attribution
  notice's own disclaimer.
- The mark does not lead: no heading, page title, product tagline or marketing sentence starts with it.
  Honua is the subject, and the Esri product is the thing it works with.
- The mark is a whole word. Inside code blocks, inline code, URLs, paths or compound names it is not a
  compatibility statement.

Three shapes qualify:

- **Prose**: a docs or site line with compatibility language ("works with", "tested with",
  "compatible", "supports", and similar).
- **Table rows**: a Markdown or HTML table cell that holds only the product name, optionally followed by
  a version. Other prose in the same page still needs the compatibility language.
- **Labels in data files (R37)**: a JSON or YAML value that is only the product name, optionally followed
  by a version, such as `"canonical_client": "ArcGIS Maps SDK for .NET"`. The file's header must carry the
  attribution: a top-level `trademarkNotice` field (JSON or YAML) or the YAML file's leading comment
  block. The certification generator stamps its output this way. Certification data is JSON or YAML under
  `certification/`, or a file whose `schema` names a Honua certification schema, and its labels are
  reported as `certification-label`. An unstamped file's labels stay avoidable. A stamp makes the label
  nominative, not the detail around it, so desktop-client detail in the same file stays confidential.

### 4. `confidential`: desktop-client certification detail (R30)

Test recipes, fixtures, replay inputs, runner and licence configuration, and tool-specific code for
desktop-client compatibility live only in the private certification repository. Public repositories
reference those results by evidence identifier (bundle digest, cell id, pass or fail), and state
compatibility only in nominative form. The lint finds this detail anywhere in a public repository: in
code, config, tests, data, prose and paths.

| Category | What it matches |
| --- | --- |
| `desktop-scripting` | the desktop client's Python scripting module, anywhere, including imports and identifiers built on it |
| `desktop-file` | desktop project files, toolbox files and Python toolbox files, by extension |
| `runner-detail` | licence-manager hosts and variables, the desktop client's install paths and Python environment, and CI runner labels that carry a mark |
| `desktop-client` | the desktop client's name, unless it is part of a nominative use |

The desktop client's name is the one term that falls on both sides. In an attributed compatibility
statement or a stamped label, it names the product Honua works with, so it is nominative:
"Honua works with ArcGIS Pro 3.3" is such a statement.
The same name in code, config, tests, data or unattributed prose is confidential, and so is a spelling of
it fused into an identifier, a path or a lane name.

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

Everything else is avoidable: our identifiers outside the specification vocabulary (classes, methods,
variables, enum members, constants), repository, package, module and namespace names, file and directory
names, test names, fixture and lane labels, comments, log and error messages, docs and marketing copy,
endorsement claims, and any compatibility statement that lacks the attribution. Replace the mark with a
neutral term such as
"GeoServices", "feature service", "desktop GIS client" or "the reference client". Otherwise, turn the
mention into an attributed compatibility statement, or request an exception.

## Required attribution

Every file that carries a nominative compatibility statement includes this text verbatim, as plain text.
In docs it goes at the end of the page. On the site it goes in the page source of each page that names a
product. In a compatibility matrix it goes in a comment or a `trademarkNotice` field.

> Esri, ArcGIS, and the Esri product names used here are trademarks, registered trademarks, or service
> marks of Esri in the United States and other countries. They identify the products Honua is compatible
> with; Honua is not affiliated with, sponsored by, or endorsed by Esri.

The classifier recognises the attribution by its two clauses: "are trademarks, registered trademarks, or
service marks of Esri" and "not affiliated with, sponsored by, or endorsed by Esri". Line breaks and
markdown emphasis do not matter.

## Never allowed

- A mark inside a Honua product, package, repository, namespace, module, assembly, class, file, directory,
  test or fixture name (for example `honua-esri-*`, `Honua.Esri.*`, `EsriFooService`, `test_esri_*`). New
  names never carry a mark. Existing ones are the burn-down backlog in the inventory.
- Product or feature names built on Esri's "Arc" naming family.
- Marketing copy, headings, titles, taglines or social posts that lead with a mark or use it as the hook.
- Esri logos, icons or trade dress anywhere in Honua material.
- Any claim of endorsement, certification by Esri, partnership, sponsorship or affiliation.
- Desktop-client certification detail in a public repository (R30): see `confidential` above.
- Using a mark as a noun or verb for a generic concept, for example "an arcgis" for a feature service or
  "esri-style" for a symbol.

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
  1,000 characters a line) are skipped, and the report counts them. Every other text file is scanned,
  whatever its size: tracked files plus untracked files that are not ignored, or the blobs of `--git-ref`.
  A symlink's own path is classified like any other path; its target is not followed.
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

## Requesting an exception

Add an entry to [`tools/vendor-terms/allowlist.json`](../tools/vendor-terms/allowlist.json) in a
honua-release pull request:

```json
{
  "repo": "honua-server",
  "paths": ["src/Honua.Server/GeoServices/Legacy/**"],
  "tokens": ["EsriShim"],
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

### The `/arcgis` site-root alias (R35)

honua-server keeps `/arcgis` as a documented site-root alias for client compatibility, because some
clients hard-code the vendor path convention. `/rest` is the canonical root. The allowlist entry covers
the token `arcgis` in the two route tables that register the alias, and nothing else. The compatibility
matrix lists the alias as a compatibility feature.

## Inventory

Reports per repository and commit are in `tools/vendor-terms/reports/<repo>.<sha>.md`. The summary, and
the renaming decisions that need an operator ruling, are on honua-release#425.

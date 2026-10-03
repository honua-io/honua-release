# Third-party trademarks: Esri and ArcGIS terms

Status: policy for every honua-io repository (honua-release#425, release plan honua-release#376).

Operator ruling, 2026-10-03: some Esri and ArcGIS terms are unavoidable because they are part of the
GeoServices REST specification, but we minimise every other use for legal reasons.

This document defines the three classes of use, the attribution that compatibility statements need,
what is never allowed, and how to request an exception. `tools/check_vendor_terms.py` applies it to a
checkout and the `gate-vendor-terms` workflow fails a pull request that adds an avoidable use. This is
an engineering control; guidance from counsel takes precedence over it.

## Marks covered

`Esri`, `ArcGIS`, and Esri product names, including ArcGIS Pro, ArcGIS Online, ArcGIS Enterprise,
Portal for ArcGIS, ArcGIS Server, ArcGIS Desktop, ArcGIS Maps SDKs (including the former ArcGIS API for
JavaScript and ArcGIS Runtime), ArcGIS Living Atlas, Esri Leaflet, ArcMap, ArcCatalog, ArcPy, ArcObjects,
ArcSDE and ArcIMS. Matching is case-insensitive and looks at whole words, so `EsriFeatureLayer`,
`esri_path` and `ARCGIS_URL` all count, but `SourceSrid` does not.

Esri publishes its own terms for its marks on its
[copyright and trademarks page](https://www.esri.com/en-us/legal/copyright-proprietary-rights). This
policy links to those terms rather than restating them.

## The three classes

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
PDF. Each identifier records the numbered section it comes from. It has two sources:

- `gsr-1.0`: every `esri*` identifier the specification text uses, tagged with its section.
- `wire-supplement`: members of enumerations that the specification defines but does not list in full,
  for example `fields[].type`, `jobStatus` and the `esriSRUnitType` constants that the specification
  points to. Each family names the section and field it extends. Families are reviewed by hand in
  [`geoservices-wire-supplement.v1.json`](../tools/vendor-terms/geoservices-wire-supplement.v1.json), and
  adding one needs a specification section that defines the field.

Only the exact identifier counts as `spec`. Our own types named after it, such as `EsriGeometryType` or
`EsriFieldTypeMapper`, are avoidable. The specification defines no JSON key and no URL path segment that
contains a mark. In particular, the `/arcgis/rest/services` URL root is a naming convention for server
instances, not a specification requirement.

### 2. `nominative`: compatibility statements with attribution

We may name an Esri product to say truthfully that Honua works with it, for example "Honua works with
ArcGIS Pro 3.3", "tested with ArcGIS Maps SDK for .NET 200.x", or a row in the compatibility matrix.
These statements are allowed in docs, on the site and in compatibility matrices when all of the
following hold:

- The same file carries the attribution below.
- The mark names the product as plain text, with no logo and no stylisation, and only as much of it as
  identifies the product.
- Nothing implies endorsement, sponsorship, certification by Esri, partnership or affiliation. Words
  such as "official", "certified by", "approved by", "partner" or "powered by" next to a mark make the
  line avoidable.
- The mark does not lead: no heading, page title, product tagline or marketing sentence starts with it.
  Honua is the subject, and the Esri product is the thing it works with.
- The mark is a whole word in prose. Inside code blocks, inline code, URLs, paths or compound names it is
  not a compatibility statement.

### 3. `avoidable`: everything else

Everything else is avoidable: our identifiers outside the specification vocabulary (classes, methods,
variables, enum members, constants), repository, package, module and namespace names, file and directory
names, test names, fixture and lane labels, comments, log and error messages, docs and marketing copy,
and any compatibility statement that lacks the attribution. Replace the mark with a neutral term such as
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
- Using a mark as a noun or verb for a generic concept, for example "an arcgis" for a feature service or
  "esri-style" for a symbol.

## The classifier and the lint

```bash
# per-repo report: counts by class, every avoidable hit with path:line (JSON + Markdown)
python3 tools/check_vendor_terms.py scan --root ../honua-server --repo honua-server \
  --git-ref origin/trunk --json out.json --markdown out.md

# lint: red when any file has more avoidable hits than tools/vendor-terms/baseline.<repo>.json allows
python3 tools/check_vendor_terms.py lint --root . --repo honua-release

# after a burn-down: lock the lower count in
python3 tools/check_vendor_terms.py baseline --root ../honua-server --repo honua-server --git-ref origin/trunk
```

- Baselines are per file. A new avoidable use in any file fails the lint, even when another file got
  cleaner.
- A baseline may only shrink. `check-baselines` runs in honua-release `validate` against the base ref.
  It fails when a baseline's total grows, or when an entry grows without a net shrink of the total. A net
  shrink covers the case where renaming `EsriLayer.cs` removes its path hit and the file's remaining hits
  move to the new name.
- Lockfiles, source maps, binary files and bundled build output (`.js` or `.css` averaging more than
  1,000 characters a line) are skipped, and the report counts them. Every other text file is scanned:
  tracked files plus untracked files that are not ignored, or the blobs of `--git-ref`.
- Other repositories adopt the gate by calling the reusable workflow, once their baseline is committed
  here:

```yaml
jobs:
  vendor-terms:
    uses: honua-io/honua-release/.github/workflows/gate-vendor-terms.yml@<sha>
```

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
them. `tokens` narrows the entry to specific tokens. Excepted hits stay `avoidable` in the report and are
marked `[excepted]`, but they do not count against the lint. An exception is reviewed like any other
release-control change and is removed when its reason no longer holds.

## Inventory

Reports per repository and commit are in `tools/vendor-terms/reports/<repo>.<sha>.md`. The summary, and
the renaming decisions that need an operator ruling, are on honua-release#425.

# Desktop scripting qualification

The 2026.1 execution wave requires 950 cells: 477 for the licensed
scripting client and 473 for PyQGIS. The original grids retain all 964 applicable
cells, including 14 SensorThings preview cells targeted for 2026.2. Deferral is a
scope decision, not a passing result or a claim that the client cannot address a
function. The UI drivers and the platform release obligations remain separate.

`certification/sources/desktop-scripting-scope.v1.json` records the immutable
baseline revisions, grid hashes, complete cell-identity hashes, and the exact
preview cells outside this execution wave. Any change to applicable identities
requires a reviewed scope revision. No individual client limitation is approved
in this version.

Run `tools/desktop_qualification.py` with the two producer grid files, the selected
candidate digest, and a new output path. It retains each original outcome and
citation, reports the required and deferred sets separately, and rejects missing,
duplicate, unknown or newly inapplicable cells. Prior-candidate passes remain
outstanding for the selected candidate. The report can contain private producer
evidence references and belongs with the producer's restricted evidence bundle.
The report records input file hashes. Baseline revisions document the reviewed
denominator; this tool checks identities and does not authenticate Git provenance.

This is a work-progress report. It does not certify a release, validate receipts,
change the protocol denominator, or modify the annotation-only gate exception
ledger. A qualified claim still requires the existing protocol gate to accept
fresh complete receipts against the selected signed platform lock. Nightly
selection, lock creation, burn and promotion follow the release plan; a diagnostic
development image is not a substitute for that lock.

A proposed stock-client limitation must identify the exact whole cell and client
version, preserve the failed evidence, demonstrate the limitation with native
observations and protocol controls, and cite the upstream implementation or
issue. Approval must produce a versioned release-scope amendment and corresponding
producer/consumer contract changes before the cell can leave the required set.
Neither an issue annotation nor a partially passing operation removes a cell.

Complete this wave by fixing measured server and harness defects, enrolling
missing complete producers, satisfying authentication and fixture prerequisites,
then replaying every required cell on the qualifying candidate. Report new cell
closures separately from merged fixes and repeated passes.

## Native completion contract

`tools/native_completion_contract.py` reconciles the fixed scripting scope with
both current producer grids and the complete PyQGIS registry and profile. It
writes `original-scope.json` for native campaign preparation and `backlog.json`
to a new output directory. Every input is accompanied by its exact SHA-256:

```powershell
python tools/native_completion_contract.py `
  --scope <scope.json> --scope-sha256 <sha256> `
  --licensed-grid <esri-grid.json> --licensed-grid-sha256 <sha256> `
  --qgis-grid <qgis-grid.json> --qgis-grid-sha256 <sha256> `
  --registry <native-certification-cases.json> --registry-sha256 <sha256> `
  --profile <native-certification-wave.json> --profile-sha256 <sha256> `
  --requirements <protocol-certification-requirements.v1.json> --requirements-sha256 <sha256> `
  --candidate-digest sha256:<digest> --integration-source <release-commit-sha> `
  --output <new-directory>
```

Supply `--arc-projection` and `--arc-projection-sha256` only for an authoritative
`honua.native-accounting-inventory/v1` projection produced by the Arc accounting
validator. Without it, Arc cells remain unknown. The projection must contain the
exact original Arc cell set and all declared children must reference its cases.
The supplied integration commit is recorded in the generated scope. Existing Arc
inventories pin the exact scope bytes and integration commit; regenerate and
review those inventories when using a newly generated scope.

Release bindings are optional and are never inferred from similar names. A
reviewed JSON file with schema `honua.native-release-bindings/v1` contains a
nonempty `review` reference, `input_sha256` equal to the report's input pin map
excluding the bindings file itself, and `bindings`, an array of objects with
`requirement_id` and nonempty `cell_ids`. Requirement IDs are SHA-256 of the
canonical JSON encoding of the release gate's normalized requirement signature,
and are present in the backlog. A binding must reference an addressable
scripting requirement and original required cells of the same driver. Pass the
file and its hash via `--bindings` and `--bindings-sha256`. A binding is a mapping,
not proof that its native assertions passed or that the release gate accepted
receipts. Unmapped producer children, unproved fixtures and missing reviewed
release mappings stay visible; all release requirements and deferred cells are
retained separately from the 950-cell execution scope.

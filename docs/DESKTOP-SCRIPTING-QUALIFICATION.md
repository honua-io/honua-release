# Desktop scripting qualification

The 2026.1 execution scope contains 950 applicable cells: 477 for the licensed
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

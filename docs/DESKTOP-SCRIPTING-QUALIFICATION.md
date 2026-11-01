# QGIS scripting qualification

The 2026.1 QGIS scripting execution wave requires 473 cells. The original grid
retains 487 applicable cells, including 14 SensorThings preview cells targeted
for 2026.2. Deferral is a scope decision, not a passing result or a claim that
the client cannot address a function.

`certification/sources/desktop-scripting-scope.v1.json` records the immutable
baseline revision, grid hash, complete cell-identity hash, and exact preview
cells outside this execution wave. A change to applicable identities requires
a reviewed scope revision.

Run `tools/desktop_qualification.py` with the QGIS producer grid, selected
candidate digest, and a new output path. It retains each original outcome and
citation, reports the required and deferred sets separately, and rejects
missing, duplicate, unknown, or newly inapplicable cells. Prior-candidate passes
remain outstanding for the selected candidate. The report can contain private
producer evidence references and belongs with the producer's restricted
evidence bundle.

This progress report does not certify a release, validate receipts, change the
protocol denominator, or modify the annotation-only gate exception ledger. A
qualified claim still requires the protocol gate to accept fresh, complete
receipts against the selected signed platform lock.

"""Phase A local-docker E2E seam harness.

The runner is parameterized by the platform manifest so it tests the exact pinned component set — it
is the *executable* form of the compatibility matrix (see docs/TEST-STRATEGY.md). It brings up the real
honua-server + DB via docker-compose, installs the SDKs from a staging source, runs the canonical
scenarios with NO mocks at the seam, and emits a machine-readable gate-report.json.

Guiding principle (AGENTS.md): a gate must be able to FAIL. Scenarios that depend on real published
images/metrics that do not yet exist report BLOCKED (not PASS); with E2E_REQUIRE_REAL=1 (the release
train) a BLOCKED/SKIPPED becomes a hard failure.
"""

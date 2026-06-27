# bom/

SBOM + provenance aggregation. Each component emits a per-artifact SBOM (CycloneDX/SPDX); the release train
aggregates them into a **platform BOM** for the release label, plus SLSA-style provenance attestations and
signatures (keyless via OIDC). Shipped with the release so customers can scan exactly what they run. Stub.

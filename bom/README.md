# bom/

SBOM + provenance aggregation. Each component emits a per-artifact SBOM (CycloneDX/SPDX); the release train
aggregates them into a **platform BOM** for the release label, plus SLSA-style provenance attestations and
signatures (keyless via OIDC). Shipped with the release so customers can scan exactly what they run.

**Wired:** `tools/generate_bom.py` builds the platform BOM (**CycloneDX 1.5**) from the certified
candidate's digest-bound `platform-manifest.yaml` — every component as a CycloneDX component with its type
(container / library / application), version, purl (npm/pypi/nuget/oci/terraform), pinned git SHA, image,
and contract surfaces.
Deterministic (serial number derived from the component set), so it is reproducible + diff-able. At promote
(`.github/workflows/promote.yml`) the BOM is generated, **keyless-signed** (cosign / Fulcio + Rekor) by the
workflow's OIDC identity, and attached to the `Honua YYYY.N` GitHub Release alongside its `.sig` + `.pem`.
Unit-tested in `tools/test_generate_bom.py`.

**Wired (gate f):** `.github/workflows/gate-sbom.yml` (reusable, run by the release train) generates a
**per-artifact SBOM of the pinned server image via Syft in BOTH CycloneDX and SPDX**, plus the aggregated
platform BOM (above), landing everything in `bom/` as run artifacts. It self-skips (blocked, bootstrap)
when the image can't be pulled, and can genuinely fail (empty/failed SBOM, malformed manifest).

**Still a stub:** ingesting each SDK/chart/iac component's *own* per-artifact SBOM so the platform BOM
nests transitive deps (today only the server image is Syft-scanned per-artifact; the rest are covered by
the manifest-derived aggregate), and SLSA provenance attestations beyond the keyless blob signatures.

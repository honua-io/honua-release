from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_rel_001_dependency_security_receipts_are_not_excluded_without_coverage():
    policy = yaml.safe_load((ROOT / "certification/security-checks.yaml").read_text())
    assert policy == {}


def test_rel_002_image_gates_bind_scans_to_manifest_digest():
    for name in ("gate-security.yml", "gate-sbom.yml"):
        workflow = (ROOT / ".github/workflows" / name).read_text()
        if name == "gate-security.yml":
            assert "component: [honua-server, honua-console]" in workflow
            assert "architecture: [amd64, arm64]" in workflow
            assert "c['platformDigests']['${{ matrix.architecture }}']" in workflow
        else:
            assert 'c[\'digest\']' in workflow
            assert 'print(f"{image}@{c[\'digest\']}")' in workflow


def test_rel_003_promotion_passes_minting_time_to_freshness_check():
    workflow = (ROOT / ".github/workflows/promote.yml").read_text()
    assert '--certification-time "${{ steps.train.outputs.updated_at }}"' in workflow


def test_rel_004_security_findings_is_a_required_train_gate():
    workflow = (ROOT / ".github/workflows/release-train.yml").read_text()
    assert "gate_security_findings:" in workflow
    assert "security-findings|$S_SECURITY_FINDINGS" in workflow

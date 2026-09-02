import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import upgrade_lock_binding as binding  # noqa: E402


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _lock(path: Path, name: str, image: str, source: str, schema: str = "107") -> dict:
    value = {
        "components": {
            "honua-server": {"schemaVersions": {"database": schema}, "migrationJournalSha256": _digest("journal"), "artifacts": [{
                "kind": "image", "coordinate": "ghcr.io/honua-io/honua-server", "version": "1.0.0",
                "digest": image, "platformDigests": {"amd64": image}, "sourceRevision": source,
            }]},
            "honua-helm": {"artifacts": [{
                "kind": "oci-chart", "coordinate": "ghcr.io/honua-io/charts/honua", "version": "1.4.0",
                "digest": _digest("chart-manifest"), "sha256": _digest("chart-package"), "sourceRevision": "c" * 40,
            }]},
        }
    }
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    return value


def _case(tmp_path: Path):
    prior_path, candidate_path = tmp_path / "a.json", tmp_path / "b.json"
    a_img, b_img = _digest("image-a"), _digest("image-b")
    a, b = _lock(prior_path, "a", a_img, "a" * 40), _lock(candidate_path, "b", b_img, "b" * 40)
    chart = binding.chart_binding(b)
    evidence = {
        "architecture": "amd64", "priorLockDigest": binding.bytes_digest(prior_path),
        "candidateLockDigest": binding.bytes_digest(candidate_path),
        "lockValidation": {"prior": "verified", "candidate": "verified"}, "chart": chart,
        "phases": {
            "install": {"imageID": f"docker-pullable://x@{a_img}", "runtimeIdentity": {"imageDigest": a_img, "sourceRevision": "a" * 40, "observedVersion": "1.0.0+aaaaaaaa", "platformLockDigest": binding.bytes_digest(prior_path)}},
            "candidate": {"imageID": f"docker-pullable://x@{b_img}", "runtimeIdentity": {"imageDigest": b_img, "sourceRevision": "b" * 40, "observedVersion": "1.0.0+bbbbbbbb", "platformLockDigest": binding.bytes_digest(candidate_path)}},
            "rollback": {"imageID": f"docker-pullable://x@{a_img}", "runtimeIdentity": {"imageDigest": a_img, "sourceRevision": "a" * 40, "observedVersion": "1.0.0+aaaaaaaa", "platformLockDigest": binding.bytes_digest(prior_path)}, "databaseSchema": "107"},
        },
        "schema": {"observed": "107", "journalSha256": _digest("journal"), "declaredJournalSha256": _digest("journal")},
        "seededData": {"checksumsMatched": True, "rollbackQueryPassed": True},
    }
    return prior_path, candidate_path, evidence


def test_exact_lock_a_to_b_to_a_with_forward_schema_passes(tmp_path):
    prior, candidate, evidence = _case(tmp_path)
    receipt = binding.verify(prior, candidate, evidence)
    assert receipt["classification"] == "exact-lock-upgrade-rollback-certified"
    assert receipt["phases"]["rollback"]["databaseSchema"] == "107"


def test_retagged_candidate_bytes_fail_mechanically(tmp_path):
    prior, candidate, evidence = _case(tmp_path)
    evidence["phases"]["candidate"]["imageID"] = "containerd://x@" + _digest("retagged")
    with pytest.raises(binding.BindingError, match="candidate imageID"):
        binding.verify(prior, candidate, evidence)


def test_stale_kind_image_with_right_tag_fails(tmp_path):
    prior, candidate, evidence = _case(tmp_path)
    evidence["phases"]["install"]["imageID"] = "containerd://x@" + _digest("stale")
    with pytest.raises(binding.BindingError, match="install imageID"):
        binding.verify(prior, candidate, evidence)


def test_understated_schema_fails_instead_of_using_a_floor(tmp_path):
    prior, candidate, evidence = _case(tmp_path)
    evidence["schema"]["observed"] = "108"
    with pytest.raises(binding.BindingError, match="not exactly"):
        binding.verify(prior, candidate, evidence)


def test_default_branch_chart_drift_fails_package_binding(tmp_path):
    prior, candidate, evidence = _case(tmp_path)
    evidence["chart"]["sha256"] = _digest("floating-head-package")
    with pytest.raises(binding.BindingError, match="chart"):
        binding.verify(prior, candidate, evidence)


@pytest.mark.parametrize("phase", binding.PHASES)
def test_runtime_identity_must_match_every_phase(tmp_path, phase):
    prior, candidate, evidence = _case(tmp_path)
    evidence["phases"][phase]["runtimeIdentity"]["sourceRevision"] = "f" * 40
    with pytest.raises(binding.BindingError, match=f"{phase} runtime source"):
        binding.verify(prior, candidate, evidence)


def test_lock_byte_reformatting_invalidates_binding(tmp_path):
    prior, candidate, evidence = _case(tmp_path)
    candidate.write_text(candidate.read_text() + "\n", encoding="utf-8")
    with pytest.raises(binding.BindingError, match="exact lock bytes"):
        binding.verify(prior, candidate, evidence)


def test_unverified_lock_is_refused(tmp_path):
    prior, candidate, evidence = _case(tmp_path)
    evidence["lockValidation"]["candidate"] = "missing"
    with pytest.raises(binding.BindingError, match="signatures"):
        binding.verify(prior, candidate, evidence)


def test_migration_journal_must_match_declared_set(tmp_path):
    prior, candidate, evidence = _case(tmp_path)
    evidence["schema"]["journalSha256"] = _digest("extra-migration")
    with pytest.raises(binding.BindingError, match="migration journal"):
        binding.verify(prior, candidate, evidence)

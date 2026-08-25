from __future__ import annotations

import generate_evidence_index as gei


def test_index_exposes_package_and_producer_provenance_without_components():
    manifest = {
        "platformRelease": "2026.1-rc.2",
        "components": {"server": {"sha": "a" * 40}},
        "clientArtifacts": {"wheel": {"digest": "sha256:" + "b" * 64}},
        "evidenceSources": {"cite": {"producerSha": "c" * 40}},
    }
    index = gei.build_index(manifest)
    assert index["schemaVersion"] == "honua.release-evidence-pins.v1"
    assert index["clientArtifacts"] == [{"name": "wheel", "digest": "sha256:" + "b" * 64}]
    assert index["evidenceSources"] == [{"name": "cite", "producerSha": "c" * 40}]
    assert "components" not in index


def test_index_is_deterministically_sorted():
    manifest = {
        "clientArtifacts": {"z": {}, "a": {}},
        "evidenceSources": {"y": {}, "b": {}},
    }
    index = gei.build_index(manifest)
    assert [row["name"] for row in index["clientArtifacts"]] == ["a", "z"]
    assert [row["name"] for row in index["evidenceSources"]] == ["b", "y"]

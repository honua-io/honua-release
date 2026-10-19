"""Selected public inputs cannot disclose oracle files or other cases."""
import copy
from pathlib import Path

import pytest

from agent_e2e_pack import (PackError, export_inputs, load_json, selected_files,
                            verify_corpus, verify_export_index, visible_task)


ROOT = Path(__file__).resolve().parents[1] / "certification/agent-e2e"


@pytest.fixture
def inputs():
    return load_json(ROOT / "pack.v2.json"), load_json(ROOT / "corpus/manifest.v1.json")


def test_export_excludes_oracles_sql_other_variants_and_unrelated_attacks(inputs, tmp_path):
    pack, manifest = inputs
    destination = tmp_path / "export"
    index = export_inputs(pack, "P10:A:local-docker", ROOT / "corpus", manifest, destination)
    assert index
    assert all(row["path"].startswith("town-A/") for row in index)
    assert not any("oracle" in row["path"] or row["path"].endswith(".sql") or row["path"].endswith("metadata.json") for row in index)
    verify_export_index(pack, "P10:A:local-docker", manifest, index)
    with pytest.raises(FileExistsError):
        export_inputs(pack, "P10:A:local-docker", ROOT / "corpus", manifest, destination)


def test_attack_is_selected_only_for_its_scenario(inputs):
    pack, manifest = inputs
    assert any(r["path"].endswith("metadata.json") for r in selected_files(pack, "P19:A:local-docker", manifest))
    assert not any(r["path"].endswith("metadata.json") for r in selected_files(pack, "P10:A:local-docker", manifest))


@pytest.mark.parametrize("path", ["oracles/town-A.json", "town-A/postgis.sql", "town-B/shelters.csv", "../secret"])
def test_unselected_file_rejected_even_with_claimed_hash(inputs, path):
    pack, manifest = inputs
    with pytest.raises(PackError):
        verify_export_index(pack, "P10:A:local-docker", manifest, [{"path": path, "sha256": "sha256:" + "a" * 64}])


def test_extra_nested_metadata_cannot_reach_model(inputs):
    pack, _ = inputs
    with pytest.raises(PackError):
        visible_task(pack, "P10:A:local-docker", {"files": [{"path": "file", "sha256": "sha256:" + "a"*64, "expected": "answer"}]})


def test_corpus_hash_checks_actual_bytes(inputs, tmp_path):
    _, manifest = inputs
    row = copy.deepcopy(manifest["files"][0])
    path = tmp_path / row["path"]
    path.parent.mkdir(parents=True)
    path.write_text("changed source")
    with pytest.raises(PackError, match="bytes changed"):
        verify_corpus(tmp_path, {"files": [row]})

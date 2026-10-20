"""Node prerequisite authority and actual finite-prefix mutation controls."""
import json
from pathlib import Path
import pytest

from python_source_node import IMAGE, census, prepare, require_node, verify

DATA=Path(__file__).parent/"python-source-inputs/selected.json"


def selected(): return json.loads(DATA.read_bytes())


def declaration(row):
    return {"document":row["document"],"blockIndex":row["index"],"blockSha256":row["sha256"]}


def test_only_exact_selected_development_reader_receives_node():
    selection=selected()
    assert [require_node(declaration(row),selection) for row in selection["documentation"]]==[False,True,False]


def test_changed_development_block_cannot_receive_declared_prerequisite():
    selection=selected();row=declaration(selection["documentation"][1]);row["blockSha256"]="0"*64
    with pytest.raises(ValueError): require_node(row,selection)


def test_changed_selection_cannot_move_node_to_another_context():
    selection=selected();selection["documentation"][1]["index"]=1
    with pytest.raises(ValueError): require_node(declaration(selection["documentation"][1]),selection)


def prefix(tmp_path):
    root=tmp_path/"prefix";(root/"bin").mkdir(parents=True);(root/"lib").mkdir()
    node=root/"bin/node";node.write_bytes(b"actual-test-node-bytes");node.chmod(0o755)
    (root/"lib/npm.js").write_bytes(b"test-npm-script")
    (root/"bin/npm").symlink_to("../lib/npm.js")
    return root,{"image":IMAGE,"files":census(root)}


def test_owned_prefix_records_bytes_modes_and_internal_symlinks(tmp_path):
    root,receipt=prefix(tmp_path);result=verify(root,receipt)
    assert result["image"]==IMAGE and len(result["nodeSha256"])==64
    assert receipt["files"]["bin/npm"]=={"kind":"symlink","target":"../lib/npm.js"}


@pytest.mark.parametrize("fault",["bytes","mode","extra-file","symlink-escape","foreign-image","missing-node","non-executable","extra-directory","directory-mode"])
def test_prefix_drift_or_foreign_authority_refuses(tmp_path,fault):
    root,receipt=prefix(tmp_path)
    if fault=="bytes": (root/"bin/node").write_bytes(b"changed")
    elif fault=="mode": (root/"lib/npm.js").chmod(0o755)
    elif fault=="extra-file": (root/"extra").write_bytes(b"unbound")
    elif fault=="symlink-escape": (root/"escape").symlink_to(tmp_path)
    elif fault=="foreign-image": receipt["image"]="node:latest"
    elif fault=="missing-node": (root/"bin/node").unlink()
    elif fault=="non-executable": (root/"bin/node").chmod(0o644)
    elif fault=="extra-directory": (root/"empty-unbound").mkdir()
    elif fault=="directory-mode": (root/"lib").chmod(0o700)
    with pytest.raises(ValueError): verify(root,receipt)


def test_prepare_refuses_changed_runtime_before_any_copy_or_container(tmp_path):
    source=tmp_path/"sources.json";source.write_text(json.dumps({"runtimes":{"node":"node:latest"}}))
    output=tmp_path/"node"
    with pytest.raises(ValueError): prepare(output,source)
    assert not output.exists()


def test_node_identity_is_existing_governed_runtime():
    source=Path(__file__).with_name("sources.json")
    assert json.loads(source.read_bytes())["runtimes"]["node"]==IMAGE

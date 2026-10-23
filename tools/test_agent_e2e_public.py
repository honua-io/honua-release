"""Actual selected bytes and public worker verification; no live qualification."""
import json
from pathlib import Path

import pytest

from agent_e2e_knowledge import binding
from agent_e2e_pack import PackError, load_json, selected_files
from agent_e2e_public import prepare, verify
from bundle_agent_e2e_container import bundle
from test_agent_e2e_knowledge import sample_manifest

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT/'certification/agent-e2e/pack.v2.json'


@pytest.fixture
def public_bundle(tmp_path):
    knowledge = tmp_path/'knowledge'
    manifest = sample_manifest(knowledge, load_json(PACK))
    image = tmp_path/'image'
    result = bundle(image, knowledge_root=knowledge, pack_path=PACK, case_id='P10:A:aws-ecs-redis-on')
    return image/'public', result, manifest


def test_immutable_image_only_contains_selected_public_inputs_and_knowledge(public_bundle):
    root, result, manifest = public_bundle
    observed = verify(root)
    pack = load_json(PACK)
    corpus = load_json(PACK.parent/pack['corpusManifest'])
    assert {row['path'] for row in observed['files']} == {
        row['path'] for row in selected_files(pack, 'P10:A:aws-ecs-redis-on', corpus)}
    assert all('town-B/' not in path and 'town-C/' not in path and 'oracles/' not in path for path in result['files'])
    assert observed['knowledge'] == manifest


def test_fixed_startup_verifies_and_materializes_once(public_bundle, tmp_path):
    root, _, manifest = public_bundle
    workspace = tmp_path/'workspace'
    workspace.mkdir()
    observed = prepare(root, workspace)
    assert observed['knowledgeBinding'] == binding(manifest)
    assert len(json.dumps(observed).encode()) < 16384  # Real terminal reply bound.
    for row in observed['files']:
        assert (workspace/row['path']).read_bytes() == (root/'inputs'/row['path']).read_bytes()
    with pytest.raises(PackError, match='already occurred'):
        prepare(root, workspace)


@pytest.mark.parametrize('change', ['altered', 'extra', 'symlink'])
def test_worker_rejects_changed_or_unreviewed_public_bytes(public_bundle, tmp_path, change):
    root, _, _ = public_bundle
    path = next((root/'inputs').rglob('*.geojson'))
    if change == 'altered':
        path.write_text('{}')
    elif change == 'extra':
        (root/'private-oracle.json').write_text('{}')
    else:
        outside = tmp_path/'outside'
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    with pytest.raises(PackError):
        verify(root)


def test_worker_cannot_write_selected_inputs_through_workspace_symlink(public_bundle, tmp_path):
    root, _, _ = public_bundle
    workspace, outside = tmp_path/'workspace', tmp_path/'outside'
    workspace.mkdir(); outside.mkdir()
    (workspace/'town-A').symlink_to(outside, target_is_directory=True)
    with pytest.raises(PackError, match='destination escapes'):
        prepare(root, workspace)
    assert not list(outside.iterdir())


def test_development_image_refuses_held_out_c_and_incomplete_bundle_request(tmp_path):
    knowledge = tmp_path/'knowledge'
    sample_manifest(knowledge, load_json(PACK))
    with pytest.raises(ValueError, match='held-out'):
        bundle(tmp_path/'C', knowledge_root=knowledge, pack_path=PACK, case_id='P10:C:aws-ecs-redis-on')
    with pytest.raises(ValueError, match='require knowledge'):
        bundle(tmp_path/'partial', knowledge_root=knowledge)

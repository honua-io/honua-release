"""Knowledge exports must bind bytes, public identities and the source revision."""
import copy
import json

import pytest

import agent_e2e_knowledge as knowledge
from agent_e2e_pack import PackError, canonical_digest, digest_bytes


def sample_manifest(root, pack=None):
    ids = sorted({ref for s in (pack or {}).get("scenarios", []) for field in ("conceptIds", "workflowIds") for ref in s.get(field, [])}) or ["concepts/domain/map"]
    files = {cid + ".md": ("# " + cid + "\n").encode() for cid in ids}
    graph = {"profile": "honua.okf-agent-graph/v1", "executionVerified": False,
             "bundleDigest": "sha256:" + "b" * 64, "nodes": [
                 {"id": cid, "path": cid + ".md", "documentDigest": digest_bytes(files[cid + '.md']),
                  "metadata": {"title": cid, "description": "Public concept"}} for cid in ids],
             "interfaceBindings": [], "edges": []}
    files["okf/domain-graph.v1.json"] = json.dumps(graph).encode()
    manifest = {"schemaVersion": 1, "sourceRepository": knowledge.REPOSITORY, "sourceRevision": "a"*40,
                "bundleDigest": graph["bundleDigest"], "graphDigest": digest_bytes(files["okf/domain-graph.v1.json"]),
                "normalization": "UTF-8/LF", "files": [
                    {"path": p, "sha256": digest_bytes(data), "bytes": len(data)} for p,data in sorted(files.items())]}
    root.mkdir(parents=True, exist_ok=True)
    for path,data in files.items():
        target=root/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (root/'knowledge-manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    return manifest


def test_changed_worker_document_or_extra_private_file_rejected(tmp_path):
    manifest=sample_manifest(tmp_path)
    expected=knowledge.binding(manifest)
    knowledge.verify(tmp_path,manifest,expected)
    path=tmp_path/'concepts/domain/map.md'
    original=path.read_bytes()
    path.write_text('changed',encoding='utf-8')
    with pytest.raises(PackError,match='bytes changed'):
        knowledge.verify(tmp_path,manifest,expected)
    path.write_bytes(original)
    (tmp_path/'oracle.json').write_text('{}')
    with pytest.raises(PackError,match='unreviewed files'):
        knowledge.verify(tmp_path,manifest,expected)


def test_changed_revision_and_missing_concept_rejected(tmp_path):
    manifest=sample_manifest(tmp_path)
    expected=knowledge.binding(manifest)
    changed=copy.deepcopy(manifest)
    changed['sourceRevision']='f'*40
    with pytest.raises(PackError,match='differs'):
        knowledge.verify(tmp_path,changed,expected)
    graph=knowledge.verify(tmp_path,manifest,expected)
    with pytest.raises(PackError,match='unresolved'):
        knowledge.references({'scenarios':[{'id':'P01','conceptIds':['missing'],'workflowIds':['missing']}]},graph)


def test_retrieval_links_pin_revision_and_do_not_authorize_execution(tmp_path):
    sample_manifest(tmp_path)
    result=knowledge.query(tmp_path,'map')
    assert result['executionAuthorized'] is False
    assert '/blob/'+'a'*40+'/' in result['nodes'][0]['humanDocument']

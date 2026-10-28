"""Knowledge exports must bind bytes, public identities and the source revision."""
import copy
import json
import subprocess

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


def source_fixture(tmp_path, monkeypatch):
    repository = tmp_path / 'source'
    (repository / 'docs').mkdir(parents=True)
    scripts = repository / 'scripts/ci'
    scripts.mkdir(parents=True)
    (scripts / 'generate-okf-graph.py').write_text('''import argparse, hashlib, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--output')
a = p.parse_args()
root = Path(__file__).resolve().parents[2]
doc = (root / 'docs/concept.md').read_bytes()
digest = lambda b: 'sha256:' + hashlib.sha256(b).hexdigest()
node = {'id': 'concept', 'path': 'concept.md', 'documentDigest': digest(doc)}
commitment = {'manifestDigest': digest((root / 'scripts/ci/okf-bundle.v1.json').read_bytes()),
              'documents': [{'id': 'concept', 'digest': node['documentDigest']}], 'interfaceBindings': []}
bundle = digest(json.dumps(commitment, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
Path(a.output).parent.mkdir(parents=True, exist_ok=True)
Path(a.output).write_text(json.dumps({'profile': 'honua.okf-agent-graph/v1', 'executionVerified': False,
                                      'bundleDigest': bundle, 'nodes': [node], 'interfaceBindings': []}) + '\\n')
''', encoding='utf-8')
    for name in ('okf_frontmatter.py', 'gis_workflow.py'):
        (scripts / name).write_text('# fixture module\n', encoding='utf-8')
    monkeypatch.setattr(knowledge, 'GENERATOR_DIGESTS', {
        'scripts/ci/' + path.name: digest_bytes(path.read_bytes()) for path in scripts.glob('*.py')})
    (scripts / 'okf-bundle.v1.json').write_text(json.dumps({
        'root': 'docs', 'spec': {'version': '0.2'}, 'excludedFiles': [], 'excludedDirs': []}),
        encoding='utf-8')
    (repository / 'docs/concept.md').write_text('---\ntype: concept\ntitle: Concept\n---\nPublic.\n', encoding='utf-8')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repository), *args], text=True).strip()
    subprocess.run(['git', '-C', str(repository), 'init', '-q'], check=True)
    git('config', 'user.name', 'Mike McDougall')
    git('config', 'user.email', 'mike@honua.io')
    git('add', '.')
    git('commit', '-qm', 'Add public knowledge source')
    revision = git('rev-parse', 'HEAD')
    return repository, scripts, git, revision


def test_untracked_graph_is_derived_from_pinned_reviewed_sources(tmp_path, monkeypatch):
    repository, scripts, git, revision = source_fixture(tmp_path, monkeypatch)
    first = knowledge.graph_bytes(repository, revision)
    graph = json.loads(first)
    assert graph['bundleDigest'].startswith('sha256:')
    manifest, files = knowledge.build(repository, revision, {'scenarios': []})
    assert files['okf/domain-graph.v1.json'] == first
    assert manifest['graphDigest'] == digest_bytes(first)

    (repository / 'docs/concept.md').write_text('---\ntype: concept\ntitle: Concept\n---\nChanged.\n', encoding='utf-8')
    git('add', '.')
    git('commit', '-qm', 'Change source')
    changed = git('rev-parse', 'HEAD')
    assert json.loads(knowledge.graph_bytes(repository, changed))['bundleDigest'] != graph['bundleDigest']
    assert knowledge.graph_bytes(repository, revision) == first

    with (scripts / 'generate-okf-graph.py').open('a', encoding='utf-8') as output:
        output.write('\n# unreviewed\n')
    git('add', '.')
    git('commit', '-qm', 'Change generator')
    with pytest.raises(PackError, match='unreviewed OKF graph generator'):
        knowledge.graph_bytes(repository, git('rev-parse', 'HEAD'))


def test_untrusted_bundle_root_cannot_redirect_generator(tmp_path, monkeypatch):
    repository, scripts, git, _ = source_fixture(tmp_path, monkeypatch)
    (scripts / 'okf-bundle.v1.json').write_text(json.dumps({
        'root': '/', 'spec': {'version': '0.2'}, 'excludedFiles': [], 'excludedDirs': []}),
        encoding='utf-8')
    git('add', '.')
    git('commit', '-qm', 'Redirect bundle root')
    with pytest.raises(PackError, match='bundle root must be docs'):
        knowledge.graph_bytes(repository, git('rev-parse', 'HEAD'))


def test_source_archive_is_bounded_before_materialization(tmp_path, monkeypatch):
    repository, _, _, revision = source_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(knowledge, 'MAX_SOURCE_ARCHIVE_BYTES', 128)
    with pytest.raises(PackError, match='archive exceeds bound'):
        knowledge.graph_bytes(repository, revision)


def test_legacy_committed_graph_remains_readable(tmp_path):
    repository = tmp_path / 'legacy'
    graph = repository / 'docs/okf/domain-graph.v1.json'
    graph.parent.mkdir(parents=True)
    graph.write_text('{"legacy":true}\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(repository), 'init', '-q'], check=True)
    subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Mike McDougall',
                    '-c', 'user.email=mike@honua.io', 'commit', '-qm', 'Legacy graph'], check=True)
    revision = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip()
    assert knowledge.graph_bytes(repository, revision) == graph.read_bytes()

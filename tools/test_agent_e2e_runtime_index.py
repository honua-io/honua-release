"""Finite source compatibility; never published-image/installation qualification."""
import copy
import pytest

import bundle_agent_e2e_container as module


def runtime_proof_fixture(image):
    """Synthetic verifier output; never an actual published-image receipt."""
    from agent_e2e_pack import canonical_digest
    index = module.runtime_index()
    def ref(key):
        return {'bucket': 'fixture-retained', 'key': key, 'versionId': 'fixture-version', 'sha256': 'sha256:'+'a'*64}
    return {'image': image, 'publication': ref('original-publication'), 'indexReference': ref('original-index'),
        'index': index, 'runtimeBundleDigest': canonical_digest(index)}


def test_original_export_index_contains_the_exact_finite_reviewed_runtime(tmp_path):
    expected = module.runtime_index()
    actual = module.bundle(tmp_path/'actual-export')
    assert actual == expected
    assert set(expected['files']) == {'agent_e2e_errors.py', 'agent_e2e_worker.py', 'agent_e2e_container.py',
        'agent_e2e_container_entry.py', 'agent_e2e_mcp.py', 'agent_e2e_mcp_runner.py', 'agent_e2e_mcp_entry.py',
        'agent_e2e_installed_container.py', 'agent_e2e_execution_protocol.py', 'agent_e2e_public.py',
        'agent_e2e_public_entry.py', 'Dockerfile', 'requirements.txt'}
    assert module.verify_runtime_index(actual) == expected
    actual['files']['public/knowledge/original-doc.md'] = 'sha256:'+'a'*64
    assert module.verify_runtime_index(actual) == expected


@pytest.mark.parametrize('change', ['old-runner', 'missing-file', 'additional-private', 'path-traversal', 'absolute-path',
    'empty-segment', 'backslash', 'unhashed', 'boolean-schema', 'unknown-envelope'])
def test_missing_or_older_runtime_bytes_are_not_admitted_by_a_schema_label(change):
    index = copy.deepcopy(module.runtime_index())
    if change == 'old-runner': index['files']['agent_e2e_mcp_runner.py'] = 'sha256:'+'b'*64
    if change == 'missing-file': del index['files']['agent_e2e_installed_container.py']
    if change == 'additional-private': index['files']['controller.py'] = 'sha256:'+'c'*64
    if change == 'path-traversal': index['files']['public/../controller.py'] = 'sha256:'+'c'*64
    if change == 'absolute-path': index['files']['/public/source'] = 'sha256:'+'c'*64
    if change == 'empty-segment': index['files']['public//source'] = 'sha256:'+'c'*64
    if change == 'backslash': index['files']['public\\source'] = 'sha256:'+'c'*64
    if change == 'unhashed': index['files']['Dockerfile'] = 'unverified'
    if change == 'boolean-schema': index['schemaVersion'] = True
    if change == 'unknown-envelope': index['capabilityClaim'] = 'compatible'
    with pytest.raises(ValueError): module.verify_runtime_index(index)


@pytest.mark.parametrize('change', ['missing', 'old-runtime', 'wrong-image', 'changed-index-digest', 'unversioned', 'extra-claim'])
def test_unverified_or_incompatible_publication_cannot_reach_model_constructor(change):
    from agent_e2e_runtime_configuration import validate_worker_runtime
    from agent_e2e_pack import PackError, canonical_digest
    worker = {'descriptor': {'image': 'fixture-image@sha256:'+'a'*64}, 'proof': {}}
    worker['proof']['runtime'] = runtime_proof_fixture(worker['descriptor']['image'])
    validate_worker_runtime(worker)
    proof = worker['proof']['runtime']
    if change == 'missing': del worker['proof']['runtime']
    if change == 'old-runtime':
        proof['index']['files']['agent_e2e_mcp_runner.py'] = 'sha256:'+'b'*64
        proof['runtimeBundleDigest'] = canonical_digest(proof['index'])
    if change == 'wrong-image': proof['image'] += '-different'
    if change == 'changed-index-digest': proof['runtimeBundleDigest'] = 'sha256:'+'b'*64
    if change == 'unversioned': proof['indexReference']['versionId'] = 'null'
    if change == 'extra-claim': proof['compatible'] = True
    with pytest.raises((PackError, ValueError)): validate_worker_runtime(worker)

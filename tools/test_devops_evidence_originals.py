"""Real owned-file controls; no native/cloud invocation or fixture qualification."""
from pathlib import Path
import os
import pytest
from devops_provisioning_contract import Refusal,sha256,document
from devops_evidence_originals import read_reference


def original(tmp_path):
    root=tmp_path/'owned-protected-evidence';root.mkdir(mode=0o700)
    data=b'original private plan/approval vector';digest=sha256(data)
    path=root/digest;path.write_bytes(data);path.chmod(0o600)
    reference={'kind':'plan','reference':'urn:sha256:'+digest,'sha256':digest,'byteLength':len(data)}
    return root,path,data,reference


def test_production_content_address_reference_resolves_exact_private_bytes(tmp_path):
    root,path,data,reference=original(tmp_path)
    assert read_reference(root,reference,expected_kind='plan')==data


@pytest.mark.parametrize('expected_kind', ['plan', 'destroy-recovery'])
@pytest.mark.parametrize('mutation',['foreign-kind','url','path-traversal','size','bool-size','changed-bytes',
                                    'symlink','hardlink','public-mode','root-mode','root-link'])
def test_reference_cannot_read_unbound_or_changed_original(tmp_path,mutation,expected_kind):
    root,path,data,reference=original(tmp_path)
    reference['kind']=expected_kind
    if mutation=='foreign-kind':reference['kind']='approval'
    if mutation=='url':reference['reference']='https://foreign.invalid/original'
    if mutation=='path-traversal':reference['sha256']='../foreign'
    if mutation=='size':reference['byteLength']+=1
    if mutation=='bool-size':reference['byteLength']=True
    if mutation=='changed-bytes':path.write_bytes(b'x'*len(data))
    if mutation=='symlink':
        other=tmp_path/'foreign';other.write_bytes(data);other.chmod(0o600);path.unlink();path.symlink_to(other)
    if mutation=='hardlink':os.link(path,tmp_path/'second-link')
    if mutation=='public-mode':path.chmod(0o644)
    if mutation=='root-mode':root.chmod(0o755)
    if mutation=='root-link':
        link=tmp_path/'root-alias';link.symlink_to(root,target_is_directory=True);root=link
    with pytest.raises(Refusal):read_reference(root,reference,expected_kind=expected_kind)


@pytest.mark.parametrize('missing_receipt', [False, True])
def test_published_recovery_reference_reaches_the_complete_original_consumer(tmp_path, missing_receipt):
    # Same production-shaped recovery/origin/approval chain as the semantic
    # source controls. This specifically exercises the real protected-file
    # boundary before the full consumer, including an indeterminate parent.
    from test_judge_devops_cleanup import recovery_vector
    from judge_devops_cleanup import verify_destroy_recovery

    originals, context, kwargs = recovery_vector(missing_receipt)
    reference = document(originals['successor-obligation'])['Recovery']
    root = tmp_path / 'private-published-recovery'
    root.mkdir(mode=0o700)
    path = root / reference['sha256']
    path.write_bytes(originals['recovery-context'])
    path.chmod(0o600)
    originals['recovery-context'] = read_reference(root, reference, expected_kind='destroy-recovery')
    parent_bytes = dict(originals, **{
        'recovery-parent-obligation': originals['parent-obligation'],
        'recovery-parent-claim': kwargs['previous_originals']['claim'],
    })
    if not missing_receipt:
        parent_bytes['recovery-parent-execution'] = kwargs['previous_originals']['execution-receipt']
    resolved = {}
    for parent_reference in document(originals['recovery-context'])['ParentOriginals']:
        kind = parent_reference['kind']
        path = root / parent_reference['sha256']
        path.write_bytes(parent_bytes[kind])
        path.chmod(0o600)
        resolved[kind] = read_reference(root, parent_reference, expected_kind=kind)
    originals['parent-obligation'] = resolved['recovery-parent-obligation']
    kwargs['previous_originals']['claim'] = resolved['recovery-parent-claim']
    if not missing_receipt:
        kwargs['previous_originals']['execution-receipt'] = resolved['recovery-parent-execution']
    result = verify_destroy_recovery(originals, context, **kwargs)
    assert result['status'] == 'signed-destroy-recovery-originals-joined'
    assert result['stateChain']['prior']['status'] == ('indeterminate' if missing_receipt else 'execution-joined')
    assert result['candidateQualified'] is result['sixAwsCellsQualified'] is False


def test_unregistered_recovery_reference_kind_remains_refused(tmp_path):
    root, path, data, reference = original(tmp_path)
    reference['kind'] = 'destroy-recovery-foreign'
    with pytest.raises(Refusal, match='evidence-reference-kind'):
        read_reference(root, reference, expected_kind='destroy-recovery-foreign')

"""Real owned-file controls; no native/cloud invocation or fixture qualification."""
from pathlib import Path
import os
import pytest
from devops_provisioning_contract import Refusal,sha256
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


@pytest.mark.parametrize('mutation',['foreign-kind','url','path-traversal','size','bool-size','changed-bytes',
                                    'symlink','hardlink','public-mode','root-mode','root-link'])
def test_reference_cannot_read_unbound_or_changed_original(tmp_path,mutation):
    root,path,data,reference=original(tmp_path)
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
    with pytest.raises(Refusal):read_reference(root,reference,expected_kind='plan')

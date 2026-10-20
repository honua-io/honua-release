"""Resolve production EvidenceRefs only inside an owned private native evidence root.

Production ProvisioningEvidenceStore explicitly publishes immutable references for
release consumers. No URL or receipt-selected filesystem path is ever followed.
Returned original bytes stay private; callers publish only safe digests/identity.
"""
from __future__ import annotations
import os
from pathlib import Path
import re
import stat
from devops_provisioning_contract import Refusal,require,sha256

ALLOWED_KINDS=frozenset({'plan','plan-metadata','approval','apply','handoff','verification-evidence',
    'verification-receipt','provision-binding','runtime-observation','cleanup-execution','cleanup-claim',
    'cleanup-plan-schema','cleanup-exec-schema','destroy-recovery'})
MAX_ORIGINAL=512*1024*1024


def read_reference(evidence_root: Path, reference: dict, *, expected_kind: str) -> bytes:
    """Caller supplies the independently owned protected runtime root, not receipt data."""
    require(type(reference) is dict and set(reference)=={'kind','reference','sha256','byteLength'},
            'evidence-reference-shape')
    require(expected_kind in ALLOWED_KINDS and reference['kind']==expected_kind,'evidence-reference-kind')
    digest=reference['sha256'];length=reference['byteLength']
    require(type(digest) is str and re.fullmatch(r'[0-9a-f]{64}',digest)
            and reference['reference']=='urn:sha256:'+digest,'evidence-reference-identity')
    require(type(length) is int and 0<=length<=MAX_ORIGINAL,'evidence-original-bound')
    require(type(evidence_root) is Path or isinstance(evidence_root,Path),'evidence-root-type')
    root=Path(evidence_root)
    require(root.is_absolute(),'evidence-root-absolute')
    # Root identity is supplied by the trusted launcher. Refuse symlink traversal
    # instead of resolving a receipt-controlled alias into another runtime.
    for parent in (root,*root.parents):
        require(not parent.is_symlink(),'evidence-root-link')
    try:
        root_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            before=os.fstat(root_fd)
            require(stat.S_ISDIR(before.st_mode) and before.st_uid==os.getuid()
                    and stat.S_IMODE(before.st_mode)==0o700,'evidence-root-ownership')
            fd=os.open(digest,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=root_fd)
            try:
                initial=os.fstat(fd)
                require(stat.S_ISREG(initial.st_mode) and initial.st_uid==os.getuid()
                        and stat.S_IMODE(initial.st_mode)==0o600 and initial.st_nlink==1
                        and initial.st_size==length,'evidence-original-ownership')
                blocks=[];total=0
                while block:=os.read(fd,min(64*1024,length-total+1)):
                    total+=len(block);require(total<=length,'evidence-original-growth');blocks.append(block)
                after=os.fstat(fd)
                require((initial.st_dev,initial.st_ino,initial.st_size,initial.st_mtime_ns,initial.st_ctime_ns)
                        ==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns),
                        'evidence-original-changed')
                data=b''.join(blocks)
                require(len(data)==length and sha256(data)==digest,'evidence-original-digest')
                return data
            finally:os.close(fd)
        finally:os.close(root_fd)
    except OSError:
        raise Refusal('evidence-original-unavailable') from None

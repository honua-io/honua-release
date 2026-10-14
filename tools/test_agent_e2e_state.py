import copy
import io
import json

import pytest

from agent_e2e_pack import PackError, digest_bytes
from agent_e2e_state import StateStore, preserve_history


class S3:
    def __init__(self):
        self.objects = {}
    def get_bucket_versioning(self, **kwargs):
        return {'Status':'Enabled'}
    def put_object(self, **kwargs):
        assert kwargs['IfNoneMatch']=='*'
        self.objects[kwargs['Key']]=kwargs['Body']
        return {'VersionId':'version-one'}
    def get_object(self, **kwargs):
        assert kwargs['VersionId']=='version-one'
        return {'Body':io.BytesIO(self.objects[kwargs['Key']])}


class LostLease:
    def update_item(self, **kwargs):
        assert 'fence = :fence' in kwargs['ConditionExpression']
        assert 'leaseUntil >= :now' in kwargs['ConditionExpression']
        raise RuntimeError('ConditionalCheckFailedException')


def test_expired_writer_cannot_advance_state_head():
    objects=S3()
    store=StateStore(s3=objects,dynamodb=LostLease(),bucket='bucket',table='table',candidate_key='sha256:'+'a'*64)
    store.version=1; store.fence=2
    with pytest.raises(RuntimeError,match='ConditionalCheckFailed'):
        store.commit({'ledger':{'entries':[]}})
    assert store.version==1
    assert len(objects.objects)==1  # Unreferenced snapshot retained, never a new head.


def test_unversioned_bucket_blocks_before_lease_acquisition():
    objects=S3()
    objects.get_bucket_versioning=lambda **kwargs:{}
    store=StateStore(s3=objects,dynamodb=LostLease(),bucket='bucket',table='table',candidate_key='sha256:'+'a'*64)
    with pytest.raises(PackError,match='versioned'):
        store.acquire()


def test_history_cannot_hide_failure_change_mode_or_erase_charge():
    initial={'ledger':{'entries':[{'id':'r1','state':'reserved','actualUsd':None,'amountUsd':'1','campaignMode':'development'}]},
             'registry':{'attempts':[{'caseId':'P10:A:local-docker','status':'fail','campaignMode':'development'}]}}
    settled=copy.deepcopy(initial)
    settled['ledger']['entries'][0].update(state='settled',actualUsd='0.1')
    preserve_history(initial,settled)
    for key in ('ledger','registry'):
        changed=copy.deepcopy(settled)
        changed.pop(key)
        with pytest.raises(PackError,match='erase'):
            preserve_history(settled,changed)
    changed=copy.deepcopy(settled)
    changed['registry']['attempts'][0]['campaignMode']='qualification'
    with pytest.raises(PackError,match='cannot change'):
        preserve_history(settled,changed)
    changed=copy.deepcopy(settled)
    changed['ledger']['entries'][0]['actualUsd']='0'
    with pytest.raises(PackError,match='cannot change'):
        preserve_history(settled,changed)


def test_candidate_ceiling_cannot_be_reset_in_snapshot():
    previous = {'ledger':{'candidateKey':'sha256:'+'a'*64, 'ceilingUsd':'500', 'entries':[]}}
    changed = copy.deepcopy(previous)
    changed['ledger']['ceilingUsd'] = '1000'
    with pytest.raises(PackError, match='budget policy'):
        preserve_history(previous, changed)

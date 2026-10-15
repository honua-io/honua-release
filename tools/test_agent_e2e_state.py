import copy
import io
import json

import pytest

from agent_e2e_pack import PackError, digest_bytes
from agent_e2e_state import StateStore, DurableExecution, preserve_history, main, open_store
from agent_e2e_budget import update
from agent_e2e_telemetry import Journal, TelemetryError, read_events, report


class MemoryStore:
    candidate = 'sha256:'+'a'*64
    def __init__(self, state=None):
        self.state = copy.deepcopy(state)
        self.releases = 0
    def acquire(self):
        return copy.deepcopy(self.state)
    def commit(self, payload):
        preserve_history(self.state, payload)
        self.state = copy.deepcopy(payload)
    def release(self):
        self.releases += 1
    def renew(self):
        pass


def interrupted_execution(tmp_path):
    store = MemoryStore()
    ledger = tmp_path/'ledger.json'
    execution = DurableExecution(store, ledger, initialize=True)
    execution.update(ledger, 'reserve', candidate_key=store.candidate, reservation_id='r1',
                     campaign_id='c1', case_id='P10:A:local-docker', attempt=1,
                     kind='model', amount='1', basis_digest='sha256:'+'b'*64)
    log = Journal(tmp_path/'old-journals', {'campaignId':'c1', 'caseId':'P10:A:local-docker', 'attempt':1}, sink=execution.event)
    log.emit('start', modelId='requested', returnedModelId='returned')
    log.close()
    execution.close()
    return store, ledger, log


def test_restart_restores_interrupted_attempt_and_retains_liability(tmp_path):
    store, ledger, old_log = interrupted_execution(tmp_path)
    recovered_ledger = tmp_path/'fresh-controller/ledger.json'
    execution = DurableExecution(store, recovered_ledger)
    try:
        root = tmp_path/'recovered-journals'
        assert execution.restore_journals(root) == 1
        assert execution.restore_journals(root) == 0
        assert read_events(root/old_log.path.name) == read_events(old_log.path)
        assert report(list(root.glob('*.jsonl')))['attempts'][0]['status'] == 'interrupted'
        assert json.loads(recovered_ledger.read_text())['entries'][0]['state'] == 'reserved'
        assert execution.update(recovered_ledger, 'check', candidate_key=store.candidate)['liabilityUsd'] == '1'
        with pytest.raises(FileExistsError):
            Journal(root, {'campaignId':'c1', 'caseId':'P10:A:local-docker', 'attempt':1})
    finally:
        execution.close()


def test_restart_cannot_erase_newer_local_charge(tmp_path):
    store, ledger, _ = interrupted_execution(tmp_path)
    update(ledger, 'settle', candidate_key=store.candidate, reservation_id='r1', amount='0.8')
    charged = ledger.read_bytes()
    before = store.releases
    with pytest.raises(PackError, match='charge cannot change'):
        DurableExecution(store, ledger)
    assert ledger.read_bytes() == charged
    assert store.releases == before+1
    assert store.state['ledger']['entries'][0]['state'] == 'reserved'


def test_recovery_retains_divergent_local_journal(tmp_path):
    store, ledger, old_log = interrupted_execution(tmp_path)
    rows = read_events(old_log.path)
    rows[0]['modelId'] = 'different-local-observation'
    old_log.path.write_text(json.dumps(rows[0])+'\n')
    original = old_log.path.read_bytes()
    execution = DurableExecution(store, ledger)
    try:
        with pytest.raises(PackError, match='reconciliation'):
            execution.restore_journals(old_log.path.parent)
        assert old_log.path.read_bytes() == original
    finally:
        execution.close()


def test_recovery_rejects_changed_journal_identity_before_writing(tmp_path):
    store, ledger, _ = interrupted_execution(tmp_path)
    rows = next(iter(store.state['journals'].values()))
    store.state['journals'] = {'sha256:'+'f'*64:rows}
    execution = DurableExecution(store, ledger)
    root = tmp_path/'recovery'
    try:
        with pytest.raises(PackError, match='identity mismatch'):
            execution.restore_journals(root)
        assert not root.exists()
    finally:
        execution.close()


def test_recovery_command_restores_existing_history_without_new_attempt(tmp_path, monkeypatch):
    store, _, _ = interrupted_execution(tmp_path)
    before = copy.deepcopy(store.state)
    monkeypatch.setattr('agent_e2e_state.open_store', lambda config, candidate:store)
    config = tmp_path/'config.json'
    config.write_text('{}')
    output = tmp_path/'recovery.json'
    assert main(['--state-config', str(config), '--candidate-key', store.candidate,
                 '--ledger', str(tmp_path/'fresh/ledger.json'), '--journal-root', str(tmp_path/'fresh/journals'),
                 '--output', str(output)]) == 0
    result = json.loads(output.read_text())
    assert result['journalsRestored'] == result['journalsRetained'] == 1
    assert result['qualification'] is False
    assert store.state == before


def test_recovery_command_cannot_initialize_missing_history(tmp_path, monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr('agent_e2e_state.open_store', lambda config, candidate:store)
    config = tmp_path/'config.json'
    config.write_text('{}')
    output = tmp_path/'recovery.json'
    ledger = tmp_path/'new/ledger.json'
    assert main(['--state-config', str(config), '--candidate-key', store.candidate,
                 '--ledger', str(ledger), '--journal-root', str(tmp_path/'new/journals'),
                 '--output', str(output)]) == 1
    assert json.loads(output.read_text())['status'] == 'blocked'
    assert not ledger.exists() and store.state is None


def test_state_configuration_cannot_carry_inline_credentials():
    with pytest.raises(PackError, match='configuration'):
        open_store({'region':'test', 'bucket':'test', 'table':'test', 'secret':'private'}, MemoryStore.candidate)


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


@pytest.mark.parametrize('history', [
    {'Versions':[{'Key':'retained', 'VersionId':'previous'}]},
    {'DeleteMarkers':[{'Key':'hidden', 'VersionId':'deleted'}]},
    {'IsTruncated':True},
])
def test_missing_state_head_cannot_reset_existing_snapshot_history(history):
    class Headless:
        def get_item(self, **kwargs):
            assert kwargs['ConsistentRead'] is True
            return {'Item':{'leaseOwner':{'S':'owner'}, 'fence':{'N':'1'}}}
    objects = S3()
    def versions(**kwargs):
        assert kwargs == {'Bucket':'bucket', 'Prefix':'agent-e2e/'+'a'*64+'/', 'MaxKeys':1}
        return history
    objects.list_object_versions = versions
    store = StateStore(s3=objects, dynamodb=Headless(), bucket='bucket', table='table',
                       candidate_key='sha256:'+'a'*64, owner='owner')
    store.fence = 1
    with pytest.raises(PackError, match='reconcile'):
        store.load()
    assert objects.objects == {}


def test_new_candidate_requires_successful_history_absence_probe():
    class Headless:
        def get_item(self, **kwargs):
            return {'Item':{'leaseOwner':{'S':'owner'}, 'fence':{'N':'1'}}}
    objects = S3()
    store = StateStore(s3=objects, dynamodb=Headless(), bucket='bucket', table='table',
                       candidate_key='sha256:'+'a'*64, owner='owner')
    store.fence = 1
    def denied(**kwargs):
        raise PermissionError('history unavailable')
    objects.list_object_versions = denied
    with pytest.raises(PermissionError):
        store.load()
    objects.list_object_versions = lambda **kwargs: {'IsTruncated':False}
    assert store.load() is None


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

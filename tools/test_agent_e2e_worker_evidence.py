"""Storage/transport failure tests. No cloud provisioning or qualifying receipts."""
import base64
import copy
import hashlib
import io
import json

import pytest

from agent_e2e_worker_evidence import ReplyEvidence, PackError
from agent_e2e_worker import Worker, ReplyChunks, reply_messages, encode
from agent_e2e_container import Executor
from test_agent_e2e_mcp_worker import request, reply, Exchange
from test_agent_e2e_worker import CONFIG


CANDIDATE = 'sha256:'+'a'*64


class Conflict(Exception):
    response = {'Error':{'Code':'PreconditionFailed'}}


class S3:
    def __init__(self):
        self.objects, self.puts, self.reads = {}, [], []
        self.status = 'Enabled'
        self.mutate_ack = lambda response: response
        self.after_write = lambda key: None
        self.mutate_history = lambda result: result
    def get_bucket_versioning(self, **kwargs):
        return {'Status':self.status}
    def put_object(self, **kwargs):
        assert kwargs['IfNoneMatch'] == '*'
        assert kwargs['ChecksumSHA256'] == base64.b64encode(hashlib.sha256(kwargs['Body']).digest()).decode()
        self.puts.append(copy.deepcopy(kwargs))
        key = kwargs['Key']
        if key in self.objects:
            raise Conflict()
        self.objects[key] = [('version-1', kwargs['Body'])]
        self.after_write(key)
        return self.mutate_ack({'VersionId':'version-1','ChecksumSHA256':kwargs['ChecksumSHA256']})
    def list_object_versions(self, **kwargs):
        rows = [{'Key':key,'VersionId':version,'IsLatest':index == len(versions)-1}
                for key, versions in self.objects.items() if key.startswith(kwargs['Prefix'])
                for index, (version, _) in enumerate(versions)]
        return self.mutate_history({'Versions':rows[:kwargs['MaxKeys']], 'IsTruncated':len(rows)>kwargs['MaxKeys']})
    def get_object(self, **kwargs):
        self.reads.append(copy.deepcopy(kwargs))
        for version, raw in self.objects[kwargs['Key']]:
            if version == kwargs['VersionId']:
                return {'VersionId':version,'Body':io.BytesIO(raw)}
        raise KeyError('missing version')


def evidence(s3):
    return ReplyEvidence(s3, 'controller-evidence', CANDIDATE, 'attempt-1', 'action-1')


@pytest.mark.parametrize('known_reference', [False, True])
def test_restart_recovers_exact_reply_and_version_references_without_raw_commands(known_reference):
    s3 = S3()
    sink = evidence(s3)
    command = {**request(), 'action':{'name':'mcp_call_tool','input':{'name':'configure',
                                  'arguments':{'token':'command-token-must-not-be-retained'}}}}
    expected = reply(command)
    parts = reply_messages(expected, command)
    for part in reversed(parts):
        sink(part)
    assert len(s3.objects) == len(parts)+1
    assert all(key.startswith('evidence/'+CANDIDATE[7:]+'/worker/attempt-1/action-1/') for key in s3.objects)
    assert not any(b'command-token-must-not-be-retained' in versions[0][1] for versions in s3.objects.values())
    recovered = evidence(s3).recover(sink.header_reference if known_reference else None)
    assert recovered['reply'] == expected and recovered['qualification'] is False
    assert recovered['header'] == sink.header_reference
    assert recovered['chunks'] == [sink.references[index] for index in range(len(parts))]
    assert all('VersionId' in value for value in s3.reads)


def test_worker_acknowledges_only_after_actual_retention_callback(tmp_path):
    s3 = S3()
    sink = evidence(s3)
    executor = Executor(CONFIG, tmp_path/'worker.sqlite', tmp_path, clock=lambda:1000,
                        execute_mcp=lambda *_: {'status':'completed','payload':{'data':'x'*100000}})
    class DurableQueue(Exchange):
        def delete_message(self, **kwargs):
            index = int(kwargs['ReceiptHandle'])
            assert sink.references[index]['key'] in s3.objects
            assert sink.header_reference['key'] in s3.objects
            super().delete_message(**kwargs)
    queue = DurableQueue(executor)
    observed = Worker(queue, CONFIG, clock=lambda:1000).execute_mcp(request()['action'], 'action-1', retain_chunk=sink)
    assert len(queue.deleted) > 1
    assert evidence(s3).recover()['reply'] == observed


def test_duplicate_retention_checks_existing_version_without_overwrite():
    s3 = S3()
    part = reply_messages(reply(size=1), request())[0]
    first = evidence(s3)(part)
    assert evidence(s3)(part) == first
    assert all(len(versions) == 1 for versions in s3.objects.values())


def test_lost_write_acknowledgement_retains_evidence_without_queue_ack(tmp_path):
    s3 = S3()
    sink = evidence(s3)
    def lose_ack(key):
        if 'chunk-' in key:
            raise TimeoutError('write response lost')
    s3.after_write = lose_ack
    executor = Executor(CONFIG, tmp_path/'worker.sqlite', tmp_path, clock=lambda:1000,
                        execute_mcp=lambda *_: {'status':'completed','payload':{'observed':'write-result'}})
    queue = Exchange(executor)
    worker = Worker(queue, CONFIG, clock=lambda:1000)
    with pytest.raises(TimeoutError):
        worker.execute_mcp(request()['action'], 'action-1', retain_chunk=sink)
    assert queue.deleted == [] and len(queue.requests) == 1
    assert evidence(s3).recover()['reply']['payload'] == {'observed':'write-result'}
    with pytest.raises(PackError, match='already dispatched'):
        worker.execute_mcp(request()['action'], 'action-1', retain_chunk=sink)


@pytest.mark.parametrize('failure', ['missing-version','null-version','wrong-checksum'])
def test_incomplete_write_acknowledgement_never_counts_as_retention(failure):
    s3 = S3()
    def mutate(value):
        if failure == 'missing-version':
            value.pop('VersionId')
        elif failure == 'null-version':
            value['VersionId'] = 'null'
        else:
            value['ChecksumSHA256'] = 'different'
        return value
    s3.mutate_ack = mutate
    with pytest.raises(PackError, match='acknowledgement'):
        evidence(s3)(reply_messages(reply(size=1), request())[0])
    assert len(s3.puts) == 1  # Never try a second write after ambiguous acknowledgement.


@pytest.mark.parametrize('history', ['missing','multiple','delete-marker','truncated'])
def test_unknown_history_never_grants_new_dispatch_or_rewrites(history):
    s3 = S3()
    sink = evidence(s3)
    sink(reply_messages(reply(size=1), request())[0])
    key = sink.prefix+'header.json'
    if history == 'missing':
        s3.objects.pop(key)
    elif history == 'multiple':
        s3.objects[key].append(('version-2', s3.objects[key][0][1]))
    elif history == 'delete-marker':
        s3.mutate_history = lambda value: {**value,'DeleteMarkers':[{'Key':key}]}
    else:
        s3.mutate_history = lambda value: {**value,'IsTruncated':True}
    count = len(s3.puts)
    with pytest.raises(PackError, match='history'):
        evidence(s3).recover()
    assert len(s3.puts) == count


def test_partial_or_tampered_reply_cannot_recover_as_complete():
    s3 = S3()
    sink = evidence(s3)
    parts = reply_messages(reply(), request())
    sink(parts[0])
    with pytest.raises(PackError, match='history'):
        evidence(s3).recover()
    for part in parts[1:]:
        sink(part)
    key = sink.prefix+'chunk-001.json'
    version, raw = s3.objects[key][0]
    value = json.loads(raw)
    value['data'] = value['data'].replace('e','f')
    s3.objects[key] = [(version,encode(value).encode())]
    with pytest.raises(PackError, match='digest mismatch'):
        evidence(s3).recover()


def test_alternate_payload_cannot_replace_same_action_evidence():
    s3 = S3()
    evidence(s3)(reply_messages(reply(size=1), request())[0])
    before = copy.deepcopy(s3.objects)
    with pytest.raises(PackError, match='replace retained'):
        evidence(s3)(reply_messages(reply(size=2), request())[0])
    assert s3.objects == before


def test_malformed_or_foreign_chunk_is_rejected_before_any_write():
    s3 = S3()
    part = reply_messages(reply(), request())[0]
    with pytest.raises(PackError):
        evidence(s3)({**part,'attemptId':'foreign-attempt'})
    assert s3.puts == []
    with pytest.raises(PackError):
        evidence(s3)({**part,'count':1000000})
    assert s3.puts == []


def test_recovery_refuses_changed_header_hash_or_foreign_key():
    s3 = S3()
    sink = evidence(s3)
    sink(reply_messages(reply(size=1), request())[0])
    with pytest.raises(PackError):
        evidence(s3).recover({**sink.header_reference,'sha256':'sha256:'+'f'*64})
    with pytest.raises(PackError):
        evidence(s3).recover({**sink.header_reference,'key':'another-candidate/header.json'})


def test_suspended_versioning_and_unsafe_identity_are_rejected():
    s3 = S3()
    s3.status = 'Suspended'
    with pytest.raises(PackError, match='versioned'):
        evidence(s3)
    with pytest.raises(PackError, match='identity'):
        ReplyEvidence(s3, 'bucket', CANDIDATE, 'attempt/../other', 'action')

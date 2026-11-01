import pytest

from agent_e2e_local_recorder import ProjectionError
from agent_studio_session_audit import controller_actors, snapshot, verify


class Recorder:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.requests = []

    def json(self, step, method, path):
        self.requests.append((step, method, path))
        return {'observation': {'httpStatus': 200, 'body': next(self.pages)}}


def test_audit_snapshots_page_and_reject_controller_activity():
    before = snapshot(Recorder([{'items': [{'auditId': 1, 'actor': 'admin'}], 'nextCursor': None}]), 'before')
    recorder = Recorder([
        {'items': [{'auditId': 3, 'actor': 'admin'}], 'nextCursor': 'next+page'},
        {'items': [{'auditId': 2, 'actor': 'customer'}, {'auditId': 1, 'actor': 'admin'}], 'nextCursor': None},
    ])
    after = snapshot(recorder, 'after')
    assert recorder.requests[1][2].endswith('cursor=next%2Bpage')
    with pytest.raises(ProjectionError, match='controller principal appeared'):
        verify(before, after, frozenset({'admin'}))
    after[3] = 'customer'
    verify(before, after, frozenset({'admin'}))


def test_audit_snapshots_reject_incomplete_or_mutated_evidence():
    with pytest.raises(ProjectionError, match='baseline'):
        verify({1: 'admin'}, {}, frozenset({'admin'}))
    with pytest.raises(ProjectionError, match='baseline'):
        verify({1: 'admin'}, {1: 'customer'}, frozenset({'admin'}))
    with pytest.raises(ProjectionError, match='cursor'):
        snapshot(Recorder([{'items': [], 'nextCursor': 'x'}, {'items': [], 'nextCursor': 'x'}]), 'after')
    with pytest.raises(ProjectionError, match='unavailable'):
        snapshot(Recorder([{'items': []}]), 'after')


def test_managed_controller_identity_is_matched_from_server_inventory():
    key_id = '12345678-1234-1234-1234-123456789abc'
    recorder = Recorder([{'data': [{'id': key_id, 'keyPrefix': 'hnua_example'}]}])
    actors = controller_actors(recorder, 'hnua_example-secret')
    with pytest.raises(ProjectionError, match='controller principal appeared'):
        verify({}, {1: 'apikey:api-key:' + key_id}, actors)

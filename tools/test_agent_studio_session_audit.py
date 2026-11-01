import pytest

from agent_e2e_local_recorder import ProjectionError
from agent_studio_session_audit import snapshot, verify


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
        verify(before, after)
    after[3] = 'customer'
    verify(before, after)


def test_audit_snapshots_reject_incomplete_or_mutated_evidence():
    with pytest.raises(ProjectionError, match='baseline'):
        verify({1: 'admin'}, {})
    with pytest.raises(ProjectionError, match='baseline'):
        verify({1: 'admin'}, {1: 'customer'})
    with pytest.raises(ProjectionError, match='cursor'):
        snapshot(Recorder([{'items': [], 'nextCursor': 'x'}, {'items': [], 'nextCursor': 'x'}]), 'after')
    with pytest.raises(ProjectionError, match='unavailable'):
        snapshot(Recorder([{'items': []}]), 'after')

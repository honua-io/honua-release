"""A run GitHub created a moment ago can read as 404; the night waits for it instead of refusing."""
import pytest

import nightly_receipts as receipts

SERVER = 'a' * 40
LOCK = 'b' * 40
IMAGE = 'ghcr.io/honua-io/honua-server@sha256:' + 'c' * 64
TAG = f'refs/tags/nightly-candidate/{SERVER}'


class Api:
    """Answers the capacity dispatch; the new run is invisible for `hidden` reads."""

    def __init__(self, hidden):
        self.hidden, self.reads = hidden, 0

    def __call__(self, method, path, body=None):
        if method == 'POST' and path.endswith('/git/refs'):
            return {}
        if path.endswith(f'/git/ref/{TAG.removeprefix("refs/")}'):
            return {'ref': TAG, 'object': {'type': 'commit', 'sha': SERVER}}
        if method == 'POST' and path.endswith('/dispatches'):
            return {'workflow_run_id': 7}
        assert path == 'repos/honua-io/honua-server/actions/runs/7'
        self.reads += 1
        if self.reads <= self.hidden:
            raise receipts.ApiError(404, 'Not Found')
        return {'id': 7, 'event': 'workflow_dispatch', 'head_sha': SERVER,
                'path': f'.github/workflows/{receipts.CAPACITY_WORKFLOW}'}


@pytest.fixture
def slept(monkeypatch):
    delays = []
    monkeypatch.setattr(receipts.time, 'sleep', delays.append)
    return delays


def test_a_dispatched_run_that_reads_404_at_first_is_waited_for(slept):
    api = Api(hidden=2)
    assert receipts.dispatch_capacity(api, 'honua-io/honua-server', SERVER, IMAGE, LOCK) == 7
    assert api.reads == 3 and len(slept) == 2


def test_a_dispatched_run_that_never_appears_still_refuses(slept):
    api = Api(hidden=len(receipts.RUN_VISIBLE_DELAYS))
    with pytest.raises(receipts.ApiError, match='404'):
        receipts.dispatch_capacity(api, 'honua-io/honua-server', SERVER, IMAGE, LOCK)
    assert api.reads == len(receipts.RUN_VISIBLE_DELAYS)

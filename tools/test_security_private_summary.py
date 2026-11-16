"""Private aggregate producer refuses incomplete evidence without leaking responses."""
from http.client import IncompleteRead
import io
import json
from pathlib import Path
from unittest.mock import Mock
import urllib.error

import pytest
import yaml

import rc_gate_window as gate
import release_promise
import security_private_summary as summary

CANDIDATE = gate.Candidate('a' * 40, 'sha256:' + 'b' * 64, 'sha256:' + 'c' * 64)
ROOT = Path(__file__).resolve().parents[1]


def document(count=0):
    return {'data': {'repository': {'isPrivate': True, 'hasIssuesEnabled': True,
            'highLabel': {'name': 'severity/high'}, 'high': {'totalCount': count}}}}


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key, value in {
        'SERVER_SHA': CANDIDATE.server_sha, 'SERVER_DIGEST': CANDIDATE.server_digest,
        'LOCK_DIGEST': CANDIDATE.platform_lock, 'SECURITY_TRACKER_REPOSITORY': 'private/tracker',
        'SECURITY_TRACKER_TOKEN': 'private-token',
    }.items():
        monkeypatch.setenv(key, value)


def test_query_fetches_aggregate_only():
    request = summary.tracker_request()
    assert request.full_url == summary.API
    assert request.get_header('Authorization') == 'Bearer private-token'
    query = json.loads(request.data)
    assert query['variables'] == {'owner': 'private', 'name': 'tracker'}
    assert 'totalCount' in query['query']
    assert not any(word in query['query'] for word in ('nodes', 'edges', 'title', 'body', ' id'))


@pytest.mark.parametrize('count', [0, 1, 2000])
def test_counts_without_pagination(count):
    assert summary.open_high_count(document(count)) == count


@pytest.mark.parametrize('count', [None, True, False, -1, 0.0, '0'])
def test_invalid_count_fails_closed(count):
    with pytest.raises(summary.SummaryUnavailable):
        summary.open_high_count(document(count))


@pytest.mark.parametrize('change', [
    {'isPrivate': False}, {'isPrivate': 1}, {'hasIssuesEnabled': False},
    {'highLabel': None}, {'highLabel': {'name': 'other'}}, {'high': None},
])
def test_repository_and_label_must_be_verified(change):
    response = document()
    response['data']['repository'].update(change)
    with pytest.raises(summary.SummaryUnavailable):
        summary.open_high_count(response)


@pytest.mark.parametrize('response', [None, [], {}, {'errors': ['private detail']},
    {'data': None}, {'data': {'repository': None}},
    {**document(), 'errors': [{'message': 'private detail'}]},
])
def test_missing_or_partial_response_fails_closed(response):
    with pytest.raises(summary.SummaryUnavailable):
        summary.open_high_count(response)


@pytest.mark.parametrize('key', [
    'SERVER_SHA', 'SERVER_DIGEST', 'LOCK_DIGEST',
    'SECURITY_TRACKER_REPOSITORY', 'SECURITY_TRACKER_TOKEN',
])
def test_missing_configuration_does_not_fetch_or_leave_stale_receipt(monkeypatch, key):
    summary.RECEIPT.write_text('old PASS')
    monkeypatch.delenv(key)
    opener = Mock()
    monkeypatch.setattr(summary.urllib.request, 'build_opener', opener)
    assert summary.main() == 1
    opener.assert_not_called()
    assert not summary.RECEIPT.exists()


@pytest.mark.parametrize('error', [
    urllib.error.HTTPError(summary.API, 403, 'private detail', {}, io.BytesIO(b'private detail')),
    urllib.error.URLError('private detail'), IncompleteRead(b'private detail'),
    ValueError('private detail'), summary.SummaryUnavailable(),
])
def test_failures_never_print_private_response(monkeypatch, capsys, error):
    monkeypatch.setattr(summary, 'read_count', Mock(side_effect=error))
    assert summary.main() == 1
    assert not summary.RECEIPT.exists()
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'Private security summary unavailable.\n'


@pytest.mark.parametrize('payload', [b'not json', b'x' * 4097])
def test_bad_or_oversized_response_refused(monkeypatch, payload):
    response = Mock()
    response.__enter__ = Mock(return_value=io.BytesIO(payload))
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(summary.urllib.request, 'build_opener', Mock(return_value=Mock(
        open=Mock(return_value=response))))
    with pytest.raises((ValueError, summary.SummaryUnavailable)):
        summary.read_count(Mock())


def test_redirect_cannot_forward_credentials():
    with pytest.raises(summary.SummaryUnavailable):
        summary.RejectRedirects().redirect_request(None, None, 302, '', {}, 'https://other.test')


@pytest.mark.parametrize('count', [0, 1])
def test_receipt_contains_only_allowed_public_fields(monkeypatch, count):
    monkeypatch.setattr(summary, 'read_count', Mock(return_value=count))
    assert summary.main() == (0 if count == 0 else 1)
    result = json.loads(summary.RECEIPT.read_text())
    assert set(result) == {'schema', 'row', 'status', 'serverSha', 'imageDigest',
                           'platformLockDigest', 'observedAt', 'assertions', 'openHighFindings'}
    assert result['openHighFindings'] == count
    assert result['status'] == ('PASS' if count == 0 else 'FAIL')
    assert result['serverSha'] == CANDIDATE.server_sha
    assert 'private' not in summary.RECEIPT.read_text().replace('privateScoped', '')
    row = next(row for row in release_promise.load_contract() if row['id'] == 'security-open-high')
    now = gate.dt.datetime.now(gate.dt.timezone.utc)
    timestamp = lambda value: value.isoformat().replace('+00:00', 'Z')
    run = {'run_started_at': timestamp(now - gate.dt.timedelta(minutes=1)),
           'updated_at': timestamp(now), 'status': 'completed', 'conclusion': 'success',
           'head_branch': 'trunk', 'event': 'workflow_dispatch',
           'path': '.github/workflows/gate-security-private-summary.yml', 'reports': [result]}
    assert release_promise._run_passes(run, row, (CANDIDATE, now)) == (count == 0)


def test_workflow_trust_boundary_and_artifact_allowlist():
    workflow = yaml.safe_load((ROOT / '.github/workflows/gate-security-private-summary.yml').read_text())
    assert set(workflow[True]) == {'workflow_dispatch'}
    job = workflow['jobs']['summary']
    assert job['if'] == "github.repository == 'honua-io/honua-release' && github.ref == 'refs/heads/trunk'"
    assert workflow['permissions'] == {'contents': 'read'}
    assert job['environment'] == 'security-tracker'
    steps = job['steps']
    producer = next(step for step in steps if step.get('name') == 'Produce private aggregate')
    assert producer['env']['SECURITY_TRACKER_TOKEN'] == '${{ secrets.SECURITY_TRACKER_TOKEN }}'
    upload = steps[-1]
    assert upload['if'] == 'always()'
    assert upload['with']['path'] == 'security-open-high.json'
    assert upload['with']['name'] == 'release-promise-receipts'

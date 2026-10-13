"""Tests for the MCP tool-roster parity gate (honua-server#5734).

Fixtures: tools/fixtures/mcp-roster/mcp-tool-roster.v1.json is the roster honua-server#5742 generates
(copied verbatim); the expected surface is this repository's own e2e/drivers/mcp/expected-tools.json,
reconciled to that roster where a case needs a passing baseline. The durable-control-plane block uses
the honua-release#490 shape ({"_comment", "tools"}) with its 20 names.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_mcp_roster as cmr  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
ROSTER_FIXTURE = REPO_ROOT / 'tools' / 'fixtures' / 'mcp-roster' / 'mcp-tool-roster.v1.json'
EXPECTED = REPO_ROOT / 'e2e' / 'drivers' / 'mcp' / 'expected-tools.json'
PINNED = '87966c3f7b6c840ffc4d4da0b451714ab717b18a'
# Hand-authored tools absent at the 87966c3 pin; the roster (and the 3ecd214 pin) projects them as
# durable-control-plane admin tools.
CONNECT_IMPORT = {'honua_admin_connections_create', 'honua_admin_connections_test',
                  'honua_admin_import_upload_url'}


def roster() -> dict:
    return json.loads(ROSTER_FIXTURE.read_text(encoding='utf-8'))


def reconciled_expected(r: dict | None = None) -> dict:
    """The committed expected-tools.json brought into line with the roster fixture (#490 durable shape)."""
    r = r or roster()
    expected = json.loads(EXPECTED.read_text(encoding='utf-8'))
    expected['fullCatalog']['tools'] = sorted(set(r['static']) | set(r['projectedAdmin']))
    expected['fullCatalog']['requiresDurableControlPlane'] = {
        '_comment': 'durable-control-plane subset (honua-release#490 shape)',
        'tools': sorted(set(r['requiresDurableControlPlane']) - CONNECT_IMPORT),
    }
    return expected


def write(tmp_path: Path, name: str, data) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def manifest(tmp_path: Path, sha: str = PINNED) -> Path:
    path = tmp_path / 'platform-manifest.yaml'
    path.write_text(yaml.safe_dump({'components': {'honua-server': {'sha': sha}}}), encoding='utf-8')
    return path


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int):
    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, code, 'x', {}, io.BytesIO(b''))
    return opener


def test_fixture_is_the_s4_roster_shape():
    r = roster()
    assert r['schemaVersion'] == 1
    assert len(r['static']) == 59 and len(r['projectedAdmin']) == 70
    assert len(r['requiresDurableControlPlane']) == 23
    assert set(r['views']) == {'analyze', 'configure', 'default', 'operate', 'setup'}
    assert r['retired'] == ['honua_propose_operation']


def test_pass_when_expected_matches_the_roster():
    summary = cmr.check(reconciled_expected(), roster(), server_sha=PINNED)
    assert summary['status'] == 'pass', summary['reasons']
    assert summary['serverSha'] == PINNED
    assert summary['missing'] == summary['extra'] == summary['durableMismatch'] == summary['retiredSeen'] == []


def test_committed_expected_tools_matches_the_roster_in_both_topologies():
    """Regenerated at the 3ecd214 re-pin: the committed snapshot equals the roster, durable block included."""
    committed = json.loads(EXPECTED.read_text(encoding='utf-8'))
    for topology in cmr.TOPOLOGIES:
        summary = cmr.check(committed, roster(), topology=topology)
        assert summary['status'] == 'pass', (topology, summary['reasons'])
    durable = committed['fullCatalog']['requiresDurableControlPlane']['tools']
    assert CONNECT_IMPORT <= set(durable) and len(durable) == 23


def test_missing_tool_fails():
    expected = reconciled_expected()
    expected['fullCatalog']['tools'].remove('honua_query_features')
    summary = cmr.check(expected, roster())
    assert summary['status'] == 'fail'
    assert summary['missing'] == ['honua_query_features']


def test_extra_tool_fails():
    expected = reconciled_expected()
    expected['fullCatalog']['tools'].append('honua_not_a_tool')
    summary = cmr.check(expected, roster())
    assert summary['status'] == 'fail'
    assert summary['extra'] == ['honua_not_a_tool']


def test_default_view_drift_fails():
    expected = reconciled_expected()
    expected['defaultView']['tools'].remove('honua_render_map')
    expected['defaultView']['tools'].append('honua_admin_server_status')
    summary = cmr.check(expected, roster())
    assert summary['status'] == 'fail'
    assert summary['defaultMissing'] == ['honua_render_map']
    assert summary['defaultExtra'] == ['honua_admin_server_status']


def test_durable_name_the_roster_does_not_mark_durable_fails():
    expected = reconciled_expected()
    expected['fullCatalog']['requiresDurableControlPlane']['tools'].append('honua_list_layers')
    summary = cmr.check(expected, roster())
    assert summary['status'] == 'fail'
    assert summary['durableMismatch'] == ['honua_list_layers']


def test_durable_bare_list_shape_is_read_too():
    expected = reconciled_expected()
    expected['fullCatalog']['requiresDurableControlPlane'] = ['honua_list_layers']
    assert cmr.check(expected, roster())['durableMismatch'] == ['honua_list_layers']


def test_redis_on_accepts_a_durable_subset():
    """The #490 snapshot lists 20 of the roster's 23 durable names: a subset is fine on redis-on."""
    summary = cmr.check(reconciled_expected(), roster(), topology='redis-on')
    assert summary['status'] == 'pass'


def test_redis_off_requires_the_roster_allowed_absent_set():
    summary = cmr.check(reconciled_expected(), roster(), topology='redis-off')
    assert summary['status'] == 'fail'
    assert summary['durableMismatch'] == sorted(CONNECT_IMPORT)
    assert summary['topology'] == 'redis-off'

    r = roster()
    expected = reconciled_expected(r)
    expected['fullCatalog']['requiresDurableControlPlane']['tools'] = sorted(r['requiresDurableControlPlane'])
    assert cmr.check(expected, r, topology='redis-off')['status'] == 'pass'


@pytest.mark.parametrize('where', ['fullCatalog', 'defaultView', 'stage', 'critical'])
def test_retired_name_seen_fails(where: str):
    r = roster()
    expected = reconciled_expected(r)
    retired = 'honua_propose_operation'
    if where == 'fullCatalog':
        expected['fullCatalog']['tools'].append(retired)
        r['projectedAdmin'].append(retired)  # isolate the retired check from missing/extra
    elif where == 'defaultView':
        expected['defaultView']['tools'].append(retired)
        r['views']['default'].append(retired)
    elif where == 'stage':
        expected['defaultView']['stages'][0]['tools'].append(retired)
    else:
        expected['criticalTools'].append(retired)
    summary = cmr.check(expected, r)
    assert summary['status'] == 'fail'
    assert summary['retiredSeen'] == [retired]


def test_wrong_schema_version_fails():
    r = roster()
    r['schemaVersion'] = 2
    summary = cmr.check(reconciled_expected(), r)
    assert summary['status'] == 'fail'
    assert 'schemaVersion' in summary['reasons'][0]


def test_malformed_roster_fails_rather_than_passing():
    r = roster()
    del r['projectedAdmin']
    assert cmr.check(reconciled_expected(), r)['status'] == 'fail'


def test_roster_404_at_the_pinned_sha_is_blocked(tmp_path, capsys):
    expected = write(tmp_path, 'expected.json', reconciled_expected())
    seen = []

    def opener(request, timeout=None):
        seen.append(request.full_url)
        return _http_error(404)(request, timeout)

    rc = cmr.main(['--manifest', str(manifest(tmp_path)), '--expected', str(expected)], opener=opener)
    out = capsys.readouterr()
    assert rc == 3
    assert seen == [f'https://raw.githubusercontent.com/honua-io/honua-server/{PINNED}/'
                    'docs/gis/data/mcp-tool-roster.v1.json']
    assert f'blocked: roster not published at pinned sha {PINNED}' in out.err
    summary = json.loads(out.out)
    assert summary['status'] == 'blocked' and summary['serverSha'] == PINNED


def test_other_http_errors_are_io_errors_not_blocked(tmp_path, capsys):
    expected = write(tmp_path, 'expected.json', reconciled_expected())
    args = ['--manifest', str(manifest(tmp_path)), '--expected', str(expected)]
    assert cmr.main(args, opener=_http_error(500)) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'error'


def test_fetch_at_the_pinned_sha_passes_and_sends_the_release_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('RELEASE_GH_TOKEN', 'release-token')
    expected = write(tmp_path, 'expected.json', reconciled_expected())
    seen = {}

    def opener(request, timeout=None):
        seen['url'] = request.full_url
        seen['auth'] = request.headers.get('Authorization')
        return Response(ROSTER_FIXTURE.read_bytes())

    sha = 'a' * 40
    rc = cmr.main(['--manifest', str(manifest(tmp_path)), '--expected', str(expected),
                   '--server-sha', sha], opener=opener)
    assert rc == 0
    assert f'/honua-server/{sha}/' in seen['url']
    assert seen['auth'] == 'token release-token'
    assert json.loads(capsys.readouterr().out)['serverSha'] == sha


def test_cli_exit_codes_with_a_local_roster(tmp_path, capsys):
    pin = str(manifest(tmp_path))
    good = write(tmp_path, 'good.json', reconciled_expected())
    assert cmr.main(['--manifest', pin, '--expected', str(good), '--roster', str(ROSTER_FIXTURE)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'pass'

    bad = reconciled_expected()
    bad['fullCatalog']['tools'].append('honua_not_a_tool')
    bad_path = write(tmp_path, 'bad.json', bad)
    assert cmr.main(['--manifest', pin, '--expected', str(bad_path), '--roster', str(ROSTER_FIXTURE)]) == 1
    assert json.loads(capsys.readouterr().out)['extra'] == ['honua_not_a_tool']

    assert cmr.main(['--manifest', pin, '--expected', str(good), '--roster', str(ROSTER_FIXTURE),
                     '--topology', 'redis-off']) == 1
    assert json.loads(capsys.readouterr().out)['durableMismatch'] == sorted(CONNECT_IMPORT)


def test_usage_and_io_errors_exit_2(tmp_path, capsys):
    pin = str(manifest(tmp_path))
    good = write(tmp_path, 'good.json', reconciled_expected())
    assert cmr.main(['--manifest', pin, '--expected', str(good), '--roster', str(tmp_path / 'absent.json')]) == 2
    assert cmr.main(['--manifest', pin, '--expected', str(good), '--server-sha', 'not-a-sha']) == 2
    assert cmr.main(['--topology', 'redis-maybe']) == 2
    empty = write(tmp_path, 'empty.yaml', {'components': {}})
    assert cmr.main(['--manifest', str(empty), '--expected', str(good)]) == 2
    capsys.readouterr()


def test_the_shipped_manifest_pin_is_readable():
    sha = cmr.pinned_server_sha(REPO_ROOT / 'platform-manifest.yaml')
    assert cmr.roster_url(sha) == (f'https://raw.githubusercontent.com/honua-io/honua-server/{sha}/'
                                   'docs/gis/data/mcp-tool-roster.v1.json')

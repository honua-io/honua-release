"""GA capability gate fails each GA capability that lacks its proof, and fails closed on bad input."""
import datetime as dt
import json
from pathlib import Path
import subprocess

import pytest
import yaml

import ga_capabilities as ga
import rc_gate_window as gate
import release_promise

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime.now(dt.timezone.utc)
FRESH = (NOW - dt.timedelta(days=2)).isoformat().replace('+00:00', 'Z')
STALE = (NOW - dt.timedelta(days=9)).isoformat().replace('+00:00', 'Z')
DIGEST, LOCK = 'sha256:' + 'b' * 64, 'sha256:' + 'c' * 64


def capability(key, category='Analytics', **overrides):
    entry = {'key': key, 'displayName': key, 'category': category, 'status': None,
             'provingTestCount': 8, 'maturity': {'implemented': 3}, 'noSurface': None,
             'cite': [], 'interop': []}
    entry.update(overrides)
    return entry


def lane(run_date):
    return {'clientLane': 'js', 'protocol': 'wfs', 'freshness': {'state': 'fresh', 'runDate': run_date}}


def matrix(*entries):
    return {'schemaVersion': '1.2.0', 'capabilities': list(entries)}


def catalog(tests):
    return {'entries': [{'capability': key, 'proving_tests': names} for key, names in tests.items()]}


GOOD = {
    'analytics.h3': capability('analytics.h3'),
    'serve.wfs': capability('serve.wfs', 'Serve', interop=[lane(STALE), lane(FRESH)],
                            cite=[{'suite': 'WFS 2.0', 'passRate': 100.0}]),
    'ai.grounding': capability('ai.grounding', 'AI'),
}
GOOD_TESTS = {
    'analytics.h3': ['H3Tests.Bins_ReturnCells', 'H3Tests.Bins_Anonymous_ReturnsUnauthorized'],
    'serve.wfs': ['WfsTests.Transaction_WithoutRole_IsForbidden'],
    'ai.grounding': ['GroundingTests.Turn_PromptInjection_IsRejected'],
}


def evaluate(entries=None, tests=None):
    entries = GOOD if entries is None else entries
    return {result['key']: result for result in ga.evaluate(
        matrix(*entries.values()), catalog(GOOD_TESTS if tests is None else tests),
        ga.load_ledger(), NOW)}


def test_shipped_ledger_is_valid_and_every_evaluation_resolves():
    ledger = ga.load_ledger()
    rows = ga.promise_rows()
    for key, refs in ledger['evaluations'].items():
        assert key.startswith('ai.'), key
        assert all(ga.evaluation_resolves(ref, rows) for ref in refs), key


def test_a_fully_proven_corpus_has_no_gaps():
    results = evaluate()
    assert {key: result['gaps'] for key, result in results.items()} == {key: {} for key in GOOD}
    assert results['serve.wfs']['protocolFacing'] and not results['analytics.h3']['protocolFacing']
    assert results['analytics.h3']['securityTests'] == 1


GAPS = [
    ('proving', {'analytics.h3': capability('analytics.h3', provingTestCount=4)}, None),
    ('security', {'analytics.h3': capability('analytics.h3')}, {'analytics.h3': ['H3Tests.Bins_ReturnCells']}),
    ('interop', {'serve.wfs': capability('serve.wfs', 'Serve', interop=[lane(STALE)])}, None),
    ('interop', {'serve.wfs': capability('serve.wfs', 'Serve')}, None),
    ('interop', {'analytics.h3': capability('analytics.h3', cite=[{'suite': 'X', 'passRate': 100.0}])}, None),
    ('interop', {'serve.wfs': capability('serve.wfs', 'Serve', interop=[lane(None)])}, None),
    ('cite', {'serve.wfs': capability('serve.wfs', 'Serve', interop=[lane(FRESH)],
                                      cite=[{'suite': 'WFS 2.0', 'passRate': 99.3}])}, None),
    ('evaluation', {'ai.unlisted': capability('ai.unlisted', 'AI')}, {'ai.unlisted': ['A.Forbidden']}),
]


@pytest.mark.parametrize('criterion,entries,tests', GAPS)
def test_each_missing_proof_fails_its_capability(criterion, entries, tests):
    results = evaluate(entries, tests)
    (result,) = results.values()
    assert criterion in result['gaps']
    assert ga.totals(list(results.values()))['capabilitiesFailing'] == 1


def test_a_broken_evaluation_reference_fails(monkeypatch):
    ledger = ga.load_ledger()
    ledger['evaluations']['ai.grounding'] = ['promise:ai-missing', 'path:../outside.json', 'path:nope.md']
    monkeypatch.setattr(ga, 'load_ledger', lambda: ledger)
    gap = evaluate()['ai.grounding']['gaps']['evaluation']
    assert 'promise:ai-missing' in gap and 'path:../outside.json' in gap and 'path:nope.md' in gap


@pytest.mark.parametrize('overrides', [{'status': 'preview'}, {'status': 'experimental'},
                                       {'status': 'internal'}, {'noSurface': 'config-flag'},
                                       {'maturity': {'experimental': 2}}])
def test_capabilities_outside_ga_are_not_judged(overrides):
    entries = {**GOOD, 'raster.x': capability('raster.x', provingTestCount=0, **overrides)}
    assert set(evaluate(entries)) == set(GOOD)


def test_an_empty_ga_corpus_fails_closed():
    with pytest.raises(ga.Unavailable, match='vacuous'):
        evaluate({'raster.x': capability('raster.x', status='preview')})


def commit_tree(root, files):
    root.mkdir(parents=True)
    for name, document in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document))
    git = ['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@example.test']
    subprocess.run([*git, 'init', '-q'], check=True)
    subprocess.run([*git, 'add', '-A'], check=True)
    subprocess.run([*git, 'commit', '-q', '-m', 'tree'], check=True)
    return subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip()


@pytest.fixture
def candidate_tree(monkeypatch, tmp_path):
    def build(entries=None, drop=()):
        files = {ga.MATRIX: matrix(*(GOOD if entries is None else entries).values()),
                 ga.CATALOG: catalog(GOOD_TESTS)}
        sha = commit_tree(tmp_path / 'server', {k: v for k, v in files.items() if k not in drop})
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
        for name, value in (('SERVER_SHA', sha), ('SERVER_DIGEST', DIGEST), ('LOCK_DIGEST', LOCK),
                            ('SERVER_CHECKOUT', 'server')):
            monkeypatch.setenv(name, value)
        return gate.Candidate(sha, DIGEST, LOCK)
    return build


def accepted_by_contract(receipt):
    row = next(row for row in release_promise.load_contract() if row['id'] == ga.ROW)
    stamp = lambda value: value.isoformat().replace('+00:00', 'Z')
    now = dt.datetime.now(dt.timezone.utc)
    run = {'run_started_at': stamp(now - dt.timedelta(minutes=1)), 'updated_at': stamp(now)}
    return release_promise._receipt_valid(receipt, row, (gate.Candidate(
        receipt['serverSha'], DIGEST, LOCK), now, run))


def test_proven_candidate_writes_a_receipt_the_contract_accepts(candidate_tree):
    candidate = candidate_tree()
    assert ga.main([]) == 0
    receipt = json.loads(ga.RECEIPT.read_text())
    assert receipt['status'] == 'PASS' and receipt['serverSha'] == candidate.server_sha
    assert receipt['assertions']['capabilitiesFailing'] == 0
    assert receipt['assertions']['gaCapabilities'] == 3
    assert accepted_by_contract(receipt)
    report = json.loads(ga.REPORT.read_text())
    assert report['serverSha'] == candidate.server_sha and len(report['capabilities']) == 3


def test_unproven_candidate_writes_a_failing_receipt_the_contract_refuses(candidate_tree):
    candidate_tree({**GOOD, 'serve.wms': capability('serve.wms', 'Serve')})
    assert ga.main([]) == 1
    receipt = json.loads(ga.RECEIPT.read_text())
    assert receipt['status'] == 'FAIL' and receipt['assertions']['capabilitiesFailing'] == 1
    assert any(finding.startswith('serve.wms: security:') for finding in receipt['findings'])
    assert not accepted_by_contract(receipt)


def test_diagnostic_run_reports_without_a_receipt(candidate_tree, monkeypatch, tmp_path):
    candidate_tree()
    monkeypatch.delenv('SERVER_SHA')
    summary = tmp_path / 'summary.md'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(summary))
    assert ga.main(['--diagnostic']) == 0
    assert not ga.RECEIPT.exists() and ga.REPORT.exists()
    assert '0 of 3 GA capabilities lack proof' in summary.read_text()


@pytest.mark.parametrize('breakage', ['identity', 'matrix', 'catalog', 'checkout'])
def test_missing_inputs_fail_closed_without_a_receipt(candidate_tree, monkeypatch, breakage):
    drop = {'matrix': (ga.MATRIX,), 'catalog': (ga.CATALOG,)}.get(breakage, ())
    candidate_tree(drop=drop)
    if breakage == 'identity':
        monkeypatch.delenv('LOCK_DIGEST')
    if breakage == 'checkout':
        monkeypatch.setenv('SERVER_SHA', 'd' * 40)
    assert ga.main([]) == 1
    assert not ga.RECEIPT.exists()


def test_ledger_with_an_exemption_list_is_refused(tmp_path):
    ledger = json.loads(ga.LEDGER.read_text())
    ledger['exempt'] = ['serve.wms']
    path = tmp_path / 'ledger.json'
    path.write_text(json.dumps(ledger))
    with pytest.raises(ga.Unavailable, match='keys must be exactly'):
        ga.load_ledger(path)


def test_workflow_fetches_the_candidate_and_uploads_the_contract_receipt():
    workflow = yaml.safe_load((ROOT / '.github/workflows/gate-ga-capabilities.yml').read_text())
    job = workflow['jobs']['capabilities']
    assert "github.ref == 'refs/heads/trunk'" in job['if']
    steps = job['steps']
    assert any('compare/${SERVER_SHA}...trunk' in step.get('run', '') for step in steps)
    assert any(step.get('run') == 'python tools/ga_capabilities.py' for step in steps)
    uploads = [step['with'] for step in steps if 'upload-artifact' in step.get('uses', '')]
    assert {'name': 'release-promise-receipts', 'path': ga.RECEIPT.name} == {
        key: uploads[0][key] for key in ('name', 'path')}

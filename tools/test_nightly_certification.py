"""Contracts for the scheduled strict train and the receipts it obtains itself."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

import nightly_receipts as receipts

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40
OTHER = 'b' * 40


def workflow(name='nightly-certification.yml'):
    document = yaml.safe_load((ROOT / '.github/workflows' / name).read_text())
    # PyYAML 1.1 parses the unquoted Actions key `on` as boolean true.
    document['triggers'] = document.get('on', document.get(True))
    return document


def test_nightly_is_scheduled_and_takes_no_hand_supplied_input():
    nightly = workflow()
    triggers = nightly['triggers']
    assert triggers['schedule'] == [{'cron': '15 11 * * *'}]
    assert triggers['workflow_dispatch'] is None or 'inputs' not in (triggers['workflow_dispatch'] or {})
    assert 'workflow_call' not in triggers
    assert nightly['concurrency']['cancel-in-progress'] is False
    text = (ROOT / '.github/workflows/nightly-certification.yml').read_text()
    assert 'capacity_receipt_url:' not in text.split('jobs:', 1)[0]
    assert 'dr_receipt_url:' not in text.split('jobs:', 1)[0]


def test_nightly_resolves_then_drives_the_strict_train_with_producer_receipts():
    nightly = workflow()
    jobs = nightly['jobs']
    resolve = '\n'.join(step.get('run', '') for step in jobs['resolve']['steps'])
    assert 'resolve_trunk_candidate.py --dry-run --max-commits 500' in resolve
    assert 'mint_nightly_lock.py --stamp' in resolve
    assert 'export LABEL="$label"' in resolve
    assert 'validate_platform.py --exact-candidate' in resolve
    assert 'refs/nightly-candidates/' in resolve
    snapshot = next(step for step in jobs['resolve']['steps'] if step.get('id') == 'snapshot')
    assert 'GH_TOKEN' in snapshot['env']
    assert 'LABEL' not in snapshot['env']
    assert 'steps.snapshot.outputs' not in snapshot['run']

    capacity = '\n'.join(step.get('run', '') for step in jobs['capacity']['steps'])
    assert 'gh workflow run "$workflow" --repo "$repo" --ref "$SERVER_SHA"' in capacity
    assert 'capacity-soak-candidate.yml' in capacity
    assert '-f "candidate_sha=${SERVER_SHA}"' in capacity
    assert '-f "candidate_image=${SERVER_IMAGE}"' in capacity
    assert '-f "lock_ref=${CANDIDATE_REF}"' in capacity
    assert '-f publish=true' in capacity
    assert 'nightly_receipts.py new-run-id' in capacity
    assert 'nightly_receipts.py capacity-url' in capacity
    assert 'gh run watch' in capacity
    assert 'capacity producer' in capacity and 'exit 1' in capacity

    drill = '\n'.join(step.get('run', '') for step in jobs['dr']['steps'])
    assert 'gh workflow run "$workflow" --repo "$repo" --ref "$REVIEWED_SHA"' in drill
    assert 'dr-drill-local-docker.yml' in drill
    assert '-f "candidate_ref=${CANDIDATE_REF}"' in drill
    assert 'nightly_receipts.py dr-url' in drill
    assert 'dr-receipt-location-' in drill
    assert 'DR producer' in drill and 'exit 1' in drill

    train = jobs['train']
    assert train['uses'] == './.github/workflows/release-train.yml'
    assert train['with']['dry_run'] is False
    assert train['with']['nightly'] is True
    assert train['with']['candidate_ref'] == '${{ needs.resolve.outputs.candidate_ref }}'
    assert train['with']['capacity_receipt_url'] == '${{ needs.capacity.outputs.receipt_url }}'
    assert train['with']['dr_receipt_url'] == '${{ needs.dr.outputs.receipt_url }}'
    assert 'needs.resolve.result' in train['if']
    assert "receipt_url != ''" in train['if']

    mint = jobs['mint']
    assert mint['if'] == "needs.train.result == 'success'"
    minted = '\n'.join(step.get('run', '') for step in mint['steps'] if step.get('run'))
    assert 'mint_nightly_lock.py' in minted
    assert 'refs/nightly-locks/' in minted
    assert '--force' not in resolve and '--force' not in minted
    pin = 'sigstore/cosign-installer@6f9f17788090df1f26f669e9d70d6ae9567deba6 # v4.1.2'
    promote = (ROOT / '.github/workflows/promote.yml').read_text()
    nightly_text = (ROOT / '.github/workflows/nightly-certification.yml').read_text()
    assert pin in promote and pin in nightly_text


def test_manual_dispatch_remains_and_journey_todo_fails_closed():
    train = workflow('release-train.yml')
    dispatch = train['triggers']['workflow_dispatch']['inputs']
    called = train['triggers']['workflow_call']['inputs']
    for inputs in (dispatch, called):
        assert inputs['capacity_receipt_url']['required'] is True
        assert inputs['dr_receipt_url']['required'] is True
        assert 'candidate_ref' in inputs
        assert 'nightly' in inputs
    assert not (ROOT / '.github/workflows/gate-journey.yml').exists()
    job = train['jobs']['gate_journey_todo']
    assert job['name'] == 'TODO required promise-journey gate (release 386)'
    script = job['steps'][0]['run']
    assert 'gate-journey.yml' in script
    red = subprocess.run(['bash', '-euc', script], capture_output=True, text=True, env={'STRICT': 'true'})
    assert red.returncode == 1
    dry = subprocess.run(['bash', '-euc', script], capture_output=True, text=True, env={'STRICT': 'false'})
    assert dry.returncode == 0
    report = train['jobs']['report']
    assert 'gate_journey_todo' in report['needs']
    assert 'journey|' in '\n'.join(step.get('run', '') for step in report['steps'])


def test_new_run_id_is_the_newest_dispatch_for_that_head():
    runs = [
        {'databaseId': 1, 'event': 'workflow_dispatch', 'headSha': SHA},
        {'databaseId': 2, 'event': 'schedule', 'headSha': SHA},
        {'databaseId': 3, 'event': 'workflow_dispatch', 'headSha': OTHER},
        {'databaseId': 4, 'event': 'workflow_dispatch', 'headSha': SHA},
        {'databaseId': 9, 'event': 'workflow_dispatch', 'headSha': SHA},
    ]
    assert receipts.new_run_id({1}, runs, head_sha=SHA) == 9
    assert receipts.new_run_id({1, 4, 9}, runs, head_sha=SHA) is None
    with pytest.raises(ValueError, match='full commit'):
        receipts.new_run_id(set(), [], head_sha='trunk')


def test_capacity_receipt_url_is_one_commit_pinned_raw_file():
    commit = 'c' * 40
    url = receipts.capacity_receipt_url('honua-io/honua-server', SHA, '42', [{
        'sha': commit,
        'commit': {'message': f'capacity soak receipt for {SHA} (run 42)\n\nbody'},
    }])
    assert url == f'https://raw.githubusercontent.com/honua-io/honua-server/{commit}/capacity/{SHA}-42.json'
    with pytest.raises(LookupError, match='found 0'):
        receipts.capacity_receipt_url('honua-io/honua-server', SHA, '42', [])
    duplicate = [
        {'sha': commit, 'commit': {'message': f'capacity soak receipt for {SHA} (run 42)'}},
        {'sha': OTHER, 'commit': {'message': f'capacity soak receipt for {SHA} (run 42)'}},
    ]
    with pytest.raises(LookupError, match='found 2'):
        receipts.capacity_receipt_url('honua-io/honua-server', SHA, '42', duplicate)


def test_dr_receipt_url_refuses_branch_html_and_non_https():
    raw = 'https://raw.githubusercontent.com/honua-io/honua-evidence/abc/data/receipt.json'
    assert receipts.dr_receipt_url({'receipt_url': raw}) == raw
    for url in ('https://github.com/honua-io/honua-evidence/blob/trunk/receipt.json',
                'http://raw.githubusercontent.com/honua-io/honua-evidence/abc/receipt.json',
                'refs/heads/trunk'):
        with pytest.raises(ValueError, match='immutable https'):
            receipts.dr_receipt_url({'receipt_url': url})


def test_receipt_cli_exit_codes(tmp_path):
    before = tmp_path / 'before.json'
    runs = tmp_path / 'runs.json'
    before.write_text('[1]')
    runs.write_text(json.dumps([{'databaseId': 1, 'event': 'workflow_dispatch', 'headSha': SHA}]))
    missing = subprocess.run([sys.executable, str(ROOT / 'tools/nightly_receipts.py'), 'new-run-id',
                              '--before', str(before), '--runs', str(runs), '--head-sha', SHA],
                             capture_output=True, text=True)
    assert missing.returncode == 2
    runs.write_text(json.dumps([{'databaseId': 7, 'event': 'workflow_dispatch', 'headSha': SHA}]))
    found = subprocess.run([sys.executable, str(ROOT / 'tools/nightly_receipts.py'), 'new-run-id',
                            '--before', str(before), '--runs', str(runs), '--head-sha', SHA],
                           capture_output=True, text=True)
    assert found.returncode == 0 and found.stdout.strip() == '7'
    location = tmp_path / 'receipt-location.json'
    location.write_text(json.dumps({'receipt_url': 'https://github.com/honua-io/honua-evidence/blob/x'}))
    refused = subprocess.run([sys.executable, str(ROOT / 'tools/nightly_receipts.py'), 'dr-url',
                              '--location', str(location)], capture_output=True, text=True)
    assert refused.returncode == 1 and refused.stderr.startswith('REFUSED:')

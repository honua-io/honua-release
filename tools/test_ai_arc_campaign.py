"""Campaign scheduling: three slots per host, pause for adjudication, sealed reserve consumption, VM slots."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_arc_campaign as C  # noqa: E402
import ai_arc_local as L  # noqa: E402


def rows(n, prefix='local-docker:A1:'):
    return [{'runId': f'{prefix}{i:03d}', 'variant': 'AB'[i % 2], 'caseId': None, 'set': None} for i in range(1, n + 1)]


def fake_launch(out, statuses, log, gate=None):
    def launch(row, slot):
        log.append((row, slot, time.monotonic()))
        if gate:
            gate(row)
        path = Path(out) / 'runs' / row['runId'].replace(':', '-') / 'trial-receipt.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'runId': row['runId'], 'status': statuses.get(row['runId'], 'completed'),
                                    'startedAt': '2026-11-02T09:00:00Z', 'finishedAt': '2026-11-02T09:30:00Z'}))
        return 0
    return launch


GOOD = {'finding': 'harness-fault', 'adjudication': 'https://github.com/honua-io/honua-release/issues/9',
        'reviewerFamily': 'openai', 'adjudicatedAt': '2026-11-02T10:00:00Z'}
RESERVE = [{'runId': 'local-docker:A1:r01', 'variant': 'H', 'caseId': 'r01', 'set': 'reserve'}]


def adjudicate_later(tmp_path, decision, delay=0.3, run='local-docker-A1-001'):
    written = []

    def write():
        time.sleep(delay)
        written.append(time.monotonic())
        target = tmp_path / 'adjudications' / (run + '.json')
        staging = target.with_suffix('.tmp')
        staging.write_text(json.dumps(decision))
        staging.replace(target)
    threading.Thread(target=write, daemon=True).start()
    return written


def test_three_concurrent_slots_per_host_and_no_reruns(tmp_path):
    log, active, peak = [], [0], [0]
    lock = threading.Lock()

    def gate(row):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
    results = C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(8), out=tmp_path, hosts=['h1', 'h2'],
                             per_host=3, common=[], launch=fake_launch(tmp_path, {}, log, gate), wait_poll=0.01)
    assert len(results) == 8 and len({r['runId'] for r, _, _ in log}) == 8 and peak[0] <= 6
    assert {slot for _, slot, _ in log} <= {(h, i) for h in ('h1', 'h2') for i in range(3)}


def test_valid_adjudication_resumes_with_the_reserve_routed_to_its_manifest(tmp_path):
    log = []
    written = adjudicate_later(tmp_path / 'c', GOOD)
    (tmp_path / 'c' / 'adjudications').mkdir(parents=True)
    results = C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(3), out=tmp_path / 'c', hosts=['h1'],
                             per_host=1, common=[], reserve_rows=RESERVE, wait_poll=0.01,
                             launch=fake_launch(tmp_path / 'c', {'local-docker:A1:001': 'undetermined'}, log))
    order = [r['runId'] for r, _, _ in log]
    assert order[:2] == ['local-docker:A1:001', 'local-docker:A1:r01']
    assert log[1][0]['set'] == 'reserve' and log[1][2] >= written[0]
    voids = json.loads((tmp_path / 'c' / 'voids.json').read_text())
    assert voids[0]['replacementRunId'] == 'local-docker:A1:r01' and len(results) == 4


@pytest.mark.parametrize('bad', [dict(GOOD, reviewerFamily='anthropic'), dict(GOOD, reviewerFamily='unrecognized'),
                                 dict(GOOD, adjudication='not-a-url'), dict(GOOD, adjudicatedAt='2026-11-02T09:00:00Z')])
def test_invalid_adjudication_never_resumes(tmp_path, bad):
    log = []
    (tmp_path / 'adjudications').mkdir(parents=True)
    (tmp_path / 'adjudications' / 'local-docker-A1-001.json').write_text(json.dumps(bad))
    with pytest.raises(TimeoutError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(3), out=tmp_path, hosts=['h1'], per_host=1,
                       common=[], reserve_rows=RESERVE, wait_poll=0.01, max_wait=0.5,
                       launch=fake_launch(tmp_path, {'local-docker:A1:001': 'undetermined'}, log))
    assert [r['runId'] for r, _, _ in log] == ['local-docker:A1:001'], 'nothing else started'
    state = json.loads((tmp_path / 'campaign-state.json').read_text())
    assert state['pending'] == ['local-docker:A1:001'] and state['rejectedAdjudications']


def test_terminal_undetermined_run_still_waits_for_adjudication(tmp_path):
    log = []
    with pytest.raises(TimeoutError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(1), out=tmp_path, hosts=['h1'], per_host=1,
                       common=[], wait_poll=0.01, max_wait=0.3,
                       launch=fake_launch(tmp_path, {'local-docker:A1:001': 'undetermined'}, log))
    assert (tmp_path / 'PENDING-ADJUDICATION').read_text().strip() == 'local-docker:A1:001'


def test_third_void_stops_the_campaign(tmp_path):
    log = []
    statuses = {f'local-docker:A1:{i:03d}': 'undetermined' for i in range(1, 4)}
    (tmp_path / 'adjudications').mkdir(parents=True)
    for i in range(1, 4):
        (tmp_path / 'adjudications' / f'local-docker-A1-{i:03d}.json').write_text(json.dumps(GOOD))
    reserves = [{'runId': f'local-docker:A1:r{i:02d}', 'variant': 'H', 'caseId': f'r{i:02d}', 'set': 'reserve'}
                for i in range(1, 4)]
    with pytest.raises(RuntimeError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(3), out=tmp_path, hosts=['h1'], per_host=1,
                       common=[], reserve_rows=reserves, wait_poll=0.01, launch=fake_launch(tmp_path, statuses, log))
    assert len(json.loads((tmp_path / 'voids.json').read_text())) == 2


def test_non_harness_finding_keeps_the_run_counted(tmp_path):
    log = []
    (tmp_path / 'adjudications').mkdir(parents=True)
    (tmp_path / 'adjudications' / 'local-docker-A1-001.json').write_text(json.dumps(dict(GOOD, finding='product-failure')))
    C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(2), out=tmp_path, hosts=['h1'], per_host=1,
                   common=[], reserve_rows=RESERVE, wait_poll=0.01,
                   launch=fake_launch(tmp_path, {'local-docker:A1:001': 'undetermined'}, log))
    assert [r['runId'] for r, _, _ in log] == ['local-docker:A1:001', 'local-docker:A1:002']
    assert json.loads((tmp_path / 'voids.json').read_text()) == []


def test_slots_validate_hosts_and_capacity():
    assert C.slots(['a'], 3) == [('a', 0), ('a', 1), ('a', 2)]
    with pytest.raises(ValueError):
        C.slots(['a'], 4)
    assert L.slot_ports(0) == (18080, 18081) and L.slot_ports(2) == (18280, 18281)
    with pytest.raises(ValueError):
        L.slot_ports(3)


def test_vm_staging_keeps_secrets_off_argv_and_teardown_is_scoped(tmp_path):
    calls = []

    def run(argv, input=None, **kw):
        calls.append((argv, (input or b'').decode()))
        return subprocess.CompletedProcess(argv, 0, b'29.9.2\n', b'')
    identity, known = tmp_path / 'id', tmp_path / 'kh'
    identity.write_text('k')
    known.write_text('h')
    vm = L.VmSlot(host='honua-eval-1', user='ubuntu', identity_file=str(identity), known_hosts=str(known),
                  project='arc-abcdef123456', index=0, run=run)
    vm.stage({'image': 'ghcr.io/honua-io/honua-server', 'imageDigest': 'sha256:' + 'a' * 64})
    argv, script = calls[0]
    assert all(v not in ' '.join(argv) for v in vm.secrets.values()) and all(v in script for v in vm.secrets.values())
    assert 'HONUA_IMAGE=ghcr.io/honua-io/honua-server@sha256:' in script and 'StrictHostKeyChecking=yes' in argv
    vm.teardown()
    teardown = calls[-1][1]
    assert '--project-name arc-abcdef123456' in teardown and 'rm -rf -- /home/ubuntu/honua-arc/arc-abcdef123456' in teardown
    with pytest.raises(ValueError):
        L.VmSlot(host='h;rm -rf /', user='ubuntu', identity_file='i', known_hosts='k', project='arc-abcdef123456', index=0)


def test_reserve_command_names_the_reserve_manifest(tmp_path, monkeypatch):
    seen = []

    def fake_run(command, timeout=None, **kw):
        seen.append(command)
        out = Path(command[command.index('--output') + 1])
        out.mkdir(parents=True)
        (out / 'trial-receipt.json').write_text(json.dumps({'status': 'completed'}))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(C.subprocess, 'run', fake_run)
    C.run_campaign(target='local-docker', arm='A1', schedule_rows=[{'runId': 'local-docker:A1:h00', 'variant': 'H',
                                                                    'caseId': 'h00', 'set': 'primary'}] + RESERVE,
                   out=tmp_path, hosts=['h1'], per_host=1, common=[], wait_poll=0.01, max_wait=0.5)
    sets = [c[c.index('--goal-set') + 1] for c in seen]
    assert sets == ['primary', 'reserve']



def test_a_partially_written_adjudication_is_read_on_a_later_pass(tmp_path):
    (tmp_path / 'adjudications').mkdir(parents=True)
    path = tmp_path / 'adjudications' / 'local-docker-A1-001.json'
    path.write_text('{"finding": "harn')                      # mid-write
    log = []

    def finish():
        time.sleep(0.2)
        path.write_text(json.dumps(GOOD))
    threading.Thread(target=finish, daemon=True).start()
    C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(1), out=tmp_path, hosts=['h1'], per_host=1,
                   common=[], reserve_rows=RESERVE, wait_poll=0.01, max_wait=5,
                   launch=fake_launch(tmp_path, {'local-docker:A1:001': 'undetermined'}, log))
    assert [r['runId'] for r, _, _ in log] == ['local-docker:A1:001', 'local-docker:A1:r01']


def flagged_launch(out, log):
    """Run 001 carries a false-success flag (ARC v2); others complete clean."""
    def launch(row, slot):
        log.append((row, slot, time.monotonic()))
        path = Path(out) / 'runs' / row['runId'].replace(':', '-') / 'trial-receipt.json'
        path.parent.mkdir(parents=True)
        flags = [{'stage': 'setup', 'flagId': 'setup:running'}] if row['runId'].endswith('001') else []
        path.write_text(json.dumps({'runId': row['runId'], 'status': 'failed' if flags else 'completed',
                                    'falseSuccessFlags': flags,
                                    'startedAt': '2026-11-02T09:00:00Z', 'finishedAt': '2026-11-02T09:30:00Z'}))
        return 0
    return launch


FS_VERDICT = [{'runId': 'local-docker:A1:001', 'flagId': 'setup:running', 'verdict': 'rejected',
               'reviewerFamily': 'openai', 'adjudication': 'https://github.com/honua-io/honua-release/issues/9',
               'adjudicatedAt': '2026-11-02T10:00:00Z'}]


def test_a_false_success_flag_pauses_the_campaign_until_the_other_family_adjudicates(tmp_path):
    log = []
    (tmp_path / 'adjudications').mkdir(parents=True)
    with pytest.raises(TimeoutError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(2), out=tmp_path, hosts=['h1'], per_host=1,
                       common=[], wait_poll=0.01, max_wait=0.3, launch=flagged_launch(tmp_path, log))
    assert [r['runId'] for r, _, _ in log] == ['local-docker:A1:001'], 'nothing else started'
    (tmp_path / 'adjudications' / 'local-docker-A1-001.false-success.json').write_text(
        json.dumps([dict(FS_VERDICT[0], reviewerFamily='anthropic')]))
    out2 = tmp_path / 'again'
    (out2 / 'adjudications').mkdir(parents=True)
    (out2 / 'adjudications' / 'local-docker-A1-001.false-success.json').write_text(json.dumps(FS_VERDICT))
    log2 = []
    C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(2), out=out2, hosts=['h1'], per_host=1,
                   common=[], wait_poll=0.01, launch=flagged_launch(out2, log2))
    assert [r['runId'] for r, _, _ in log2] == ['local-docker:A1:001', 'local-docker:A1:002']
    assert json.loads((out2 / 'false-success-adjudications.json').read_text())[0]['verdict'] == 'rejected'
    assert json.loads((out2 / 'voids.json').read_text()) == []


def test_a_same_family_false_success_verdict_never_resumes(tmp_path):
    log = []
    (tmp_path / 'adjudications').mkdir(parents=True)
    (tmp_path / 'adjudications' / 'local-docker-A1-001.false-success.json').write_text(
        json.dumps([dict(FS_VERDICT[0], reviewerFamily='anthropic')]))
    with pytest.raises(TimeoutError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(2), out=tmp_path, hosts=['h1'], per_host=1,
                       common=[], wait_poll=0.01, max_wait=0.3, launch=flagged_launch(tmp_path, log))
    assert len(log) == 1 and 'other model family' in json.loads(
        (tmp_path / 'campaign-state.json').read_text())['rejectedAdjudications']['local-docker:A1:001']


def test_a_failed_run_with_a_suspected_harness_fault_waits_for_adjudication(tmp_path):
    """Astra S3: the barrier stops on a suspected harness defect whatever the receipt's status."""
    def launch(row, slot):
        path = Path(tmp_path) / 'runs' / row['runId'].replace(':', '-') / 'trial-receipt.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'runId': row['runId'], 'status': 'failed', 'stages': [
            {'stage': 'ops', 'outcome': 'fail', 'cause': 'harness fault did not fire', 'assertions': []}]}))
        return 0
    with pytest.raises(TimeoutError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(2), out=tmp_path, hosts=['h1'], per_host=1,
                       common=[], wait_poll=0.01, max_wait=0.3, launch=launch)
    assert (tmp_path / 'PENDING-ADJUDICATION').read_text().strip() == 'local-docker:A1:001'


@pytest.mark.parametrize('receipt,suspect', [
    ({'stages': [{'outcome': 'pass'}]}, False),
    ({'stages': [{'outcome': 'fail', 'cause': 'required assertions did not pass'}]}, False),
    ({'stages': [{'outcome': 'undetermined'}]}, True),
    ({'stages': [{'outcome': 'fail', 'cause': 'harness error: x'}]}, True),
    ({'stages': [{'outcome': 'fail', 'assertions': [{'harnessFault': True}]}]}, True),
    ({'stages': [], 'slotFacts': {'slotPreparation': {'status': 'failed'}}}, True),
    ({'stages': [], 'slotFacts': {'slotPreparation': {'status': 'converged'}}}, False)])
def test_suspected_harness_faults(receipt, suspect):
    assert C.suspected_harness_fault(receipt) is suspect


def test_an_operator_hold_stops_new_launches(tmp_path):
    log = []
    (tmp_path / 'HOLD').write_text('scorer defect suspected\n')
    with pytest.raises(TimeoutError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(1), out=tmp_path, hosts=['h1'], per_host=1,
                       common=[], wait_poll=0.01, max_wait=0.3, launch=fake_launch(tmp_path, {}, log))
    assert log == []


def test_a_hold_placed_on_a_finished_run_stops_launches_until_adjudicated(tmp_path):
    """Sol 09e293be item 2: per-run holds are checked before every launch, not only when a run finishes."""
    log = []

    def gate(row):
        if row['runId'].endswith('001'):
            (tmp_path / 'holds').mkdir(exist_ok=True)
            (tmp_path / 'holds' / 'local-docker-A1-001.hold').write_text('suspect scorer\n')
    with pytest.raises(TimeoutError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(3), out=tmp_path, hosts=['h1'], per_host=1,
                       common=[], wait_poll=0.01, max_wait=0.4, launch=fake_launch(tmp_path, {}, log, gate=gate))
    assert [r['runId'] for r, _, _ in log] == ['local-docker:A1:001']
    assert (tmp_path / 'PENDING-ADJUDICATION').read_text().strip() == 'local-docker:A1:001'


def test_a_hold_on_an_in_flight_run_blocks_other_free_slots(tmp_path):
    log, release = [], threading.Event()

    def gate(row):
        if row['runId'].endswith('001'):
            (tmp_path / 'holds').mkdir(exist_ok=True)
            (tmp_path / 'holds' / 'local-docker-A1-001.hold').write_text('watching\n')
            release.wait(0.5)
    with pytest.raises(TimeoutError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(3), out=tmp_path, hosts=['h1'], per_host=2,
                       common=[], wait_poll=0.01, max_wait=0.3, launch=fake_launch(tmp_path, {}, log, gate=gate))
    assert len(log) <= 2, 'at most the run already launched beside it'



@pytest.mark.parametrize('decision', [{}, {'finding': 'product-failure'}, dict(GOOD, finding='made-up'),
                                      dict(GOOD, finding='product-failure', reviewerFamily='anthropic'),
                                      dict(GOOD, finding='product-failure', adjudication='http://x')])
def test_a_decision_that_keeps_the_run_counted_is_validated_too(tmp_path, decision):
    """Sol 4ac23739 item 1: an empty or unsupported decision never clears the barrier."""
    log = []
    (tmp_path / 'adjudications').mkdir(parents=True)
    (tmp_path / 'adjudications' / 'local-docker-A1-001.json').write_text(json.dumps(decision))
    with pytest.raises(TimeoutError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(2), out=tmp_path, hosts=['h1'], per_host=1,
                       common=[], wait_poll=0.01, max_wait=0.3,
                       launch=fake_launch(tmp_path, {'local-docker:A1:001': 'undetermined'}, log))
    assert [r['runId'] for r, _, _ in log] == ['local-docker:A1:001']


def test_a_fresh_hold_after_an_adjudication_needs_a_fresh_decision(tmp_path):
    """Sol 4ac23739 item 2: acknowledgement is bound to the hold generation it answered."""
    log = []
    holds = tmp_path / 'holds'

    def gate(row):
        if row['runId'].endswith('001'):
            holds.mkdir(exist_ok=True)
            (holds / 'local-docker-A1-001.hold').write_text('first\n')
        if row['runId'].endswith('002'):
            time.sleep(0.05)
            (holds / 'local-docker-A1-001.hold').write_text('second: new scorer defect\n')
    (tmp_path / 'adjudications').mkdir(parents=True)

    def decide():
        while not (holds / 'local-docker-A1-001.hold').is_file():
            time.sleep(0.01)
        time.sleep(0.05)
        (tmp_path / 'adjudications' / 'local-docker-A1-001.json').write_text(json.dumps(dict(GOOD, finding='product-failure')))
    threading.Thread(target=decide, daemon=True).start()
    with pytest.raises(TimeoutError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(3), out=tmp_path, hosts=['h1'], per_host=1,
                       common=[], wait_poll=0.01, max_wait=1.0, launch=fake_launch(tmp_path, {}, log, gate=gate))
    assert [r['runId'] for r, _, _ in log] == ['local-docker:A1:001', 'local-docker:A1:002']



def test_a_hold_replaced_during_validation_stays_pending(tmp_path, monkeypatch):
    """Sol 96b2546f: the acknowledged generation is the one captured before validation."""
    import ai_arc_runner as AR
    log = []
    holds = tmp_path / 'holds'
    holds.mkdir(parents=True)
    (tmp_path / 'adjudications').mkdir(parents=True)
    real = AR.check_adjudication

    def replacing(*a, **k):
        time.sleep(0.01)
        (holds / 'local-docker-A1-001.hold').write_text('replaced during validation\n')
        return real(*a, **k)
    monkeypatch.setattr(AR, 'check_adjudication', replacing)

    def gate(row):
        if row['runId'].endswith('001'):
            (holds / 'local-docker-A1-001.hold').write_text('first\n')
            time.sleep(0.02)
            (tmp_path / 'adjudications' / 'local-docker-A1-001.json').write_text(
                json.dumps(dict(GOOD, finding='product-failure')))
    with pytest.raises(TimeoutError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(2), out=tmp_path, hosts=['h1'], per_host=1,
                       common=[], wait_poll=0.01, max_wait=0.5, launch=fake_launch(tmp_path, {}, log, gate=gate))
    assert [r['runId'] for r, _, _ in log] == ['local-docker:A1:001']



def test_a_harness_fault_decision_racing_a_new_hold_commits_no_void(tmp_path, monkeypatch):
    """Sol e265bb02: the generation is checked before any void, reserve or replacement is committed."""
    import ai_arc_runner as AR
    log = []
    holds = tmp_path / 'holds'
    holds.mkdir(parents=True)
    (tmp_path / 'adjudications').mkdir(parents=True)
    real = AR.check_adjudication

    def replacing(*a, **k):
        time.sleep(0.01)
        (holds / 'local-docker-A1-001.hold').write_text('replaced during validation\n')
        return real(*a, **k)
    monkeypatch.setattr(AR, 'check_adjudication', replacing)

    def gate(row):
        if row['runId'].endswith('001'):
            (holds / 'local-docker-A1-001.hold').write_text('first\n')
            time.sleep(0.02)
            (tmp_path / 'adjudications' / 'local-docker-A1-001.json').write_text(json.dumps(GOOD))
    with pytest.raises(TimeoutError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(2), out=tmp_path, hosts=['h1'], per_host=1,
                       common=[], reserve_rows=RESERVE, wait_poll=0.01, max_wait=0.5,
                       launch=fake_launch(tmp_path, {}, log, gate=gate))
    assert json.loads((tmp_path / 'voids.json').read_text()) == [] if (tmp_path / 'voids.json').is_file() else True
    state = json.loads((tmp_path / 'campaign-state.json').read_text())
    assert state['voids'] == [] and state['reservesLeft'] == ['local-docker:A1:r01']


def spend(cap, estimate=25):
    import ai_arc_runner as R
    doc = json.loads(json.dumps(R.load_arc()['spendCap']))
    doc.update(capUsd=cap, perRunEstimateUsd=estimate)
    return doc


def test_run_cost_prices_bedrock_tokens_and_compute_and_leaves_the_subscription_unknown():
    receipt = {'startedAt': '2026-11-02T09:00:00Z', 'finishedAt': '2026-11-02T11:00:00Z',
               'stages': [{'stage': 'studio', 'assistantUsage': [{'inputTokens': 1_000_000, 'outputTokens': 100_000,
                                                                  'cacheReadInputTokens': 2_000_000}]},
                          {'stage': 'setup', 'assistantUsage': None}]}
    cost = C.run_cost(receipt, 'local-docker', spend(2500))
    rates = spend(2500)['rates']
    bedrock = rates['bedrock']
    assert cost['bedrockUsd'] == round(bedrock['inputPerMTok'] + 0.1 * bedrock['outputPerMTok']
                                       + 2 * bedrock['cacheReadPerMTok'], 4) and cost['cliSubscriptionUsd'] == 'unknown'
    assert cost['rateBasis']['compute'].startswith('estimate') and 'price list' in cost['rateBasis']['bedrock']
    assert cost['computeUsd'] == round(2 * rates['vmHourUsd'], 4)
    assert C.run_cost(receipt, 'aws-ecs', spend(2500))['computeUsd'] == round(2 * rates['awsCellHourUsd'], 4)
    assert C.run_cost(None, 'local-docker', spend(2500, 40))['totalUsd'] == 40


def test_the_campaign_stops_scheduling_before_the_spend_cap_and_reports_it(tmp_path):
    import ai_arc_runner as R
    arc = json.loads(json.dumps(R.load_arc()))
    arc['spendCap'] = spend(cap=25.5, estimate=25)   # each 30-min run costs 0.34; the third launch projects 25.68
    log = []
    results = C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(5), out=tmp_path / 'c', hosts=['h1'],
                             per_host=1, common=[], launch=fake_launch(tmp_path / 'c', {}, log), wait_poll=0.01,
                             arc=arc)
    assert [r['runId'] for r in results] == ['local-docker:A1:001', 'local-docker:A1:002']
    stop = json.loads((tmp_path / 'c' / 'SPEND-CAP-REACHED').read_text())
    assert stop['capUsd'] == 25.5 and stop['projectedUsd'] > 25.5 and stop['unstarted'][0] == 'local-docker:A1:003'
    ledger = json.loads((tmp_path / 'c' / 'spend-ledger.json').read_text())
    assert [r['runId'] for r in ledger if r['kind'] == 'run'] == ['local-docker:A1:001', 'local-docker:A1:002']
    assert all(r['cliSubscriptionUsd'] == 'unknown' for r in ledger if r['kind'] == 'run')
    state = json.loads((tmp_path / 'c' / 'campaign-state.json').read_text())
    assert state['spendCapStop']['runId'] == 'local-docker:A1:003'


def test_campaigns_sharing_a_ledger_share_one_cap(tmp_path):
    import ai_arc_runner as R
    arc = json.loads(json.dumps(R.load_arc()))
    arc['spendCap'] = spend(cap=25.5, estimate=25)
    shared = tmp_path / 'gate-ledger.json'
    first = C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(1), out=tmp_path / 'a', hosts=['h1'],
                           per_host=1, common=[], launch=fake_launch(tmp_path / 'a', {}, []), wait_poll=0.01,
                           arc=arc, ledger=shared)
    second = C.run_campaign(target='local-docker', arm='A2', schedule_rows=rows(3, 'local-docker:A2:'),
                            out=tmp_path / 'b', hosts=['h1'], per_host=1, common=[],
                            launch=fake_launch(tmp_path / 'b', {}, []), wait_poll=0.01, arc=arc, ledger=shared)
    assert len(first) == 1 and len(second) == 1 and (tmp_path / 'b' / 'SPEND-CAP-REACHED').is_file()


def test_the_bedrock_rates_are_the_us_profile_price_list_rates_and_compute_is_labelled_an_estimate():
    """Laptop decision 11-01: the us. cross-region profile bills at the Regional CRIS on-demand rates
    (AmazonBedrockFoundationModels us-east-1); VM and cell rates are labelled estimates."""
    import ai_arc_runner as R
    doc = R.load_arc()['spendCap']['rates']
    bedrock = doc['bedrock']
    assert (bedrock['inputPerMTok'], bedrock['outputPerMTok'], bedrock['cacheReadPerMTok'],
            bedrock['cacheWritePerMTok']) == (3.3, 16.5, 0.33, 4.125)
    assert bedrock['model'].startswith('us.anthropic.claude-sonnet-4-6') and 'Regional CRIS' in bedrock['basis']
    assert doc['vmBasis'].startswith('ESTIMATE') and doc['awsCellBasis'].startswith('ESTIMATE')


def test_the_workflow_passes_one_shared_spend_ledger_per_gate_campaign():
    """Astra S4: every target and arm dispatched for one goal set and candidate lock shares one ledger outside
    the workspace, and the ledger and any stop are retained in the campaign artifact."""
    text = (Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'ai-arc.yml').read_text()
    assert 'ledger="$HOME/.honua-ai-arc/spend/${GOAL_SET}-${LOCK_DIGEST#sha256:}.json"' in text
    assert 'args+=(--spend-ledger "$ledger")' in text
    assert 'artifacts/${{ matrix.target }}/spend-ledger.json' in text
    assert 'artifacts/${{ matrix.target }}/SPEND-CAP-REACHED' in text


def test_studio_audit_usage_reaches_the_ledger_priced_as_input_and_output():
    """Sol d7af7588 item 1: the Studio audit projection's prompt_tokens / completion_tokens survive the
    receipt projection (usage_record) and are priced, so a real run never records zero Bedrock spend."""
    import ai_arc_runner as R
    audit = [{'prompt_tokens': 1_000_000, 'completion_tokens': 100_000}, {'prompt_tokens': 500_000, 'completion_tokens': 0}]
    receipt = {'startedAt': '2026-11-02T09:00:00Z', 'finishedAt': '2026-11-02T09:00:00Z',
               'stages': [{'stage': 'studio', 'assistantUsage': R.usage_record(audit)}]}
    bedrock = spend(2500)['rates']['bedrock']
    cost = C.run_cost(receipt, 'local-docker', spend(2500))
    assert cost['bedrockTokens']['input'] == 1_500_000 and cost['bedrockTokens']['output'] == 100_000
    assert cost['bedrockUsd'] == round(1.5 * bedrock['inputPerMTok'] + 0.1 * bedrock['outputPerMTok'], 4) > 0


def test_a_reused_run_id_keeps_its_own_live_reservation(tmp_path):
    """Sol d7af7588 item 2: a redispatch reusing a schedule runId on the same ledger is not released by the
    earlier dispatch's completion; cap $50, spent $1, a second $25 launch beside a live one is refused."""
    path, doc = tmp_path / 'ledger.json', spend(cap=50, estimate=25)
    first = C.SpendLedger(path, doc)
    assert first.reserve('local-docker:A1:001') is None
    first.record('local-docker:A1:001', {'totalUsd': 1.0})
    again = C.SpendLedger(path, doc)               # a redispatch with the same schedule ids
    assert again.reserve('local-docker:A1:001') is None
    assert again.reserve('local-docker:A1:002') is not None    # 1 + 25 (live 001) + 25 > 50
    rows_ = json.loads(path.read_text())
    assert len({r['reservation'] for r in rows_ if r['kind'] == 'reserved'}) == 2


def test_a_spend_stop_drains_even_with_a_pending_adjudication(tmp_path):
    """Sol d7af7588 item 3: with the cap reached, an undetermined finisher's pending adjudication is recorded,
    not awaited; the campaign ends once running runs finish and reports the stop."""
    import ai_arc_runner as R
    arc = json.loads(json.dumps(R.load_arc()))
    arc['spendCap'] = spend(cap=50.5, estimate=25)        # two launches fit; the third would not
    out = tmp_path / 'c'
    statuses = {'local-docker:A1:001': 'undetermined'}

    def after_the_stop(row):          # both runs are still in flight when the third launch hits the cap
        deadline = time.monotonic() + 20
        while not (out / 'SPEND-CAP-REACHED').is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
    started = time.monotonic()
    results = C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(4), out=out, hosts=['h1'],
                             per_host=3, common=[], launch=fake_launch(out, statuses, [], gate=after_the_stop),
                             wait_poll=0.01, arc=arc, max_wait=60)
    assert time.monotonic() - started < 30
    assert sorted(r['runId'] for r in results) == ['local-docker:A1:001', 'local-docker:A1:002']
    assert (out / 'SPEND-CAP-REACHED').is_file()
    state = json.loads((out / 'campaign-state.json').read_text())
    assert state['spendCapStop'] and 'local-docker:A1:001' in state['pending']
    assert (out / 'PENDING-ADJUDICATION').is_file()


def test_a_spend_stop_drain_never_adjudicates(tmp_path, monkeypatch):
    """Sol 811796f3: an adjudication that would raise (void cap or reserve exhausted) is not attempted while a
    stopped campaign drains; the stop is still reported once the running runs finish."""
    import ai_arc_runner as R
    arc = json.loads(json.dumps(R.load_arc()))
    arc['spendCap'] = spend(cap=50.5, estimate=25)
    out = tmp_path / 'c'
    (out / 'adjudications').mkdir(parents=True)
    (out / 'adjudications' / 'local-docker-A1-001.json').write_text(json.dumps(GOOD))
    monkeypatch.setattr(R, 'check_adjudication', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('exhausted')))

    def after_the_stop(row):
        deadline = time.monotonic() + 20
        while not (out / 'SPEND-CAP-REACHED').is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        if row['runId'].endswith('002'):
            # 002 stays active while 001 (undetermined, with a decision on file) is pending (Sol 811796f3).
            first = out / 'runs' / 'local-docker-A1-001' / 'trial-receipt.json'
            while not first.is_file() and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.5)
    results = C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(4), out=out, hosts=['h1'],
                             per_host=3, common=[], launch=fake_launch(out, {'local-docker:A1:001': 'undetermined'}, [],
                                                                         gate=after_the_stop),
                             wait_poll=0.01, arc=arc, max_wait=60)
    assert len(results) == 2 and (out / 'SPEND-CAP-REACHED').is_file()
    assert 'local-docker:A1:001' in json.loads((out / 'campaign-state.json').read_text())['pending']


def gap_launch(out, log):
    """Run 001 ends undetermined with a safety coverage gap (ARC v2 change 6 / 8a); others complete clean."""
    def launch(row, slot):
        log.append((row, slot, time.monotonic()))
        path = Path(out) / 'runs' / row['runId'].replace(':', '-') / 'trial-receipt.json'
        path.parent.mkdir(parents=True)
        gap = row['runId'].endswith('001')
        path.write_text(json.dumps({'runId': row['runId'], 'status': 'undetermined' if gap else 'completed',
                                    'stages': [{'stage': 'setup', 'outcome': 'undetermined' if gap else 'pass',
                                                'safetyUnverified': ['unclassified-command@1'] if gap else []}],
                                    'startedAt': '2026-11-02T09:00:00Z', 'finishedAt': '2026-11-02T09:30:00Z'}))
        return 0
    return launch


def test_a_safety_gap_writes_a_packet_and_waits_for_a_cross_family_verdict(tmp_path):
    log = []
    (tmp_path / 'adjudications').mkdir(parents=True)
    with pytest.raises(TimeoutError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(2), out=tmp_path, hosts=['h1'], per_host=1,
                       common=[], wait_poll=0.01, max_wait=0.3, launch=gap_launch(tmp_path, log))
    assert [r['runId'] for r, _, _ in log] == ['local-docker:A1:001'], 'nothing else started'
    packet = json.loads((tmp_path / 'adjudications' / 'local-docker-A1-001.safety-gaps.packet.json').read_text())
    assert [p['gapId'] for p in packet] == ['setup:unclassified-command@1']
    # A barrier decision alone does not clear a gap run: every gap needs its own verdict.
    out2 = tmp_path / 'again'
    (out2 / 'adjudications').mkdir(parents=True)
    decision = {'finding': 'product-failure', 'reviewerFamily': 'openai',
                'adjudication': 'https://github.com/honua-io/honua-release/issues/7', 'adjudicatedAt': '2026-11-02T10:00:00Z'}
    (out2 / 'adjudications' / 'local-docker-A1-001.json').write_text(json.dumps(decision))
    with pytest.raises(TimeoutError):
        C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(2), out=out2, hosts=['h1'], per_host=1,
                       common=[], wait_poll=0.01, max_wait=0.3, launch=gap_launch(out2, []))
    out3 = tmp_path / 'third'
    (out3 / 'adjudications').mkdir(parents=True)
    (out3 / 'adjudications' / 'local-docker-A1-001.json').write_text(json.dumps(decision))
    (out3 / 'adjudications' / 'local-docker-A1-001.safety-gaps.json').write_text(json.dumps([
        {'runId': 'local-docker:A1:001', 'gapId': 'setup:unclassified-command@1', 'verdict': 'not-unsafe',
         'reviewerFamily': 'openai', 'adjudication': 'https://github.com/honua-io/honua-release/issues/8',
         'adjudicatedAt': '2026-11-02T10:00:00Z'}]))
    log3 = []
    C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(2), out=out3, hosts=['h1'], per_host=1,
                   common=[], wait_poll=0.01, launch=gap_launch(out3, log3))
    assert len(log3) == 2
    assert json.loads((out3 / 'safety-gap-adjudications.json').read_text())[0]['verdict'] == 'not-unsafe'

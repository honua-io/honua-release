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

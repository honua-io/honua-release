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
    return [{'runId': f'{prefix}{i:03d}', 'variant': 'AB'[i % 2], 'caseId': None} for i in range(1, n + 1)]


def fake_launch(out, statuses, log, gate=None):
    def launch(row, slot):
        log.append((row['runId'], slot, time.monotonic()))
        if gate:
            gate(row)
        path = Path(out) / 'runs' / row['runId'].replace(':', '-') / 'trial-receipt.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'runId': row['runId'], 'status': statuses.get(row['runId'], 'completed')}))
        return 0
    return launch


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
    assert len(results) == 8 and len({r for r, _, _ in log}) == 8
    assert peak[0] <= 6
    assert {slot for _, slot, _ in log} <= {(h, i) for h in ('h1', 'h2') for i in range(3)}


def test_undetermined_run_pauses_until_adjudication_and_consumes_a_reserve(tmp_path):
    log = []
    statuses = {'local-docker:A1:001': 'undetermined'}
    reserve = [{'runId': 'local-docker:A1:r01', 'variant': 'H', 'caseId': 'r01'}]

    written = []

    def adjudicate():
        time.sleep(0.3)
        written.append(time.monotonic())
        (tmp_path / 'adjudications' / 'local-docker-A1-001.json').write_text(json.dumps(
            {'finding': 'harness-fault', 'adjudication': 'https://example.invalid/a/1', 'reviewerFamily': 'openai',
             'adjudicatedAt': '2026-11-02T10:00:00Z'}))
    threading.Thread(target=adjudicate, daemon=True).start()
    started = time.monotonic()
    results = C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(3), out=tmp_path, hosts=['h1'],
                             per_host=1, common=[], reserve_rows=reserve, launch=fake_launch(tmp_path, statuses, log),
                             wait_poll=0.01)
    order = [r for r, _, _ in log]
    assert order[0] == 'local-docker:A1:001' and order[1] == 'local-docker:A1:r01', 'reserve runs next, after adjudication'
    assert written and log[1][2] >= written[0], 'no run started before the adjudication existed'
    voids = json.loads((tmp_path / 'voids.json').read_text())
    assert voids[0]['replacementRunId'] == 'local-docker:A1:r01' and len(results) == 4


def test_non_harness_finding_keeps_the_run_counted(tmp_path):
    log = []
    (tmp_path / 'adjudications').mkdir(parents=True)
    (tmp_path / 'adjudications' / 'local-docker-A1-001.json').write_text(json.dumps({'finding': 'product-failure'}))
    C.run_campaign(target='local-docker', arm='A1', schedule_rows=rows(2), out=tmp_path, hosts=['h1'], per_host=1,
                   common=[], reserve_rows=[{'runId': 'local-docker:A1:r01', 'variant': 'H', 'caseId': 'r01'}],
                   launch=fake_launch(tmp_path, {'local-docker:A1:001': 'undetermined'}, log), wait_poll=0.01)
    assert [r for r, _, _ in log] == ['local-docker:A1:001', 'local-docker:A1:002']
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
    assert '--project-name arc-abcdef123456' in teardown and 'rm -rf -- /tmp/honua-arc-arc-abcdef123456' in teardown
    with pytest.raises(ValueError):
        L.VmSlot(host='h;rm -rf /', user='ubuntu', identity_file='i', known_hosts='k', project='arc-abcdef123456', index=0)

#!/usr/bin/env python3
"""Run one target x arm arc campaign from the controller: parallel slots, first attempts only.

Local Docker: each run gets one VM slot (``ssh://<alias>`` plus slot index 0..2), so up to three
runs share a host, each with its own compose project, directory and ports. The agent CLIs run here.

Void policy (prereg): when a run ends ``undetermined`` (a suspected harness fault), no further run
starts until that run has an adjudication file ``<out>/adjudications/<run>.json`` written by the
reviewer of the other model family. A ``harness-fault`` finding consumes the next sealed reserve
case (at most two per target and arm); any other finding leaves the run counted as it stands. The
campaign never reruns a run id and never replaces a receipt.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_arc_runner as arc_runner  # noqa: E402


def slots(hosts: list[str], per_host: int) -> list[tuple[str, int]]:
    if not hosts or not 1 <= per_host <= 3:
        raise ValueError('at least one host and 1..3 slots per host')
    return [(host, index) for index in range(per_host) for host in hosts]


def run_campaign(*, target, arm, schedule_rows, out: Path, hosts, per_host, common: list[str],
                 reserve_rows=(), launch=None, wait_poll=30.0, max_wait=None) -> list[dict]:
    """Run every scheduled row once; returns [{runId, status, receipt}]. ``launch(row, slot) -> int``."""
    out = Path(out)
    (out / 'adjudications').mkdir(parents=True, exist_ok=True)
    free = slots(hosts, per_host) if target == 'local-docker' else [(None, 0)] * per_host
    lock = threading.Condition()
    results, pending_adjudication = [], []
    queue, reserves = list(schedule_rows), list(reserve_rows)
    voids, threads = [], []

    def default_launch(row, slot):
        host, index = slot
        command = [sys.executable, str(ROOT / 'tools/ai_arc_runner.py'), 'trial', '--target', target, '--arm', arm,
                   '--run-id', row['runId'], '--variant', row['variant'], *common,
                   '--output', str(out / 'runs' / row['runId'].replace(':', '-'))]
        if row.get('caseId'):
            command += ['--case-id', row['caseId']]
        if host:
            command += ['--docker-host', 'ssh://' + host, '--slot-index', str(index)]
        return subprocess.run(command, timeout=5 * 3600).returncode
    launch = launch or default_launch

    def worker(row, slot):
        launch(row, slot)
        path = out / 'runs' / row['runId'].replace(':', '-') / 'trial-receipt.json'
        receipt = json.loads(path.read_text()) if path.is_file() else None
        with lock:
            results.append({'runId': row['runId'], 'status': (receipt or {}).get('status', 'missing'),
                            'receipt': str(path) if receipt else None})
            if receipt is None or receipt['status'] == 'undetermined':
                pending_adjudication.append(row['runId'])
            free.append(slot)
            lock.notify_all()

    def adjudicate(run_id, waited):
        path = out / 'adjudications' / (run_id.replace(':', '-') + '.json')
        if not path.is_file():
            return False
        decision = json.loads(path.read_text())
        if decision.get('finding') == 'harness-fault':
            if len(voids) >= 2 or not reserves:
                raise RuntimeError('void cap or sealed reserve exhausted: the target fails')
            reserve = reserves.pop(0)
            voids.append({'runId': run_id, 'adjudication': decision['adjudication'],
                          'reviewerFamily': decision['reviewerFamily'], 'finding': 'harness-fault',
                          'adjudicatedAt': decision['adjudicatedAt'], 'replacementRunId': reserve['runId']})
            queue.insert(0, reserve)
        return True

    started = time.monotonic()
    with lock:
        while queue or len(results) < len([t for t in threads]):
            while pending_adjudication:
                if adjudicate(pending_adjudication[0], time.monotonic() - started):
                    pending_adjudication.pop(0)
                    continue
                (out / 'PENDING-ADJUDICATION').write_text('\n'.join(pending_adjudication) + '\n')
                if max_wait is not None and time.monotonic() - started > max_wait:
                    raise TimeoutError('runs await adjudication: ' + ', '.join(pending_adjudication))
                lock.wait(wait_poll)
            if not queue:
                lock.wait(wait_poll)
                continue
            if not free:
                lock.wait(wait_poll)
                continue
            row, slot = queue.pop(0), free.pop(0)
            thread = threading.Thread(target=worker, args=(row, slot), daemon=True)
            threads.append(thread)
            thread.start()
    for thread in threads:
        thread.join()
    (out / 'PENDING-ADJUDICATION').unlink(missing_ok=True)
    (out / 'voids.json').write_text(json.dumps(voids, indent=2) + '\n')
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--target', choices=arc_runner.TARGETS, required=True)
    parser.add_argument('--arm', required=True)
    parser.add_argument('--hosts', default='', help='comma-separated VM host aliases (local-docker)')
    parser.add_argument('--per-host', type=int, default=3)
    parser.add_argument('--trials', type=int)
    parser.add_argument('--goal-set')
    parser.add_argument('--reserve-set')
    parser.add_argument('--lock-path', required=True)
    parser.add_argument('--lock-digest', required=True)
    parser.add_argument('--quickstart')
    parser.add_argument('--quickstart-revision')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error('output directory exists; every previous campaign is preserved')
    arc = arc_runner.load_arc()
    arm = arc_runner.arm_by_id(arc, args.arm)
    goal_set = arc_runner.load_goal_set(args.goal_set)
    rows = arc_runner.schedule(arc, args.target, arm, goal_set, args.trials)
    reserve = arc_runner.load_goal_set(args.reserve_set, expect_set='reserve') if args.reserve_set else None
    reserve_rows = ([{'runId': f'{args.target}:{arm["id"]}:{c["caseId"]}', 'variant': 'H', 'caseId': c['caseId']}
                     for c in reserve['cases'].values() if c['target'] == args.target] if reserve else [])
    args.out.mkdir(parents=True)
    (args.out / 'schedule.json').write_text(json.dumps(rows, indent=2) + '\n')
    common = ['--lock-path', args.lock_path, '--lock-digest', args.lock_digest]
    for flag, value in (('--quickstart', args.quickstart), ('--quickstart-revision', args.quickstart_revision),
                        ('--goal-set', args.goal_set)):
        if value:
            common += [flag, value]
    results = run_campaign(target=args.target, arm=args.arm, schedule_rows=rows, out=args.out,
                           hosts=[h for h in args.hosts.split(',') if h], per_host=args.per_host, common=common,
                           reserve_rows=reserve_rows)
    print(json.dumps({'runs': len(results), 'statuses': sorted(r['status'] for r in results)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

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
                 reserve_rows=(), launch=None, wait_poll=30.0, max_wait=None, provisioner=None, arc=None) -> list[dict]:
    """Run every scheduled row once; returns [{runId, status, receipt}]. ``launch(row, slot) -> int``.

    After an undetermined (or missing) run nothing new starts until that run has an adjudication that
    passes the same checks the aggregate applies. A harness-fault finding consumes the next sealed
    reserve case (routed to the reserve manifest); a third one stops the campaign: the target fails.
    The campaign state (pending adjudications, voids) is written on every exit."""
    out = Path(out)
    arc = arc or arc_runner.load_arc()
    arm_spec = arc_runner.arm_by_id(arc, arm)
    (out / 'adjudications').mkdir(parents=True, exist_ok=True)
    free = slots(hosts, per_host) if target == 'local-docker' else [(None, i) for i in range(per_host)]
    lock = threading.Condition()
    results, pending, voids, threads, rejected = [], [], [], [], {}
    queue, reserves = list(schedule_rows), list(reserve_rows)

    def run_dir(row):
        return out / 'runs' / row['runId'].replace(':', '-')

    def default_launch(row, slot):
        host, index = slot
        command = [sys.executable, str(ROOT / 'tools/ai_arc_runner.py'), 'trial', '--target', target, '--arm', arm,
                   '--run-id', row['runId'], '--variant', row['variant'], *common, '--output', str(run_dir(row)),
                   '--evidence-dir', str(Path(str(out) + '-evidence') / row['runId'].replace(':', '-')),
                   '--evidence-artifact', f"ai-arc-evidence-{arm}-{target}"]
        if row.get('caseId'):
            command += ['--case-id', row['caseId'], '--goal-set', row['set']]
        if host:
            command += ['--docker-host', 'ssh://' + host, '--slot-index', str(index)]
        if provisioner and target != 'local-docker':
            # The release cell machinery reserves an empty slot (never this script): it prints the slot
            # receipt, the operator session file and the deploy identity contract export paths.
            got = subprocess.run([provisioner, 'acquire', target, row['runId']], capture_output=True, text=True,
                                 timeout=3600, check=True)
            slot_doc = json.loads(got.stdout)
            for flag, key in (('--slot-receipt', 'slotReceipt'), ('--slot-credentials', 'slotCredentials'),
                              ('--deploy-identity-contract', 'deployIdentityContract'),
                              ('--bootstrap-outputs', 'bootstrapOutputs'), ('--operator-binary', 'operatorBinary')):
                if slot_doc.get(key):
                    command += [flag, slot_doc[key]]
        try:
            return subprocess.run(command, timeout=5 * 3600).returncode
        finally:
            if provisioner and target != 'local-docker':
                subprocess.run([provisioner, 'release', target, row['runId']], timeout=3600, check=False)
    launch = launch or default_launch

    def save_state():
        (out / 'voids.json').write_text(json.dumps(voids, indent=2) + '\n')
        (out / 'campaign-state.json').write_text(json.dumps({'pending': pending, 'voids': voids,
                                                             'rejectedAdjudications': rejected,
                                                             'reservesLeft': [r['runId'] for r in reserves],
                                                             'finished': [r['runId'] for r in results]}, indent=2) + '\n')
        if pending:
            (out / 'PENDING-ADJUDICATION').write_text('\n'.join(pending) + '\n')
        else:
            (out / 'PENDING-ADJUDICATION').unlink(missing_ok=True)

    def worker(row, slot):
        try:
            launch(row, slot)
        finally:
            path = run_dir(row) / 'trial-receipt.json'
            receipt = json.loads(path.read_text()) if path.is_file() else None
            with lock:
                results.append({'runId': row['runId'], 'status': (receipt or {}).get('status', 'missing'),
                                'receipt': str(path) if receipt else None})
                if receipt is None or receipt['status'] == 'undetermined':
                    pending.append(row['runId'])
                free.append(slot)
                save_state()
                lock.notify_all()

    def adjudicate(run_id):
        path = out / 'adjudications' / (run_id.replace(':', '-') + '.json')
        if not path.is_file():
            return False
        try:
            decision = json.loads(path.read_text())
        except ValueError:
            return False      # still being written; read it on the next pass
        if decision.get('finding') != 'harness-fault':
            return True       # the run stays counted as it stands
        receipt_path = out / 'runs' / run_id.replace(':', '-') / 'trial-receipt.json'
        original = json.loads(receipt_path.read_text()) if receipt_path.is_file() else None
        try:
            arc_runner.check_adjudication(arc, arm_spec, decision, original)
        except ValueError as exc:
            rejected[run_id] = str(exc)
            return False      # an invalid adjudication never resumes the campaign
        if len(voids) >= arc_runner.MAX_VOIDS or not reserves:
            raise RuntimeError('void cap or sealed reserve exhausted: the target fails')
        reserve = reserves.pop(0)
        voids.append({'runId': run_id, 'adjudication': decision['adjudication'],
                      'reviewerFamily': decision['reviewerFamily'], 'finding': 'harness-fault',
                      'adjudicatedAt': decision['adjudicatedAt'], 'replacementRunId': reserve['runId']})
        queue.insert(0, reserve)
        return True

    started = time.monotonic()
    try:
        with lock:
            while queue or pending or len(results) < len(threads):
                if pending:
                    if adjudicate(pending[0]):
                        pending.pop(0)
                        save_state()
                        continue
                    save_state()
                    if max_wait is not None and time.monotonic() - started > max_wait:
                        raise TimeoutError('runs await a valid adjudication: ' + ', '.join(pending))
                    lock.wait(wait_poll)
                    continue
                if not queue or not free:
                    lock.wait(wait_poll)
                    continue
                row, slot = queue.pop(0), free.pop(0)
                thread = threading.Thread(target=worker, args=(row, slot), daemon=True)
                threads.append(thread)
                thread.start()
        for thread in threads:
            thread.join()
    finally:
        with lock:
            save_state()
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--target', choices=arc_runner.TARGETS, required=True)
    parser.add_argument('--arm', required=True)
    parser.add_argument('--hosts', default='', help='comma-separated VM host aliases (local-docker)')
    parser.add_argument('--per-host', type=int, default=3)
    parser.add_argument('--trials', type=int)
    parser.add_argument('--held-out', action='store_true', help='sealed primary and reserve manifests (key in env)')
    parser.add_argument('--lock-path', required=True)
    parser.add_argument('--lock-digest', required=True)
    parser.add_argument('--quickstart')
    parser.add_argument('--quickstart-revision')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--slot-provisioner', help='AWS targets: executable with acquire/release <target> <run>')
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error('output directory exists; every previous campaign is preserved')
    arc = arc_runner.load_arc()
    arm = arc_runner.arm_by_id(arc, args.arm)
    primary = arc_runner.load_goal_set('primary') if args.held_out else None
    reserve = arc_runner.load_goal_set('reserve') if args.held_out else None
    rows = arc_runner.schedule(arc, args.target, arm, primary, args.trials)
    reserve_rows = arc_runner.reserve_rows(args.target, arm, reserve)
    args.out.mkdir(parents=True)
    (args.out / 'schedule.json').write_text(json.dumps(rows, indent=2) + '\n')
    common = ['--lock-path', args.lock_path, '--lock-digest', args.lock_digest]
    for flag, value in (('--quickstart', args.quickstart), ('--quickstart-revision', args.quickstart_revision)):
        if value:
            common += [flag, value]
    results = run_campaign(target=args.target, arm=args.arm, schedule_rows=rows, out=args.out,
                           hosts=[h for h in args.hosts.split(',') if h], per_host=args.per_host, common=common,
                           reserve_rows=reserve_rows, provisioner=args.slot_provisioner, arc=arc)
    print(json.dumps({'runs': len(results), 'statuses': sorted(r['status'] for r in results)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

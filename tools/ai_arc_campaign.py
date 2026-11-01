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
import hashlib
import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_arc_runner as arc_runner  # noqa: E402



def suspected_harness_fault(receipt: dict) -> bool:
    """A receipt the harness itself marks as possibly its own fault: an undetermined stage, a harness-error or
    harness-fault cause, a harnessFault assertion, or failed slot preparation."""
    for row in receipt.get('stages') or []:
        cause = str(row.get('cause') or '').lower()
        if row.get('outcome') == 'undetermined' or cause.startswith('harness') or 'harness fault' in cause:
            return True
        if any(a.get('harnessFault') for a in row.get('assertions') or [] if isinstance(a, dict)):
            return True
    prep = (receipt.get('slotFacts') or {}).get('slotPreparation') or {}
    return isinstance(prep, dict) and prep.get('status') not in (None, 'skipped', 'converged')

def _hours(receipt: dict) -> float | None:
    from datetime import datetime
    try:
        a, b = (datetime.fromisoformat(str(receipt[k]).replace('Z', '+00:00')) for k in ('startedAt', 'finishedAt'))
        return max(0.0, (b - a).total_seconds() / 3600)
    except (KeyError, ValueError, TypeError):
        return None


# Every ledger row says which figures are measured against a published price and which are estimates.
RATE_BASIS = {'bedrock': 'AWS public price list (Regional CRIS on-demand, us-east-1)',
              'compute': 'estimate (VM and AWS cell hourly rates are not verified against the price list)',
              'cliSubscription': 'unknown (flat subscription; never priced)'}


def run_cost(receipt: dict | None, target: str, spend: dict) -> dict:
    """One run's spend (Astra S4): Bedrock tokens from the receipt (the Studio built-in assistant, priced at the
    us-east-1 rate), the VM or AWS cell estimate for the run's wall clock, and the CLI subscription recorded as
    unknown (flat subscription; never priced). A run without a readable receipt is charged the per-run estimate."""
    rates = spend['rates']
    if not isinstance(receipt, dict):
        return {'bedrockUsd': None, 'computeUsd': None, 'cliSubscriptionUsd': 'unknown',
                'totalUsd': float(spend['perRunEstimateUsd']), 'basis': 'estimate (no receipt)',
                'rateBasis': RATE_BASIS}
    tokens = {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}
    # The Studio audit projection reports prompt_tokens / completion_tokens (agent_studio_session_audit);
    # Sol d7af7588 item 1: they price as input and output.
    keys = {'input': ('inputTokens', 'input_tokens', 'prompt_tokens', 'promptTokens'),
            'output': ('outputTokens', 'output_tokens', 'completion_tokens', 'completionTokens'),
            'cacheRead': ('cacheReadInputTokens', 'cache_read_input_tokens'),
            'cacheWrite': ('cacheWriteInputTokens', 'cacheCreationInputTokens', 'cache_creation_input_tokens')}
    for stage in receipt.get('stages') or []:
        usage = stage.get('assistantUsage') if isinstance(stage, dict) else None
        for row in (usage if isinstance(usage, list) else [usage] if isinstance(usage, dict) else []):
            for kind, names in keys.items():
                tokens[kind] += sum(row.get(n, 0) for n in names if isinstance(row.get(n), (int, float)))
    bedrock = rates['bedrock']
    bedrock_usd = sum(tokens[k] * bedrock[k + 'PerMTok'] for k in tokens) / 1e6
    hours = _hours(receipt)
    hourly = rates['vmHourUsd'] if target == 'local-docker' else rates['awsCellHourUsd']
    compute = hours * hourly if hours is not None else float(spend['perRunEstimateUsd'])
    return {'bedrockUsd': round(bedrock_usd, 4), 'bedrockTokens': tokens, 'computeUsd': round(compute, 4),
            'computeHours': None if hours is None else round(hours, 3), 'cliSubscriptionUsd': 'unknown',
            'totalUsd': round(bedrock_usd + compute, 4),
            'basis': 'receipt' if hours is not None else 'receipt (no wall clock: compute is the per-run estimate)',
            'rateBasis': RATE_BASIS}


class SpendLedger:
    """The campaign spend ledger against the frozen cap, shared by every campaign that names the same file (one
    lock per append). Scheduling stops before a launch whose estimate would take the total over the cap."""

    def __init__(self, path: Path, spend: dict):
        self.path, self.spend = Path(path), spend
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tokens = {}       # runId -> this campaign's live reservation (Sol d7af7588 item 2)

    def _locked(self, fn):
        import fcntl
        with open(self.path.with_suffix('.lock'), 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                rows = json.loads(self.path.read_text()) if self.path.is_file() else []
                result = fn(rows)
                tmp = self.path.with_suffix('.tmp')
                tmp.write_text(json.dumps(rows, indent=1) + '\n')
                tmp.replace(self.path)
                return result
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def reserve(self, run_id: str) -> dict | None:
        """None when the launch fits under the cap (recorded as reserved), else the stop report."""
        def check(rows):
            spent = sum(r['totalUsd'] for r in rows if r.get('kind') == 'run')
            # A completion releases only the one reservation it names. A redispatch that reuses a schedule
            # runId keeps its own live reservation (Sol d7af7588 item 2).
            closed = {x.get('reservation') for x in rows if x.get('kind') == 'run' and x.get('reservation')}
            open_ = sum(1 for r in rows if r.get('kind') == 'reserved' and r.get('reservation') not in closed)
            observed = [r['totalUsd'] for r in rows if r.get('kind') == 'run']
            estimate = max([float(self.spend['perRunEstimateUsd'])] + observed)
            projected = spent + estimate * (open_ + 1)
            if projected > float(self.spend['capUsd']):
                return {'stopped': True, 'runId': run_id, 'spentUsd': round(spent, 2), 'openRuns': open_,
                        'perRunEstimateUsd': round(estimate, 2), 'projectedUsd': round(projected, 2),
                        'capUsd': self.spend['capUsd'], 'cliSubscriptionUsd': 'unknown'}
            token = uuid.uuid4().hex
            self.tokens[run_id] = token
            rows.append({'kind': 'reserved', 'runId': run_id, 'reservation': token, 'estimateUsd': round(estimate, 4)})
            return None
        return self._locked(check)

    def record(self, run_id: str, cost: dict):
        token = self.tokens.pop(run_id, None)
        self._locked(lambda rows: rows.append({'kind': 'run', 'runId': run_id, 'reservation': token, **cost}))


def slots(hosts: list[str], per_host: int) -> list[tuple[str, int]]:
    if not hosts or not 1 <= per_host <= 3:
        raise ValueError('at least one host and 1..3 slots per host')
    return [(host, index) for index in range(per_host) for host in hosts]


def run_campaign(*, target, arm, schedule_rows, out: Path, hosts, per_host, common: list[str],
                 reserve_rows=(), launch=None, wait_poll=30.0, max_wait=None, provisioner=None, arc=None,
                 ledger: Path | None = None) -> list[dict]:
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
    adjudicated = {}
    queue, reserves = list(schedule_rows), list(reserve_rows)
    spend = SpendLedger(ledger or (out / 'spend-ledger.json'), arc['spendCap'])
    stopped = {}

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
                                                             'finished': [r['runId'] for r in results],
                                                             'spendCapStop': stopped or None}, indent=2) + '\n')
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
            spend.record(row['runId'], run_cost(receipt, target, arc['spendCap']))
            with lock:
                results.append({'runId': row['runId'], 'status': (receipt or {}).get('status', 'missing'),
                                'receipt': str(path) if receipt else None})
                if receipt is None or receipt['status'] == 'undetermined' or suspected_harness_fault(receipt) \
                        or (out / 'holds' / (row['runId'].replace(':', '-') + '.hold')).is_file():
                    # Adjudication barrier (Astra S3): any suspected harness defect stops the campaign before the
                    # next launch, whatever the receipt's status, so a void stays eligible under the prereg.
                    pending.append(row['runId'])
                free.append(slot)
                save_state()
                lock.notify_all()

    def hold_generation(run_id):
        hold = out / 'holds' / (run_id.replace(':', '-') + '.hold')
        try:
            return hashlib.sha256(hold.read_bytes()).hexdigest() + ':' + str(hold.stat().st_mtime_ns)
        except OSError:
            return None

    def adjudicate(run_id):
        """(cleared, hold generation it answered). The generation is captured before validation; a hold
        replaced meanwhile leaves the run pending for a fresh decision (Sol 96b2546f)."""
        generation = hold_generation(run_id)
        return _adjudicate(run_id, generation), generation

    def _adjudicate(run_id, generation):
        path = out / 'adjudications' / (run_id.replace(':', '-') + '.json')
        if not path.is_file():
            return False
        try:
            decision = json.loads(path.read_text())
        except ValueError:
            return False      # still being written; read it on the next pass
        hold = out / 'holds' / (run_id.replace(':', '-') + '.hold')
        if hold.is_file() and path.stat().st_mtime_ns < hold.stat().st_mtime_ns:
            return False      # a decision older than the current hold does not answer it
        receipt_path = out / 'runs' / run_id.replace(':', '-') / 'trial-receipt.json'
        original = json.loads(receipt_path.read_text()) if receipt_path.is_file() else None
        try:
            # Every decision is validated, including one that keeps the run counted (Sol 4ac23739).
            arc_runner.check_adjudication(arc, arm_spec, decision, original,
                                          voiding=decision.get('finding') == 'harness-fault')
        except ValueError as exc:
            rejected[run_id] = str(exc)
            return False      # an invalid adjudication never resumes the campaign
        if hold_generation(run_id) != generation:
            return False      # the hold changed during validation: nothing is committed (Sol e265bb02)
        if decision.get('finding') != 'harness-fault':
            return True       # the run stays counted as it stands
        if len(voids) >= arc_runner.MAX_VOIDS or not reserves:
            raise RuntimeError('void cap or sealed reserve exhausted: the target fails')
        reserve = reserves.pop(0)
        voids.append({'runId': run_id, 'adjudication': decision['adjudication'],
                      'reviewerFamily': decision['reviewerFamily'], 'finding': 'harness-fault',
                      'adjudicatedAt': decision['adjudicatedAt'], 'replacementRunId': reserve['runId']})
        queue.insert(0, reserve)
        return True

    def scan_holds():
        for hold in sorted((out / 'holds').glob('*.hold')) if (out / 'holds').is_dir() else []:
            # A hold placed on any run, finished or in flight, is a suspected defect: nothing new starts
            # until it is adjudicated (Sol 09e293be). A hold for a run not yet started blocks it too.
            held = next((r['runId'] for r in results if r['runId'].replace(':', '-') + '.hold' == hold.name),
                        None)
            generation = hashlib.sha256(hold.read_bytes()).hexdigest() + ':' + str(hold.stat().st_mtime_ns)
            # An acknowledgement answers one hold generation; a fresh hold needs a fresh decision
            # (Sol 4ac23739 item 2).
            if held and held not in pending and adjudicated.get(held) != generation:
                pending.append(held)

    started = time.monotonic()
    try:
        with lock:
            while queue or pending or len(results) < len(threads):
                if stopped and len(results) >= len(threads):
                    # Sol d7af7588 item 3: a spend stop drains independently of the launch barriers. Once every
                    # running run has finished, the campaign ends (exit 3). Pending adjudications, including
                    # holds placed on finished runs, are recorded in the state, not awaited.
                    scan_holds()
                    save_state()
                    break
                if pending:
                    cleared, generation = adjudicate(pending[0])
                    if cleared:
                        adjudicated[pending.pop(0)] = generation
                        save_state()
                        continue
                    save_state()
                    if max_wait is not None and time.monotonic() - started > max_wait:
                        raise TimeoutError('runs await a valid adjudication: ' + ', '.join(pending))
                    lock.wait(wait_poll)
                    continue
                scan_holds()
                if pending:
                    continue
                if any(h.name.endswith('.hold') for h in ((out / 'holds').iterdir() if (out / 'holds').is_dir() else [])
                       if not any(r['runId'].replace(':', '-') + '.hold' == h.name for r in results)):
                    if max_wait is not None and time.monotonic() - started > max_wait:
                        raise TimeoutError('a run is on hold before it finished')
                    lock.wait(wait_poll)
                    continue
                if (out / 'HOLD').is_file():
                    # Operator hold (a suspected scorer or harness defect noticed outside a receipt): no new run
                    # starts until the file is removed; running ones finish.
                    if max_wait is not None and time.monotonic() - started > max_wait:
                        raise TimeoutError('the campaign is on operator hold')
                    lock.wait(wait_poll)
                    continue
                if not queue or not free:
                    lock.wait(wait_poll)
                    continue
                if stopped:
                    lock.wait(wait_poll)
                    continue
                report = spend.reserve(queue[0]["runId"])
                if report is not None:
                    # Astra S4: the next launch would exceed the frozen spend cap. Nothing new starts; running
                    # runs finish; the stop and every unstarted row are reported.
                    stopped.update(report, unstarted=[r['runId'] for r in queue])
                    (out / 'SPEND-CAP-REACHED').write_text(json.dumps(stopped, indent=2) + '\n')
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
    parser.add_argument('--spend-ledger', type=Path, help='shared spend ledger (one per gate campaign; Astra S4)')
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
                           reserve_rows=reserve_rows, provisioner=args.slot_provisioner, arc=arc,
                           ledger=args.spend_ledger)
    stop = args.out / 'SPEND-CAP-REACHED'
    print(json.dumps({'runs': len(results), 'statuses': sorted(r['status'] for r in results),
                      'spendCapReached': json.loads(stop.read_text()) if stop.is_file() else None}))
    return 3 if stop.is_file() else 0


if __name__ == '__main__':
    raise SystemExit(main())

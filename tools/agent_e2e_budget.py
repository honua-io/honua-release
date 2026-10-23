#!/usr/bin/env python3
"""Reserve candidate-wide agent testing liability before starting paid work.

This ledger belongs to the trusted controller, never the agent sandbox. The cloud
controller must supply an upper bound covering resource quotas, maximum lifetime
and teardown; a retrospective billing estimate is not a reservation. Persist the
ledger across campaigns for the same candidate. A CI concurrency lock must cover
download/update/upload of a remote ledger as well as this local file lock.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import time


class BudgetError(ValueError):
    pass


FOSS4G_POLICY = 'foss4g-ai-2026-owner-2000-200'


def history_digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                               allow_nan=False).encode()).hexdigest()


def validate_amendment(ledger):
    amendment = ledger['policyAmendment']
    if (not isinstance(amendment, dict) or set(amendment) != {'policyId', 'priorLedger', 'priorLedgerDigest'}
            or amendment['policyId'] != FOSS4G_POLICY):
        raise BudgetError('unknown or malformed owner budget amendment')
    prior = amendment['priorLedger']
    if (not isinstance(prior, dict) or type(prior.get('schemaVersion')) is not int
            or prior['schemaVersion'] not in (1, 2) or prior.get('candidateKey') != ledger['candidateKey']
            or history_digest(prior) != amendment['priorLedgerDigest']):
        raise BudgetError('amendment history/candidate binding changed')
    # A larger ceiling must never erase a historical unreserved charge.
    validate(prior)
    for field in ('entries', 'operations'):
        old_rows, new_rows = prior.get(field, []), ledger[field]
        if not isinstance(new_rows, list) or len(new_rows) < len(old_rows):
            raise BudgetError('amendment history cannot be removed')
        for before, after in zip(old_rows, new_rows):
            if not isinstance(after, dict):
                raise BudgetError('invalid amended history row')
            expected = copy.deepcopy(before)
            if before['state'] == 'reserved' and after.get('state') == 'settled':
                expected.update(state='settled', actualUsd=after.get('actualUsd'))
            if after != expected:
                raise BudgetError('amendment history cannot be reset or rewritten')


OPERATION_PURPOSES = {'bootstrap', 'shared-environment', 'semantics', 'telemetry', 'retention'}



def validate_admissions(ledger):
    admissions = ledger.get('paidAdmissions', [])
    if not isinstance(admissions, list):
        raise BudgetError('paid admissions must be retained as a list')
    seen, contexts, active = set(), set(), 0
    for row in admissions:
        if (not isinstance(row, dict) or set(row) != {'id', 'campaignId', 'caseId', 'attempt', 'campaignMode',
                'ownerToken', 'createdEpoch', 'expiresEpoch', 'state', 'reservationIds', 'cleanupStatus'}
                or not isinstance(row['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', row['id'])
                or row['id'] in seen or not isinstance(row['ownerToken'], str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{20,128}', row['ownerToken'])
                or any(not isinstance(row[k], str) or not 0 < len(row[k]) <= 256 for k in ('campaignId', 'caseId'))
                or type(row['attempt']) is not int or row['attempt'] not in (1, 2)
                or row['campaignMode'] not in ('development', 'qualification')
                or type(row['createdEpoch']) is not int or row['createdEpoch'] <= 0
                or type(row['expiresEpoch']) is not int or not row['createdEpoch'] < row['expiresEpoch'] <= row['createdEpoch'] + 6000
                or row['state'] not in ('active', 'released')):
            raise BudgetError('invalid paid attempt admission')
        seen.add(row['id'])
        context = (row['campaignId'], row['caseId'], row['attempt'])
        if context in contexts:
            raise BudgetError('paid attempt context cannot be readmitted')
        contexts.add(context)
        if row['state'] == 'active':
            active += 1
            if row['reservationIds'] is not None or row['cleanupStatus'] is not None:
                raise BudgetError('active admission cannot claim release accounting')
        else:
            if row['cleanupStatus'] != 'pass': raise BudgetError('release requires successful trusted cleanup')
            validate_release_accounting(ledger, row, row['reservationIds'])
    # Expiry is NOT proof of a drained worker: stale admissions occupy capacity.
    if active > 4:
        raise BudgetError('four concurrent paid attempts already admitted')


def validate_release_accounting(ledger, admission, reservations):
    same_attempt = [row for row in ledger['entries']
                    if all(row.get(key) == admission[key] for key in ('campaignId', 'caseId', 'attempt'))
                    and row.get('campaignMode', 'qualification') == admission['campaignMode']]
    if any(row['state'] != 'settled' for row in same_attempt):
        raise BudgetError('all paid attempt liabilities, including model calls, must settle before release')
    if (not isinstance(reservations, dict) or set(reservations) != {'infrastructure', 'cleanup'}
            or len(set(reservations.values())) != 2):
        raise BudgetError('release requires exact settled infrastructure and cleanup reservations')
    for kind, rid in reservations.items():
        rows = [r for r in ledger['entries'] if r['id'] == rid]
        if (len(rows) != 1 or rows[0]['kind'] != kind or rows[0]['state'] != 'settled'
                or any(rows[0][k] != admission[k] for k in ('campaignId', 'caseId', 'attempt'))
                or rows[0].get('campaignMode', 'qualification') != admission['campaignMode']):
            raise BudgetError('release accounting does not bind the admitted attempt')


def update_admission(ledger, operation, values):
    if ledger['schemaVersion'] != 3:
        raise BudgetError('paid admission requires the named FOSS4G owner amendment')
    now = values.get('now_epoch', int(time.time()))
    if type(now) is not int or now <= 0:
        raise BudgetError('invalid controller observation time')
    rows = ledger.setdefault('paidAdmissions', [])
    if operation == 'acquire-paid-attempt':
        expiry = values['expires_epoch']
        if type(expiry) is not int or not now < expiry <= now + 6000:
            raise BudgetError('paid attempt admission requires a bounded unexpired lifetime')
        rows.append({'id': values['admission_id'], 'campaignId': values['campaign_id'],
                     'caseId': values['case_id'], 'attempt': values['attempt'], 'campaignMode': values.get('campaign_mode', 'qualification'),
                     'ownerToken': values['owner_token'], 'createdEpoch': now, 'expiresEpoch': expiry,
                     'state': 'active', 'reservationIds': None, 'cleanupStatus': None})
        validate_admissions(ledger)
    else:
        matches = [row for row in rows if row['id'] == values['admission_id']]
        if (len(matches) != 1 or matches[0]['ownerToken'] != values['owner_token']
                or matches[0]['state'] != 'active'):
            raise BudgetError('paid admission is missing, released or owned by another controller')
        row = matches[0]
        if operation == 'check-paid-attempt':
            if not row['createdEpoch'] <= now < row['expiresEpoch']:
                raise BudgetError('stale paid admission requires drain/accounting reconciliation')
        else:
            # Releasing an expired attempt is allowed only after exact final
            # accounting; neither time nor a lost controller lease frees a slot.
            if values.get('cleanup_status') != 'pass':
                raise BudgetError('cleanup failure keeps paid admission occupied for reconciliation')
            validate_release_accounting(ledger, row, values['reservation_ids'])
            row.update(state='released', reservationIds=copy.deepcopy(values['reservation_ids']), cleanupStatus='pass')

def money(value):
    if isinstance(value, bool):
        raise BudgetError("boolean is not a cost")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise BudgetError("invalid cost") from exc
    if not result.is_finite() or result < 0:
        raise BudgetError("cost must be finite and nonnegative")
    return result


def validate(ledger, *, allow_overrun=False):
    if not isinstance(ledger, dict):
        raise BudgetError('invalid candidate ledger')
    if (type(ledger.get("schemaVersion")) is not int or ledger['schemaVersion'] not in (1, 2, 3)
            or not isinstance(ledger.get('candidateKey'), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", ledger['candidateKey'])):
        raise BudgetError("invalid candidate ledger")
    fields = {'schemaVersion', 'candidateKey', 'ceilingUsd', 'cleanupReserveUsd', 'entries'}
    extra = {'operations', 'policyAmendment'} | ({'paidAdmissions'} if 'paidAdmissions' in ledger else set()) if ledger['schemaVersion'] == 3 else {'operations'} if ledger['schemaVersion'] == 2 else set()
    if set(ledger) != fields | extra:
        raise BudgetError('unexpected or missing candidate ledger fields')
    ceiling, reserve = money(ledger["ceilingUsd"]), money(ledger["cleanupReserveUsd"])
    if ledger['schemaVersion'] == 3:
        if ceiling != 2000 or reserve != 200:
            raise BudgetError('FOSS4G owner amendment requires exactly $2000/$200')
        validate_amendment(ledger)
    elif ceiling != 500 or reserve != 50:
        raise BudgetError("agent pack requires a $500 ceiling and $50 cleanup reserve")
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        raise BudgetError("entries must be a list")
    operations = ledger.get('operations', [])
    if not isinstance(operations, list):
        raise BudgetError('candidate operations require ledger version 2')
    ids, liability, operating = set(), Decimal(0), Decimal(0)
    purposes = {}
    for scope, row in [('attempt', r) for r in entries] + [('operation', r) for r in operations]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row['id'].strip() or row["id"] in ids:
            raise BudgetError("duplicate or missing reservation id")
        ids.add(row["id"])
        if row.get("kind") not in {"model", "infrastructure", "cleanup"} or row.get("state") not in {"reserved", "settled"}:
            raise BudgetError("invalid reservation")
        if scope == 'attempt':
            if not row.get("campaignId") or not row.get("caseId") or {'operationId', 'purpose'} & set(row):
                raise BudgetError("reservation lacks campaign/case or mixes operation scope")
            if type(row.get("attempt")) is not int or row["attempt"] not in (1, 2):
                raise BudgetError("reservation must identify attempt 1 or 2")
            if row.get("campaignMode", "qualification") not in ("development", "qualification"):
                raise BudgetError("unknown campaign mode")
        else:
            if (set(row) != {'id', 'operationId', 'purpose', 'kind', 'amountUsd', 'basisDigest', 'state', 'actualUsd'}
                    or row['kind'] not in ('infrastructure', 'cleanup')
                    or not isinstance(row['operationId'], str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', row['operationId'])
                    or not isinstance(row['purpose'], str) or row['purpose'] not in OPERATION_PURPOSES):
                raise BudgetError('invalid candidate operation reservation')
            if purposes.setdefault(row['operationId'], row['purpose']) != row['purpose']:
                raise BudgetError('candidate operation purpose changed')
        if not isinstance(row.get('basisDigest'), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", row["basisDigest"]):
            raise BudgetError("invalid pricing/liability basis digest")
        upper = money(row["amountUsd"])
        if upper <= 0:
            raise BudgetError("reservation must be positive")
        if row['state'] == 'reserved' and row.get('actualUsd') is not None:
            raise BudgetError('unsettled reservation cannot claim an actual charge')
        actual = money(row["actualUsd"]) if row["state"] == "settled" else upper
        if actual > upper and not allow_overrun:
            raise BudgetError("actual charge exceeded reserved liability; qualification blocked")
        liability += actual
        if row["kind"] != "cleanup":
            operating += actual
    if not allow_overrun and (liability > ceiling or operating > ceiling - reserve):
        raise BudgetError("candidate budget or protected cleanup reserve exhausted")
    if ledger['schemaVersion'] == 3:
        validate_admissions(ledger)
    return {"liabilityUsd": str(liability), "operatingUsd": str(operating),
            "remainingUsd": str(ceiling - liability)}


@contextmanager
def locked(path):
    lock = path.with_suffix(path.suffix + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise BudgetError("ledger is in use; never delete an active controller lock") from exc
    try:
        os.close(fd)
        yield
    finally:
        lock.unlink()


def update(path, operation, *, candidate_key, **values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        if path.exists():
            ledger = json.loads(path.read_text(encoding="utf-8"))
        elif operation == "init":
            ledger = {"schemaVersion": 2, "candidateKey": candidate_key,
                      "ceilingUsd": "500", "cleanupReserveUsd": "50", "entries": [], 'operations': []}
        else:
            raise BudgetError("candidate ledger must be initialized and retained before execution")
        if ledger["candidateKey"] != candidate_key:
            raise BudgetError("ledger belongs to another candidate")
        # A prior overrun blocks new work, but must not prevent retaining later
        # independently observed charges for already-reserved cleanup/resources.
        validate(ledger, allow_overrun=operation == 'settle')
        if operation == 'amend-foss4g':
            if ledger['schemaVersion'] != 3:
                previous = copy.deepcopy(ledger)
                ledger.update(schemaVersion=3, ceilingUsd='2000', cleanupReserveUsd='200', paidAdmissions=[],
                              operations=ledger.get('operations', []),
                              policyAmendment={'policyId': FOSS4G_POLICY, 'priorLedger': previous,
                                               'priorLedgerDigest': history_digest(previous)})
        elif operation in ('acquire-paid-attempt', 'check-paid-attempt', 'release-paid-attempt'):
            update_admission(ledger, operation, values)
        elif operation == "reserve":
            ledger["entries"].append({"id": values["reservation_id"], "campaignId": values["campaign_id"],
                "caseId": values["case_id"], "kind": values["kind"], "amountUsd": str(money(values["amount"])),
                "attempt": values["attempt"],
                "campaignMode": values.get("campaign_mode", "qualification"),
                "basisDigest": values["basis_digest"], "state": "reserved", "actualUsd": None})
        elif operation == 'reserve-operation':
            if ledger['schemaVersion'] not in (2, 3):
                raise BudgetError('legacy history requires reviewed migration, never a budget reset')
            ledger['operations'].append({'id':values['reservation_id'], 'operationId':values['operation_id'],
                'purpose':values['purpose'], 'kind':values['kind'], 'amountUsd':str(money(values['amount'])),
                'basisDigest':values['basis_digest'], 'state':'reserved', 'actualUsd':None})
        elif operation == "settle":
            rows = [r for r in ledger["entries"] + ledger.get('operations', []) if r["id"] == values["reservation_id"]]
            if len(rows) != 1 or rows[0]["state"] != "reserved":
                raise BudgetError("missing or already settled reservation")
            actual = money(values["amount"])
            # On an overrun retain the full charge as an audit fact. Subsequent
            # reads refuse this ledger; no reset or retry may erase the overrun.
            rows[0].update(state="settled", actualUsd=str(actual))
        elif operation not in {"init", "check"}:
            raise BudgetError("unsupported ledger operation")
        encoded = (json.dumps(ledger, indent=2, allow_nan=False) + "\n").encode()
        temp = path.with_suffix(path.suffix + ".tmp")
        # Failed reservation must not be committed. Actual overruns must be.
        if operation != "settle":
            validate(ledger)
        temp.write_bytes(encoded)
        os.replace(temp, path)
        result = validate(ledger)
        result["ledgerDigest"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["init", "amend-foss4g", "acquire-paid-attempt", "check-paid-attempt", "release-paid-attempt", "reserve", "reserve-operation", "settle", "check"])
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--candidate-key", required=True)
    parser.add_argument('--admission-id')
    parser.add_argument('--owner-token')
    parser.add_argument('--cleanup-status', choices=['pass','fail','blocked'])
    parser.add_argument('--expires-epoch', type=int)
    parser.add_argument('--infrastructure-reservation-id')
    parser.add_argument('--cleanup-reservation-id')
    parser.add_argument("--reservation-id")
    parser.add_argument('--operation-id')
    parser.add_argument('--purpose', choices=sorted(OPERATION_PURPOSES))
    parser.add_argument("--campaign-id")
    parser.add_argument("--campaign-mode", choices=["development", "qualification"], default="qualification")
    parser.add_argument("--case-id")
    parser.add_argument("--kind", choices=["model", "infrastructure", "cleanup"])
    parser.add_argument("--amount")
    parser.add_argument("--basis-digest")
    parser.add_argument("--attempt", type=int, choices=[1, 2], default=1)
    args = vars(parser.parse_args(argv))
    try:
        args['reservation_ids'] = {'infrastructure': args.pop('infrastructure_reservation_id'),
                                   'cleanup': args.pop('cleanup_reservation_id')}
        print(json.dumps(update(args.pop("ledger"), args.pop("operation"), **args)))
        return 0
    except (BudgetError, KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

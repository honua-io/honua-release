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
    extra = {'operations', 'policyAmendment'} if ledger['schemaVersion'] == 3 else {'operations'} if ledger['schemaVersion'] == 2 else set()
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
                ledger.update(schemaVersion=3, ceilingUsd='2000', cleanupReserveUsd='200',
                              operations=ledger.get('operations', []),
                              policyAmendment={'policyId': FOSS4G_POLICY, 'priorLedger': previous,
                                               'priorLedgerDigest': history_digest(previous)})
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
    parser.add_argument("operation", choices=["init", "amend-foss4g", "reserve", "reserve-operation", "settle", "check"])
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--candidate-key", required=True)
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
        print(json.dumps(update(args.pop("ledger"), args.pop("operation"), **args)))
        return 0
    except (BudgetError, KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

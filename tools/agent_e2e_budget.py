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
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re


class BudgetError(ValueError):
    pass


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


def validate(ledger):
    if ledger.get("schemaVersion") != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", ledger.get("candidateKey", "")):
        raise BudgetError("invalid candidate ledger")
    ceiling, reserve = money(ledger["ceilingUsd"]), money(ledger["cleanupReserveUsd"])
    if ceiling != 500 or reserve != 50:
        raise BudgetError("agent pack requires a $500 ceiling and $50 cleanup reserve")
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        raise BudgetError("entries must be a list")
    ids, liability, operating = set(), Decimal(0), Decimal(0)
    for row in entries:
        if not isinstance(row, dict) or not row.get("id") or row["id"] in ids:
            raise BudgetError("duplicate or missing reservation id")
        ids.add(row["id"])
        if row.get("kind") not in {"model", "infrastructure", "cleanup"} or row.get("state") not in {"reserved", "settled"}:
            raise BudgetError("invalid reservation")
        if not row.get("campaignId") or not row.get("caseId") or not row.get("basisDigest"):
            raise BudgetError("reservation lacks campaign, case or priced liability basis")
        if type(row.get("attempt")) is not int or row["attempt"] not in (1, 2):
            raise BudgetError("reservation must identify attempt 1 or 2")
        if row.get("campaignMode", "qualification") not in ("development", "qualification"):
            raise BudgetError("unknown campaign mode")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", row["basisDigest"]):
            raise BudgetError("invalid pricing/liability basis digest")
        upper = money(row["amountUsd"])
        if upper <= 0:
            raise BudgetError("reservation must be positive")
        actual = money(row["actualUsd"]) if row["state"] == "settled" else upper
        if actual > upper:
            raise BudgetError("actual charge exceeded reserved liability; qualification blocked")
        liability += actual
        if row["kind"] != "cleanup":
            operating += actual
    if liability > ceiling or operating > ceiling - reserve:
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
            ledger = {"schemaVersion": 1, "candidateKey": candidate_key,
                      "ceilingUsd": "500", "cleanupReserveUsd": "50", "entries": []}
        else:
            raise BudgetError("candidate ledger must be initialized and retained before execution")
        if ledger["candidateKey"] != candidate_key:
            raise BudgetError("ledger belongs to another candidate")
        validate(ledger)
        if operation == "reserve":
            ledger["entries"].append({"id": values["reservation_id"], "campaignId": values["campaign_id"],
                "caseId": values["case_id"], "kind": values["kind"], "amountUsd": str(money(values["amount"])),
                "attempt": values["attempt"],
                "campaignMode": values.get("campaign_mode", "qualification"),
                "basisDigest": values["basis_digest"], "state": "reserved", "actualUsd": None})
        elif operation == "settle":
            rows = [r for r in ledger["entries"] if r["id"] == values["reservation_id"]]
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
    parser.add_argument("operation", choices=["init", "reserve", "settle", "check"])
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--candidate-key", required=True)
    parser.add_argument("--reservation-id")
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

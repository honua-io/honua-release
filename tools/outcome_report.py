"""Replay a complete pinned outcome cohort for descriptive offline reporting."""
import argparse
from collections import Counter
from itertools import product
import json
from pathlib import Path
from statistics import median

from outcome_eval import (ARMS, MODELS, VERSION, WORKFLOWS, confined, digest,
                          plan, summarize, verify_files, write_new)
from strict_json import parse_json

SCHEMA = "honua.outcome-cohort/v1"
METRICS = ("personSentences", "personWords", "clicks", "fieldActions",
           "enteredCharacters", "distinctFields", "toolCalls", "elapsedMinutes")
STATUSES = ("success", "failed", "censored", "not-run", "excluded")


def pinned(root, manifest, name):
    if name not in manifest["files"]:
        raise ValueError(f"cohort must pin input: {name}")
    return confined(root, name)


def recorded_trial(entry, context):
    root, manifest = context
    events = pinned(root, manifest, entry["events"])
    grade = pinned(root, manifest, entry["grade"])
    result = summarize(events, grade)
    for name in result["evidence"]:
        evidence = confined(grade.parent, name)
        relative = str(evidence.relative_to(root.resolve()))
        pinned(root, manifest, relative)
    if result["trialId"] != entry["trialId"]:
        raise ValueError("cohort and independent grade trial identities differ")
    return result


def trial_result(entry, context):
    if type(entry.get("eligible")) is not bool:
        raise ValueError("explicit eligibility required for every planned trial")
    if not entry["eligible"] and (not isinstance(entry.get("reason"), str)
                                  or not entry["reason"].strip()):
        raise ValueError("ineligible trial requires a reason")
    recorded = "events" in entry or "grade" in entry
    if recorded:
        result = recorded_trial(entry, context)
    else:
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            raise ValueError("unrecorded trial requires a NOT RUN reason")
        result = {"trialId": entry["trialId"], "status": "not-run",
                  "elapsedMinutes": None, "reason": entry["reason"]}
    if result["status"] in {"success", "failed", "censored"} and not entry["eligible"]:
        raise ValueError("attempted non-excluded trial must be eligible")
    if result["status"] == "excluded" and entry["eligible"]:
        raise ValueError("excluded trial cannot be eligible")
    return dict(result, eligible=entry["eligible"], eligibilityReason=entry.get("reason"),
                attempted=result["elapsedMinutes"] is not None)


def load_cohort(path):
    manifest = parse_json(path.read_text())
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ValueError("registered cohort schema required")
    root = path.parent
    verify_files(root, manifest.get("files"))
    ordered = plan()
    supplied = parse_json(pinned(root, manifest, manifest["plan"]).read_text())
    if json.dumps(supplied, sort_keys=True) != json.dumps(ordered, sort_keys=True):
        raise ValueError("cohort plan differs from registered order or counter version")
    registered = ordered["trials"] + ordered["baselines"]
    entries = manifest["trials"]
    if (not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries)
            or [e.get("trialId") for e in entries] != [r["id"] for r in registered]):
        raise ValueError("cohort must retain every planned identity exactly once in order")
    rows = []
    for entry, trial in zip(entries, registered):
        result = trial_result(entry, (root, manifest))
        identity = {key: trial.get(key) for key in ("workflow", "pass", "model", "arm")}
        rows.append(dict(result, **identity, baselineId=trial.get("baselineId", trial["id"]),
                         modality="agent" if "model" in trial else "browser"))
    return rows


def distribution(values):
    known = [v for v in values if v is not None]
    return {"known": len(known), "missing": len(values) - len(known),
            "median": median(known) if known else None,
            "min": min(known) if known else None, "max": max(known) if known else None}


def descriptive(rows):
    attempted = [r for r in rows if r["attempted"]]
    counts = Counter(r["status"] for r in rows)
    successful = counts["success"]
    metrics = {key: distribution([r.get(key) for r in attempted]) for key in METRICS}
    metrics["tokens"] = {key: distribution([r.get("tokens", {}).get(key) for r in attempted])
                         for key in ("input", "output", "cache")}
    return {"planned": len(rows), "eligible": sum(r["eligible"] for r in rows),
            "attempted": len(attempted), **{status: counts[status] for status in STATUSES},
            "successPerPlanned": successful / len(rows) if rows else None,
            "successPerAttempted": successful / len(attempted) if attempted else None,
            "attemptedMetrics": metrics}


def groups(rows):
    result = []
    identities = list(product(WORKFLOWS, MODELS, ARMS))
    identities.extend((workflow, None, None) for workflow in WORKFLOWS)
    for workflow, model, arm in identities:
        selected = [r for r in rows if (r["workflow"], r["model"], r["arm"]) ==
                    (workflow, model, arm)]
        result.append(dict(workflow=workflow, model=model, arm=arm, **descriptive(selected)))
    return result


def browser_pairs(rows):
    by_id = {r["trialId"]: r for r in rows}
    pairs = []
    for agent in (r for r in rows if r["modality"] == "agent"):
        baseline = by_id[agent["baselineId"]]
        sentences, clicks = agent.get("personSentences"), baseline.get("clicks")
        success = agent["status"] == baseline["status"] == "success"
        ratio = clicks / sentences if success and sentences else None
        pairs.append({"trialId": agent["trialId"], "baselineId": baseline["trialId"],
                      "agentStatus": agent["status"], "baselineStatus": baseline["status"],
                      "agentSentences": sentences, "baselineClicks": clicks,
                      "agentClicks": agent.get("clicks"),
                      "agentFieldActions": agent.get("fieldActions"),
                      "agentEnteredCharacters": agent.get("enteredCharacters"),
                      "clicksPerPersonSentence": ratio})
    return pairs


def arm_pairs(rows):
    agents = {(r["workflow"], r["pass"], r["model"], r["arm"]): r
              for r in rows if r["modality"] == "agent"}
    pairs = []
    for (workflow, repetition, model, arm), treatment in agents.items():
        if arm == "A":
            continue
        control = agents[workflow, repetition, model, "A"]
        success = treatment["status"] == control["status"] == "success"
        deltas = {key: treatment[key] - control[key] if success else None
                  for key in METRICS}
        pairs.append({"controlId": control["trialId"], "treatmentId": treatment["trialId"],
                      "primary": arm == "D", "controlStatus": control["status"],
                      "treatmentStatus": treatment["status"], "treatmentMinusControl": deltas})
    return pairs


def report(path):
    rows = load_cohort(path)
    return {"schema": "honua.outcome-report/v1", "counterVersion": VERSION,
            "cohortSha256": digest(path), "live": False, "status": "OFFLINE REPORT",
            "trials": rows, "groups": groups(rows), "browserPairs": browser_pairs(rows),
            "armPairs": arm_pairs(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohort", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        write_new(args.output, report(args.cohort))
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        parser.exit(1, f"STOP: {error}\n")


if __name__ == "__main__":
    main()

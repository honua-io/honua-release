"""Prospective outcome planning and evidence-bound offline measurements."""
import argparse
import hashlib
import json
from itertools import product
from pathlib import Path
import re

from outcome_events import EventCounter
from strict_json import parse_json

VERSION = "outcome-eval-v2"
SEED = "honua-outcome-20261116-v2"
WORKFLOWS = {
    "install": 45, "upload-publish": 20, "style": 15, "share": 15,
    "build-app": 20, "sign-in-roles": 20, "recover-incident": 20, "migrate-service": 30,
}
MODELS = ("gpt-6.1-sol", "claude-opus-5-5")
ARMS = {"A": [], "B": ["okf"], "C": ["skills"], "D": ["okf", "skills"]}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def plan():
    trials = []
    rotations = {}
    for model in MODELS:
        blocks = [(r, w) for r in range(1, 4) for w in WORKFLOWS]
        blocks.sort(key=lambda b: hashlib.sha256(f"{SEED}/{model}/{b}".encode()).hexdigest())
        rotations.update({(model, *block): i % 4 for i, block in enumerate(blocks)})
    for repetition, model, (workflow, minutes) in product(range(1, 4), MODELS, WORKFLOWS.items()):
        rotation = rotations[model, repetition, workflow]
        arms = list(ARMS)
        order = arms[rotation:] + arms[:rotation]
        for arm in order:
            trials.append({
                "id": f"r{repetition}-{model}-{workflow}-{arm}",
                "pass": repetition, "model": model, "workflow": workflow,
                "arm": arm, "knowledge": ARMS[arm], "minutesLimit": minutes,
                "toolCallLimit": 100, "personSentenceLimit": 10,
                "baselineId": f"r{repetition}-browser-{workflow}",
            })
    baselines = [{"id": f"r{r}-browser-{w}", "pass": r, "workflow": w}
                 for r in range(1, 4) for w in WORKFLOWS]
    return {"counterVersion": VERSION, "seed": SEED, "status": "NOT RUN",
            "trials": trials, "baselines": baselines}


def confined(root, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError("evidence path must be relative and confined")
    root = root.resolve()
    path = (root / name).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("evidence path escapes root or is not a file")
    return path


def verify_files(root, files):
    if not isinstance(files, dict) or not files:
        raise ValueError("nonempty file hash map required")
    for name, expected in files.items():
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("invalid SHA256")
        if digest(confined(root, name)) != expected:
            raise ValueError(f"changed evidence: {name}")


def validate_grade(grade, root):
    if grade.get("status") not in {"success", "failed", "censored", "not-run", "excluded"}:
        raise ValueError("independent grade status required")
    if not isinstance(grade.get("grader"), str) or not grade["grader"].strip():
        raise ValueError("independent grader identity required")
    if grade["status"] != "success" and not grade.get("reason"):
        raise ValueError("non-success reason required")
    verify_files(root, grade.get("evidence"))
    if grade.get("workflow") not in WORKFLOWS or grade.get("modality") not in {"agent", "browser"}:
        raise ValueError("registered workflow and modality required")
    registered = plan()
    modality = grade["modality"]
    rows = registered["trials"] if modality == "agent" else registered["baselines"]
    trial = next((r for r in rows if r["id"] == grade.get("trialId")), None)
    if trial is None or trial["workflow"] != grade["workflow"]:
        raise ValueError("trialId must match registered workflow and modality")
    for key in ("pass", "model", "arm", "baselineId"):
        if key in grade and grade[key] != trial.get(key):
            raise ValueError(f"grade identity mismatch: {key}")
    if grade["status"] == "success":
        if grade.get("postconditionsPassed") is not True or grade.get("controlsPassed") is not True:
            raise ValueError("success requires independent postconditions and controls")
        if not grade.get("terminalReadback") or grade["terminalReadback"] not in grade["evidence"]:
            raise ValueError("success requires hashed terminal readback")
    return trial


def finish_timing(counter, events, grade):
    boundaries = counter.boundaries
    if grade["status"] == "not-run" or (grade["status"] == "excluded" and not events):
        if events:
            raise ValueError("NOT RUN cannot contain evaluated events")
        return {"elapsedMinutes": None, "limitExceeded": False}
    if "stop" in boundaries and "terminal-readback" in boundaries:
        raise ValueError("stop and terminal readback are mutually exclusive")
    if "dispatch" not in boundaries:
        raise ValueError("dispatch event required")
    end_kind = "terminal-readback" if grade["status"] == "success" else "stop"
    end = boundaries.get(end_kind)
    if end is None or end < boundaries["dispatch"]:
        raise ValueError("terminal readback/stop boundary required")
    start_index = next(i for i, e in enumerate(events) if e["type"] == "dispatch")
    end_index = next(i for i, e in enumerate(events) if e["type"] == end_kind)
    if start_index >= end_index:
        raise ValueError("dispatch must precede terminal readback/stop")
    if any(e["type"] != "observer" and
           (i < start_index or i > end_index) for i, e in enumerate(events)):
        raise ValueError("evaluated events outside task timing boundary")
    elapsed = (end - boundaries["dispatch"]) / 60
    exceeded = (counter.counts["personSentences"] > 10 or counter.counts["toolCalls"] > 100
                or elapsed > WORKFLOWS[grade["workflow"]])
    if exceeded and grade["status"] not in {"censored", "excluded"}:
        raise ValueError("exceeded run limits require censored or excluded status")
    return {"elapsedMinutes": elapsed, "limitExceeded": exceeded}


def _summarize(events, grade, root):
    """Count raw events, retaining failure costs; never infer a grade from prose."""
    trial = validate_grade(grade, root)
    modality = grade["modality"]
    result = {"counterVersion": VERSION, "status": grade["status"]}
    result.update({"trialId": trial["id"], "workflow": trial["workflow"],
                   "modality": modality, "pass": trial["pass"],
                   "model": trial.get("model"), "arm": trial.get("arm"),
                   "baselineId": trial.get("baselineId", trial["id"]),
                   "reason": grade.get("reason")})
    counter = EventCounter()
    for event in events:
        counter.add(event)
    result.update(counter.result())
    result.update(finish_timing(counter, events, grade))
    result["grader"] = grade["grader"]
    result["evidence"] = grade["evidence"]
    result["sentenceBoundariesReviewed"] = grade.get("sentenceBoundariesReviewed") is True
    if result["personSentences"] and not result["sentenceBoundariesReviewed"]:
        raise ValueError("QA must review sentence segmentation")
    return result


def summarize(events_path, grade_path):
    """Count the exact transcript pinned by the independent grade."""
    grade = parse_json(grade_path.read_text())
    if not isinstance(grade, dict) or not isinstance(grade.get("evidence"), dict):
        raise ValueError("grade and evidence must be objects")
    event_name = str(events_path.resolve().relative_to(grade_path.parent.resolve()))
    confined(grade_path.parent, event_name)
    raw = events_path.read_bytes()
    if grade.get("evidence", {}).get(event_name) != hashlib.sha256(raw).hexdigest():
        raise ValueError("grade must pin the raw event transcript")
    if grade.get("status") == "success":
        readback = confined(grade_path.parent, grade.get("terminalReadback"))
        if readback.samefile(events_path):
            raise ValueError("terminal readback must be independent of the transcript")
    events = [parse_json(line) for line in raw.splitlines() if line.strip()]
    return _summarize(events, grade, grade_path.parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("output", type=Path)
    p = sub.add_parser("verify-pins")
    p.add_argument("manifest", type=Path, help="JSON with files: relative-path -> SHA256")
    p = sub.add_parser("summarize")
    p.add_argument("events", type=Path, help="JSONL recorded events")
    p.add_argument("grade", type=Path, help="independent grade; evidence relative to its directory")
    p.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            write_new(args.output, plan())
        elif args.command == "verify-pins":
            verify_files(args.manifest.parent, parse_json(args.manifest.read_text())["files"])
            print("Pinned files match; this is not live admission or grading.")
        else:
            write_new(args.output, summarize(args.events, args.grade))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"STOP: {error}\n")


if __name__ == "__main__":
    main()

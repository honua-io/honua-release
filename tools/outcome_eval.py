"""Prospective outcome planning and evidence-bound offline measurements."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from outcome_events import EventCounter

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
    for repetition in range(1, 4):
        for model in MODELS:
            for workflow, minutes in WORKFLOWS.items():
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


def finish_timing(result, events, grade):
    boundaries = result.pop("boundaries")
    if grade["status"] == "not-run" or (grade["status"] == "excluded" and not events):
        if events:
            raise ValueError("NOT RUN cannot contain evaluated events")
        result["elapsedMinutes"] = None
    else:
        if "stop" in boundaries and "terminal-readback" in boundaries:
            raise ValueError("stop and terminal readback are mutually exclusive")
        if "dispatch" not in boundaries:
            raise ValueError("dispatch event required")
        end = boundaries.get("terminal-readback") if grade["status"] == "success" else boundaries.get("stop")
        if end is None or end < boundaries["dispatch"]:
            raise ValueError("terminal readback/stop boundary required")
        if any(e["type"] not in {"observer"} and
               (e["seconds"] < boundaries["dispatch"] or e["seconds"] > end) for e in events):
            raise ValueError("evaluated events outside task timing boundary")
        result["elapsedMinutes"] = (end - boundaries["dispatch"]) / 60
        result["limitExceeded"] = (
            result["personSentences"] > 10 or result["toolCalls"] > 100
            or result["elapsedMinutes"] > WORKFLOWS[grade["workflow"]]
        )
        if result["limitExceeded"] and grade["status"] not in {"censored", "excluded"}:
            raise ValueError("exceeded run limits require censored or excluded status")


def _summarize(events, grade, root):
    """Count raw events, retaining failure costs; never infer a grade from prose."""
    trial = validate_grade(grade, root)
    modality = grade["modality"]
    result = {"counterVersion": VERSION, "status": grade["status"]}
    result.update({"trialId": trial["id"], "workflow": trial["workflow"],
                   "modality": modality, "pass": trial["pass"],
                   "model": trial.get("model"), "arm": trial.get("arm"),
                   "baselineId": trial.get("baselineId", trial["id"]),
                   "reason": grade.get("reason"), "limitExceeded": False})
    counter = EventCounter()
    for event in events:
        counter.add(event)
    result.update(counter.result())
    result["boundaries"] = counter.boundaries
    finish_timing(result, events, grade)
    result["grader"] = grade["grader"]
    result["evidence"] = grade["evidence"]
    result["sentenceBoundariesReviewed"] = grade.get("sentenceBoundariesReviewed") is True
    if result["personSentences"] and not result["sentenceBoundariesReviewed"]:
        raise ValueError("QA must review sentence segmentation")
    return result


def summarize(events_path, grade_path):
    """Count the exact transcript pinned by the independent grade."""
    grade = json.loads(grade_path.read_text())
    if not isinstance(grade, dict) or not isinstance(grade.get("evidence"), dict):
        raise ValueError("grade and evidence must be objects")
    event_name = str(events_path.resolve().relative_to(grade_path.parent.resolve()))
    confined(grade_path.parent, event_name)
    raw = events_path.read_bytes()
    if grade.get("evidence", {}).get(event_name) != hashlib.sha256(raw).hexdigest():
        raise ValueError("grade must pin the raw event transcript")
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
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
            verify_files(args.manifest.parent, json.loads(args.manifest.read_text())["files"])
            print("Pinned files match; this is not live admission or grading.")
        else:
            write_new(args.output, summarize(args.events, args.grade))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"STOP: {error}\n")


if __name__ == "__main__":
    main()

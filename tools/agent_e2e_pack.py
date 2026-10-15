#!/usr/bin/env python3
"""Validate a frozen goal corpus and export ONLY one agent-visible task.

The pack and oracle corpus are controller inputs. Never mount this repository,
pack file, expected campaign, credentials store or oracle outputs in an agent container.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

from agent_e2e_errors import PackError


TARGETS = {"local-docker": 72, "aws-ecs-redis-on": 72, "aws-eks": 72,
           "aws-lambda": 18, "aws-batch": 18}
VARIANTS = ("A", "B", "C")


def digest_bytes(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_digest(value):
    return digest_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False, allow_nan=False).encode())


def load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PackError(f"duplicate JSON key {key}")
            result[key] = value
        return result
    def constant(value):
        raise PackError(f"nonfinite JSON number {value}")
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique,
                      parse_constant=constant)


def inventory(pack):
    if pack.get("schemaVersion") != 1 or pack.get("totalBudgetUsd") != 500:
        raise PackError("unsupported pack or changed candidate budget")
    rows, ids = [], set()
    expected_ids = {f"P{i:02}" for i in range(1, 25)} | {f"L{i:02}" for i in range(1, 7)} | {f"B{i:02}" for i in range(1, 7)}
    for scenario in pack["scenarios"]:
        sid = scenario["id"]
        if sid in ids or sid not in expected_ids:
            raise PackError("duplicate or unexpected scenario")
        ids.add(sid)
        expected_targets = ({"local-docker", "aws-ecs-redis-on", "aws-eks"} if sid.startswith("P")
                            else {"aws-lambda"} if sid.startswith("L") else {"aws-batch"})
        targets = scenario["targets"]
        if len(targets) != len(set(targets)) or set(targets) != expected_targets:
            raise PackError(f"incorrect target coverage for {sid}")
        if set(scenario["prompts"]) != set(VARIANTS):
            raise PackError(f"missing or extra prompt variant for {sid}")
        if len(set(scenario["prompts"].values())) != 3:
            raise PackError(f"variants must differ for {sid}")
        if not scenario["assertions"] or len(set(scenario["assertions"])) != len(scenario["assertions"]):
            raise PackError(f"empty or duplicate assertions for {sid}")
        for name in ("maxTurns", "maxActions", "timeoutSeconds"):
            value = scenario["limits"][name]
            if type(value) is not int or value <= 0:
                raise PackError(f"invalid {name} for {sid}")
        for target in targets:
            for variant in VARIANTS:
                prompt = scenario["prompts"][variant]
                if not isinstance(prompt, str) or not prompt.strip():
                    raise PackError(f"empty prompt {sid}:{variant}")
                rows.append({"caseId": f"{sid}:{variant}:{target}", "scenarioId": sid,
                             "variant": variant, "target": target,
                             "promptDigest": digest_bytes(prompt.encode("utf-8"))})
    if ids != expected_ids or len(rows) != 252:
        raise PackError("the complete 36-scenario, 252-case inventory is mandatory")
    for target, count in TARGETS.items():
        if sum(r["target"] == target for r in rows) != count:
            raise PackError("target inventory mismatch")
    return rows


def select(pack, case_id):
    matches = [row for row in inventory(pack) if row["caseId"] == case_id]
    if len(matches) != 1:
        raise PackError("case is outside frozen inventory")
    case = matches[0]
    return case, next(s for s in pack["scenarios"] if s["id"] == case["scenarioId"])


def visible_task(pack, case_id, public_inputs):
    case, scenario = select(pack, case_id)
    # Deliberate allowlist of user-facing fields, never dict subtraction: later
    # additions of oracle/custody/expected-state fields cannot silently leak.
    if not isinstance(public_inputs, dict) or set(public_inputs) - {"files", "documentation", "target", "credentialReferences"}:
        raise PackError("public inputs must contain only reviewed user-facing fields")
    for field in ("documentation", "credentialReferences"):
        if not isinstance(public_inputs.get(field, []), list) or not all(isinstance(v, str) and v for v in public_inputs.get(field, [])):
            raise PackError(f"{field} must contain only public strings")
    if not isinstance(public_inputs.get("target", ""), str):
        raise PackError("target must be a public description")
    if not isinstance(public_inputs.get("files", []), list):
        raise PackError("files must be a public file index")
    paths = set()
    for file in public_inputs.get("files", []):
        if not isinstance(file, dict) or set(file) != {"path", "sha256"}:
            raise PackError("unreviewed public file metadata")
        portable_path(file["path"])
        if file["path"] in paths or not re.fullmatch(r"sha256:[0-9a-f]{64}", file["sha256"]):
            raise PackError("duplicate or unhashed public file")
        paths.add(file["path"])
    return {"task": scenario["prompts"][case["variant"]], "environment": public_inputs}


def portable_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/")):
        raise PackError("unsafe corpus path")
    return value


def verify_corpus(root, manifest):
    """Check actual bytes; hashing a manifest alone does not bind exported data."""
    root = Path(root).resolve(strict=True)
    seen = set()
    for row in manifest["files"]:
        relative = portable_path(row["path"])
        if relative in seen:
            raise PackError("duplicate corpus file")
        seen.add(relative)
        path = root / relative
        if not path.resolve(strict=True).is_relative_to(root) or any(p.is_symlink() for p in (path, *path.parents) if p != root and root in p.parents):
            raise PackError("corpus symlink or escape")
        data = path.read_bytes()
        if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise PackError(f"corpus bytes changed: {relative}")


def selected_files(pack, case_id, manifest):
    case, scenario = select(pack, case_id)
    fixtures = {r["id"]: r for r in manifest["fixtures"]}
    prefixes, external = [], set()
    for fid in scenario["fixtureIds"]:
        fixture = fixtures[fid]
        if "variants" in fixture:
            prefixes.append(fixture["variants"][case["variant"]]["path"] + "/")
        elif "path" in fixture:
            external.add(fixture["path"])
    known = {s["id"] for s in pack["scenarios"]}
    for row in manifest["files"]:
        scope = row.get("scenarioIds")
        if scope is not None and (not isinstance(scope, list) or not scope or not all(isinstance(s, str) and s in known for s in scope) or len(set(scope)) != len(scope)):
            raise PackError("invalid fixture scenario scope")
    return [row for row in manifest["files"] if row["visibility"] == "selected-case-input"
            and ("scenarioIds" not in row or case["scenarioId"] in row["scenarioIds"])
            and (any(row["path"].startswith(p) for p in prefixes) or row["path"] in external)]


def export_inputs(pack, case_id, corpus_root, manifest, destination):
    """Materialize only selected public fixture bytes in a NEW empty directory.

    Large uncommitted inputs require a separate pinned materializer and verified
    output; this function never substitutes a smaller committed dataset.
    """
    verify_corpus(corpus_root, manifest)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    rows = selected_files(pack, case_id, manifest)
    result = []
    for row in rows:
        target = destination / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(corpus_root) / row["path"], target)
        result.append({"path": row["path"], "sha256": "sha256:" + row["sha256"]})
    return result


def verify_export_index(pack, case_id, manifest, index):
    """Reject hidden answers, other variants and fabricated source hashes."""
    allowed = {row["path"]: "sha256:" + row["sha256"] for row in selected_files(pack, case_id, manifest)}
    # Materialized large inputs are allowed ONLY in cases selecting that fixture.
    _, scenario = select(pack, case_id)
    for row in manifest["fixtures"]:
        if row["id"] in scenario["fixtureIds"] and row.get("committed") is False:
            allowed[row["name"]] = "sha256:" + row["sha256"]
    for row in index:
        if row["path"] not in allowed or row["sha256"] != allowed[row["path"]]:
            raise PackError("export contains unselected, private or changed fixture")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pack", type=Path, required=True)
    p.add_argument("--case-id")
    p.add_argument("--public-inputs", type=Path)
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)
    try:
        pack = load_json(args.pack)
        value = inventory(pack)
        if args.case_id:
            if not args.public_inputs or not args.out:
                raise PackError("export requires reviewed public inputs and explicit output")
            value = visible_task(pack, args.case_id, load_json(args.public_inputs))
        text = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        if args.out:
            args.out.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0
    except (PackError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

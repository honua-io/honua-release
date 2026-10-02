#!/usr/bin/env python3
"""Fail-closed journey gate for downloaded per-cell cloud artifacts.

The intake is one gate-report-cloud.json per artifact, with journeyAttempts pointing
to hashed receipt files relative to that report. Candidate identity is the SHA-256
of the exact manifest bytes, independently supplied by the caller. The current
terminal receipt describes the deterministic AWS adapter; target.id binds its
observation to a cloud cell, while target.kind describes the adapter transport.
Driver labels are preserved from attempt metadata (the existing producer omits
the label and uses the deterministic terminal driver). This gate records driver
selection; promotion's genuine-model qualification remains a separate gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
GA_CELLS = tuple(f"{target}/redis-{redis}" for target in ("aws-ecs", "aws-serverless")
                 for redis in ("off", "on"))
PREVIEW_CELLS = tuple(f"{target}/redis-{redis}" for target in ("aws-eks", "aws-mixed")
                      for redis in ("off", "on"))
DRIVERS = ("deterministic", "genuine-model")
MAX_AGE_SECONDS = 86400


def load_json(path: Path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("nonstandard JSON number")

    return json.loads(path.read_bytes(), object_pairs_hook=pairs, parse_constant=invalid_constant)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp needs timezone")
    return result


def require_current(value, at):
    if not 0 <= (at - timestamp(value)).total_seconds() <= MAX_AGE_SECONDS:
        raise ValueError("stale or future receipt evidence")


def validate_cell(report, directory, cell, manifest, digest, run_id, run_attempt, at):
    attempts = report.get("journeyAttempts")
    if (not isinstance(attempts, list) or not 1 <= len(attempts) <= 2
            or any(not isinstance(a, dict) or type(a.get("number")) is not int
                   or a["number"] != i for i, a in enumerate(attempts, 1))):
        raise ValueError("every attempt must be recorded in order, starting at 1, within 2 attempts")
    schema = load_json(ROOT / "certification/terminal-journey/receipt.schema.json")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    contract = load_json(ROOT / "certification/terminal-journey/journey.v1.json")
    expected_stages = [(s["number"], s["id"], s["command"]) for s in contract["stages"]]
    server = manifest["components"]["honua-server"]
    expected_server = {"sourceSha": server["sha"], "image": f"{server['image']}@{server['digest']}"}
    expected_clients = {name: {key: pin.get(key) for key in
                        ("package", "version", "integrity", "digest", "sourceSha")}
                        for name, pin in manifest["clientArtifacts"].items()
                        if name in ("honua-sdk-js", "honua-mcp-server")}
    if set(expected_clients) != {"honua-sdk-js", "honua-mcp-server"}:
        raise ValueError("candidate lacks required client pins")
    history = []
    passed = False
    for record in attempts:
        if passed:
            raise ValueError("attempt recorded after passing receipt")
        if (record.get("cell") != cell or record.get("runId") != run_id
                or record.get("runAttempt") != run_attempt or record.get("candidateDigest") != digest):
            raise ValueError("wrong candidate, run or cell binding")
        driver = record.get("driver", "deterministic")
        if driver not in DRIVERS:
            raise ValueError("unknown journey driver")
        relative = Path(record["receipt"])
        path = directory / relative
        if (relative.is_absolute() or ".." in relative.parts
                or not path.resolve().is_relative_to(directory.resolve())):
            raise ValueError("unsafe receipt path")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != record.get("receiptSha256"):
            raise ValueError("receipt digest mismatch")
        receipt = load_json(path)
        if not isinstance(receipt, dict) or not validator.is_valid(receipt):
            raise ValueError("invalid or skipped terminal journey receipt")
        if (receipt["target"]["id"] != cell or receipt["release"] != manifest["platformRelease"]
                or receipt["server"] != expected_server or receipt["clientArtifacts"] != expected_clients):
            raise ValueError("wrong candidate or receipt cell pins")
        require_current(receipt["generatedAt"], at)
        if [(s["number"], s["stage"], s["command"]) for s in receipt["stages"]] != expected_stages:
            raise ValueError("receipt does not cover every pinned journey stage")
        passed = receipt["status"] == "pass"
        attribution = record.get("failureAttribution")
        if not passed and attribution not in ("model", "infrastructure"):
            raise ValueError("failed attempt is unattributed")
        if passed:
            if (receipt["mode"] != "live" or receipt["target"]["kind"] == "none"
                    or receipt["roster"]["status"] != "pass"):
                raise ValueError("passing receipt lacks live journey or roster evidence")
            for stage in receipt["stages"]:
                evidence = stage["evidence"]
                if evidence["source"] != "live-aws-ecs":
                    raise ValueError("passing receipt lacks cloud evidence")
                require_current(evidence.get("observedAt"), at)
                if timestamp(evidence["observedAt"]) > timestamp(receipt["generatedAt"]):
                    raise ValueError("stage observation postdates receipt")
        history.append({"number": record["number"], "status": receipt["status"],
                        "driver": driver, "failureAttribution": attribution,
                        "receipt": record["receipt"]})
    if not passed or report.get("status") != "pass":
        raise ValueError("cell missing a passing receipt within 2 attempts, or cell skipped/failed")
    return history


def evaluate(receipts: Path, candidate: Path, candidate_digest: str, run_id: str,
             run_attempt: str, *, at=None):
    at = at or datetime.now(timezone.utc)
    rows, errors, by_cell = [], [], {}
    try:
        if not re.fullmatch(r"[0-9a-f]{64}", candidate_digest):
            raise ValueError("candidate digest must be a SHA-256")
        if not all(re.fullmatch(r"[1-9][0-9]*", v) for v in (run_id, run_attempt)):
            raise ValueError("source run id and attempt must be positive integers")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != candidate_digest:
            raise ValueError("candidate manifest digest mismatch")
        manifest = yaml.safe_load(candidate.read_bytes())
        if not isinstance(manifest, dict):
            raise ValueError("invalid candidate manifest")
        # Validate the identity even when all cell receipts are absent.
        manifest["platformRelease"]
        manifest["components"]["honua-server"]["sha"]
        manifest["components"]["honua-server"]["digest"]
        manifest["components"]["honua-server"]["image"]
        manifest["clientArtifacts"]
    except (ValueError, OSError, KeyError, TypeError, yaml.YAMLError):
        manifest = None
        errors.append("missing or invalid candidate identity/manifest or source run identity")
    for path in sorted(receipts.rglob("gate-report-cloud.json")):
        try:
            report = load_json(path)
            if not isinstance(report, dict) or report.get("cell") not in (*GA_CELLS, *PREVIEW_CELLS):
                raise ValueError("unknown or missing cell")
            by_cell.setdefault(report["cell"], []).append((report, path.parent))
        except (ValueError, OSError, TypeError):
            preview_cell = next((cell for cell in PREVIEW_CELLS if path.parent.name ==
                                 "e2e-cloud-aws-gate-report-" + cell.replace("/", "-")), None)
            if preview_cell:
                by_cell.setdefault(preview_cell, []).append(({"status": "invalid"}, path.parent))
                continue
            # A malformed artifact cannot silently remove a conflicting GA cell.
            errors.append(f"invalid cell report: {path.relative_to(receipts)}")
    for cell in (*GA_CELLS, *PREVIEW_CELLS):
        preview = cell in PREVIEW_CELLS
        entries = by_cell.get(cell, [])
        row = {"cell": cell, "evidenceTier": "Preview" if preview else "GA",
               "counted": not preview, "status": "missing", "drivers": [], "attempts": []}
        if preview:
            row["status"] = entries[0][0].get("status", "invalid") if entries else "missing"
            row["reports"] = [report for report, _ in entries]
            row["drivers"] = sorted({str(a.get("driver", "deterministic"))
                                     for report, _ in entries for a in report.get("journeyAttempts", [])
                                     if isinstance(a, dict)} if entries and all(
                                         isinstance(r.get("journeyAttempts", []), list) for r, _ in entries) else set())
            row["why"] = "informational Preview cell; never counted or blocking"
        else:
            try:
                if len(entries) != 1:
                    raise ValueError("missing cell receipt" if not entries else "duplicate cell reports")
                if manifest is None:
                    raise ValueError("invalid candidate identity")
                report, directory = entries[0]
                row["attempts"] = validate_cell(report, directory, cell, manifest, candidate_digest,
                                                 run_id, run_attempt, at)
                row["drivers"] = sorted({a["driver"] for a in row["attempts"]})
                row.update(status="pass", why="exact candidate journey passed within 2 recorded attempts")
            except (ValueError, OSError, KeyError, TypeError) as error:
                # Diagnostics contain no raw receipt fields or command output.
                row.update(status="fail", why=str(error) if isinstance(error, ValueError) else
                           "missing or malformed journey evidence")
                errors.append(f"{cell}: {row['why']}")
        rows.append(row)
    status = "fail" if errors else "pass"
    url = f"https://github.com/honua-io/honua-release/actions/runs/{run_id}"
    why = "; ".join(errors) if errors else "all four GA journeys passed for the exact candidate"
    return {"gate": "journey", "status": status, "overallStatus": status,
            "source": "cloud-cell-receipts", "why": why, "evidence_url": url,
            "candidateDigest": candidate_digest, "runId": run_id, "runAttempt": run_attempt,
            "generatedAt": at.isoformat().replace("+00:00", "Z"), "cells": rows,
            "gates": [{"gate": "journey", "status": status, "decided": status,
                       "source": "cloud-cell-receipts", "why": why, "evidence_url": url}]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--candidate-digest", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    report = evaluate(args.receipts, args.candidate, args.candidate_digest, args.run_id, args.run_attempt)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"journey: {report['status']} — {report['why']}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

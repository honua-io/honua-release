#!/usr/bin/env python3
"""Authenticated AI-native and workflow-outcome release producers.

The workflows pass the candidate pins. This module emits one
honua.release-promise-receipt/v1 file per contracted row only when the shared
Launch Stack harness (import gate.verify_installed) confirms the installed
stack and the pinned cohort agrees: every planned trial is present, successful
readbacks name that stack, hostile-input channels were blocked, and recovery
timings are recomputed from pinned evidence. Anything missing, changed, or
belonging to another candidate leaves no receipt. No model call and no stack
create happens here.
"""
import importlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import tempfile

import outcome_report
import rc_gate_window as gate
import release_outcomes as outcomes
from outcome_eval import confined

CONTRACT = Path(__file__).resolve().parents[1] / "certification/release-promise.v1.json"
AI_WORKFLOW = "gate-ai-native.yml"
OUTCOME_WORKFLOW = "gate-release-outcomes.yml"
STACK_NAME = re.compile(r"honua-gate-[A-Za-z0-9-]+")
PHASES = ["setup", "readback", "cleanup"]
INJECTION_CHANNELS = ("logs", "layer-names", "metadata")
SENTENCE_SOURCE = {
    "install": "install", "publish-layer": "upload-publish", "style": "style",
    "share": "share", "build-app": "build-app", "sign-in-roles": "sign-in-roles",
    "recover-incident": "recover-incident", "migrate-service": "migrate-service",
}
MINUTE_SOURCE = {
    "developer-map-with-skills": ("upload-publish", "agent", "D"),
    "developer-map-without-skills": ("upload-publish", "agent", "A"),
    "developer-app-with-skills": ("build-app", "agent", "D"),
    "developer-app-without-skills": ("build-app", "agent", "A"),
    "studio-publish-layer": ("upload-publish", "browser", None),
    "studio-build-app": ("build-app", "browser", None),
}
AI_SOURCE = {
    "ai-operate": "recover-incident", "ai-publish": "upload-publish",
    "ai-build": "build-app", "ai-migrate": "migrate-service",
}


class Unavailable(Exception):
    pass


def whole_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Unavailable("measurement is not a finite number")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if value < 0:
        raise Unavailable("measurement is negative")
    return value


def candidate_from_environment():
    candidate = gate.Candidate(
        gate.normalize_sha(os.environ.get("SERVER_SHA")),
        gate.normalize_digest(os.environ.get("SERVER_DIGEST")),
        gate.normalize_digest(os.environ.get("LOCK_DIGEST")),
    )
    if not all((candidate.server_sha, candidate.server_digest, candidate.platform_lock)):
        raise Unavailable("candidate identity is incomplete")
    return candidate


def identity_of(candidate):
    return {"serverSha": candidate.server_sha, "imageDigest": candidate.server_digest,
            "platformLockDigest": candidate.platform_lock}


def admission_dir():
    raw = os.environ.get("ADMISSION_DIR", "")
    path = Path(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise Unavailable("admission directory is missing")
    if not path.is_dir():
        raise Unavailable("admission directory is missing")
    return path


def contract_rows(workflow):
    document = json.loads(CONTRACT.read_text())
    rows = [row for row in document["rows"] if row.get("workflow") == workflow]
    rows.extend(row for row in document["outcomes"] if row.get("workflow") == workflow)
    if not rows:
        raise Unavailable(f"no contracted rows for {workflow}")
    return rows


def installed_proof(admission, candidate):
    """Ask the shared harness. Absent harness fails closed; this module never deploys."""
    try:
        module = importlib.import_module("gate.verify_installed")
        verify = module.verify_installed
    except (ImportError, AttributeError) as error:
        raise Unavailable("shared Launch Stack harness is not installed") from error
    proof = verify(admission, candidate)
    if not isinstance(proof, dict):
        raise Unavailable("harness returned no installed proof")
    return proof


def load_rows(admission):
    try:
        return outcome_report.load_cohort(admission / "cohort.json")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise Unavailable(str(error)) from error


def pinned_json(root, manifest, name):
    if not isinstance(name, str) or name not in manifest.get("files", {}):
        raise Unavailable(f"unpinned evidence: {name}")
    try:
        return json.loads(confined(root, name).read_text())
    except (OSError, ValueError) as error:
        raise Unavailable(f"unreadable evidence: {name}") from error


def require_harness(admission, proof, identity):
    manifest = json.loads((admission / "cohort.json").read_text())
    pinned = pinned_json(admission, manifest, "harness.json")
    stack = proof.get("stackName")
    if (not isinstance(stack, str) or not STACK_NAME.fullmatch(stack)
            or proof.get("phases") != PHASES or pinned.get("stackName") != stack
            or pinned.get("phases") != PHASES or pinned.get("candidate") != identity
            or proof.get("candidate") != identity):
        raise Unavailable("installed harness proof does not match the candidate")
    return manifest, stack


def require_readbacks(admission, manifest, stack, identity):
    for entry in manifest["trials"]:
        if "grade" not in entry:
            continue
        grade = pinned_json(admission, manifest, entry["grade"])
        if grade.get("status") != "success":
            continue
        readback_name = grade.get("terminalReadback")
        if not isinstance(readback_name, str):
            raise Unavailable("successful trial is missing its terminal readback")
        try:
            grade_path = confined(admission, entry["grade"])
            relative = str((grade_path.parent / readback_name).resolve().relative_to(admission.resolve()))
        except (OSError, ValueError) as error:
            raise Unavailable("terminal readback escapes the admission") from error
        readback = pinned_json(admission, manifest, relative)
        if (readback.get("stackName") != stack or readback.get("serverSha") != identity["serverSha"]
                or readback.get("imageDigest") != identity["imageDigest"]
                or readback.get("platformLockDigest") != identity["platformLockDigest"]):
            raise Unavailable("terminal readback is not the installed candidate")


def require_injection(bound):
    channels = bound["proof"].get("injection")
    if not isinstance(channels, dict) or set(channels) != set(INJECTION_CHANNELS):
        raise Unavailable("hostile-input controls are incomplete")
    for name in INJECTION_CHANNELS:
        document = pinned_json(bound["admission"], bound["manifest"], channels[name])
        if document.get("channel") != name or document.get("blocked") is not True:
            raise Unavailable(f"hostile input was not blocked: {name}")


def require_censored(rows):
    for row in rows:
        if row["status"] == "censored" and row.get("stopEvents") != 1:
            raise Unavailable("censored trial is missing its stop boundary")


def selected(rows, workflow, spec):
    modality, arm, status = spec
    return [row for row in rows if row["workflow"] == workflow and row["modality"] == modality
            and (arm is None or row.get("arm") == arm)
            and (status is None or row["status"] == status)]


def require_denominator(rows, workflow, modality):
    found = selected(rows, workflow, (modality, None, None))
    expected = 24 if modality == "agent" else 3
    if len(found) != expected:
        raise Unavailable(f"dropped {modality} trials for {workflow}")
    return found


def median_of(rows, workflow, spec):
    modality, arm, kind = spec
    require_denominator(rows, workflow, modality)
    successes = selected(rows, workflow, (modality, arm, "success"))
    if not successes:
        raise Unavailable(f"no successful {modality} trials for {workflow}")
    values = []
    for row in successes:
        if kind == "clicks":
            values.append(row["clicks"] + row["fieldActions"])
        elif kind == "minutes":
            if row.get("elapsedMinutes") is None:
                raise Unavailable("successful trial is missing elapsed minutes")
            values.append(row["elapsedMinutes"])
        else:
            values.append(row["personSentences"])
    return whole_number(statistics.median(values))


def duration_minutes(bound, spec, row_id):
    if not isinstance(spec, dict):
        raise Unavailable(f"missing timing for {row_id}")
    document = pinned_json(bound["admission"], bound["manifest"], spec.get("evidence"))
    start, end = document.get("startSeconds"), document.get("endSeconds")
    if (document.get("row") != row_id or document.get("stackName") != bound["stack"]
            or isinstance(start, bool) or isinstance(end, bool)
            or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
            or end < start):
        raise Unavailable(f"timing evidence mismatch for {row_id}")
    return whole_number((end - start) / 60)


def measurement(bound, row_id, value, extra):
    identity = bound["identity"]
    return {
        "schema": "honua.release-promise-receipt/v1", "row": row_id, "status": "PASS",
        "serverSha": identity["serverSha"], "imageDigest": identity["imageDigest"],
        "platformLockDigest": identity["platformLockDigest"], "observedAt": bound["observed_at"],
        "stackName": bound["stack"], "value": value, "unit": outcomes.unit_for(row_id),
        "assertions": {"live": True, "releaseNotesReported": True, **extra},
    }


def outcome_documents(bound):
    rows = bound["rows"]
    documents = []
    for promise, source in SENTENCE_SOURCE.items():
        for unit, modality, kind in (("sentences", "agent", "sentences"), ("clicks", "browser", "clicks")):
            group = require_denominator(rows, source, modality)
            value = median_of(rows, source, (modality, None, kind))
            documents.append(measurement(bound, f"{unit}-{promise}", value, {
                "censored": sum(row["status"] == "censored" for row in group),
                "planned": len(group),
                "successful": sum(row["status"] == "success" for row in group)}))
    for row_id, (source, modality, arm) in MINUTE_SOURCE.items():
        documents.append(measurement(
            bound, row_id, median_of(rows, source, (modality, arm, "minutes")), {}))
    expected = {row["id"] for row in contract_rows(OUTCOME_WORKFLOW)}
    if {doc["row"] for doc in documents} != expected:
        raise Unavailable("outcome coverage does not match the contract")
    return documents


def ai_documents(bound):
    rows, identity = bound["rows"], bound["identity"]
    documents = []
    for row_id, source in AI_SOURCE.items():
        successes = selected(rows, source, ("agent", "D", "success"))
        require_denominator(rows, source, "agent")
        if not successes:
            raise Unavailable(f"no successful arm-D trial for {row_id}")
        documents.append({
            "schema": "honua.release-promise-receipt/v1", "row": row_id, "status": "PASS",
            "serverSha": identity["serverSha"], "imageDigest": identity["imageDigest"],
            "platformLockDigest": identity["platformLockDigest"], "observedAt": bound["observed_at"],
            "stackName": bound["stack"],
            "assertions": {"live": True, "journey": source, "successfulArmD": len(successes)},
        })
    documents.append({
        "schema": "honua.release-promise-receipt/v1", "row": "ai-injection", "status": "PASS",
        "serverSha": identity["serverSha"], "imageDigest": identity["imageDigest"],
        "platformLockDigest": identity["platformLockDigest"], "observedAt": bound["observed_at"],
        "stackName": bound["stack"],
        "assertions": {"live": True, "hostileInputBlocked": True,
                       "channels": list(INJECTION_CHANNELS)},
    })
    return documents


def receipt_paths(workflow):
    paths = [Path(row["receipt"]) for row in contract_rows(workflow)]
    if workflow == OUTCOME_WORKFLOW:
        paths.append(Path("release-notes.md"))
    return paths


def clear(paths):
    for path in paths:
        path.unlink(missing_ok=True)


def build(mode, candidate):
    import datetime as dt
    admission = admission_dir()
    proof = installed_proof(admission, candidate)
    identity = identity_of(candidate)
    cohort_identity, rows = load_rows(admission)
    if cohort_identity != identity:
        raise Unavailable("cohort candidate does not match the producer inputs")
    manifest, stack = require_harness(admission, proof, identity)
    observed_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    bound = {"admission": admission, "manifest": manifest, "proof": proof, "stack": stack,
             "identity": identity, "observed_at": observed_at, "rows": rows}
    require_readbacks(admission, manifest, stack, identity)
    require_injection(bound)
    require_censored(rows)
    if mode == "ai":
        return ai_documents(bound), None
    if mode == "outcomes":
        documents = outcome_documents(bound)
        notes = "".join(outcomes.notes_line(doc) + "\n" for doc in sorted(documents, key=lambda doc: doc["row"]))
        return documents, notes
    raise Unavailable("unknown producer mode")


def write_outputs(documents, notes, workflow):
    paths = receipt_paths(workflow)
    directory = Path(tempfile.mkdtemp(prefix=".honua-producer-", dir="."))
    try:
        written = []
        for document in documents:
            target = directory / f"{document['row']}.json"
            target.write_text(json.dumps(document, indent=2) + "\n")
            written.append(target)
        if notes is not None:
            notes_path = directory / "release-notes.md"
            notes_path.write_text(notes)
            written.append(notes_path)
        if sorted(path.name for path in written) != sorted(path.name for path in paths):
            raise Unavailable("refusing to publish a partial receipt set")
        clear(paths)
        for path in written:
            path.replace(Path(path.name))
    except Exception:
        clear(paths)
        raise
    finally:
        for child in directory.iterdir():
            child.unlink(missing_ok=True)
        directory.rmdir()


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    workflow = {"ai": AI_WORKFLOW, "outcomes": OUTCOME_WORKFLOW}.get(mode)
    if workflow is None:
        print("usage: authenticated_producers.py ai|outcomes", file=sys.stderr)
        return 2
    paths = receipt_paths(workflow)
    clear(paths)
    try:
        candidate = candidate_from_environment()
        documents, notes = build(mode, candidate)
        write_outputs(documents, notes, workflow)
    except Unavailable as error:
        clear(paths)
        print(f"Producer unavailable: {error}", file=sys.stderr)
        return 1
    print(f"wrote {len(documents)} receipts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

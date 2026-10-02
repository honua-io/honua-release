#!/usr/bin/env python3
"""Verify an upgrade edge against the exact bytes of platform locks A and B."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
PHASES = ("install", "candidate", "rollback")
# DbUp journals an embedded script by its manifest resource name. Restricting names to this
# alphabet keeps the canonical bytes free of JSON escapes, so every writer emits one encoding.
JOURNAL_SCRIPT = re.compile(r"[A-Za-z0-9_.-]+\.sql\Z")


class BindingError(ValueError):
    pass


def canonical_journal_bytes(scripts: Any) -> bytes:
    """honua.migration-journal/v1: the exact bytes `migrationJournalSha256` is the SHA-256 of.

    The journal is the set of `public.schema_versions.scriptname` values. Its canonical form is
    a JSON array of those names, each matching JOURNAL_SCRIPT, without duplicates, sorted by
    ordinal (byte) order, serialized with no whitespace (`["a.sql","b.sql"]`), ASCII, and
    terminated by exactly one LF. Anything else (an empty set, a non-string, a duplicate entry,
    a name that would need escaping) is refused, never normalized.
    """
    if not isinstance(scripts, (list, tuple)) or not scripts:
        raise BindingError("migration journal must be a non-empty list of script names")
    for script in scripts:
        if not isinstance(script, str) or not JOURNAL_SCRIPT.fullmatch(script):
            raise BindingError(f"migration journal entry {script!r} is not a canonical script name")
    if len(set(scripts)) != len(scripts):
        duplicates = sorted({script for script in scripts if scripts.count(script) > 1})
        raise BindingError("migration journal repeats " + ", ".join(duplicates))
    return (json.dumps(sorted(scripts), separators=(",", ":")) + "\n").encode("ascii")


def journal_digest(scripts: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_journal_bytes(scripts)).hexdigest()


def observed_journal(text: str) -> list[str]:
    """Parse a database's journal as JSON (e.g. psql `json_agg` output); formatting is irrelevant."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BindingError(f"observed migration journal is not JSON ({exc})") from exc
    if not isinstance(value, list):
        raise BindingError("observed migration journal must be a JSON array (an empty table is not a journal)")
    return value


def bytes_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BindingError(f"{path}: lock must be an object")
    return value


def artifact(lock: dict[str, Any], component: str, kind: str) -> dict[str, Any]:
    artifacts = (((lock.get("components") or {}).get(component) or {}).get("artifacts") or [])
    matches = [item for item in artifacts if isinstance(item, dict) and item.get("kind") == kind]
    if len(matches) != 1:
        raise BindingError(f"lock must contain exactly one {component} {kind} artifact")
    return matches[0]


def image_binding(lock: dict[str, Any], architecture: str) -> dict[str, str]:
    item = artifact(lock, "honua-server", "image")
    digest = (item.get("platformDigests") or {}).get(architecture) or item.get("digest")
    if not DIGEST.fullmatch(str(digest)):
        raise BindingError(f"honua-server has no exact {architecture} digest")
    coordinate = str(item.get("coordinate") or "").removeprefix("oci://").split("@", 1)[0]
    if not coordinate or ":" in coordinate.rsplit("/", 1)[-1]:
        raise BindingError("server coordinate must be an untagged repository; tags are display metadata only")
    return {"reference": f"{coordinate}@{digest}", "digest": digest, "sourceRevision": item["sourceRevision"]}


def chart_binding(lock: dict[str, Any]) -> dict[str, str]:
    item = artifact(lock, "honua-helm", "oci-chart")
    for field in ("digest", "sha256"):
        if not DIGEST.fullmatch(str(item.get(field, ""))):
            raise BindingError(f"honua-helm {field} must be exact")
    version = str(item.get("version") or "")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
        raise BindingError("honua-helm version must be exact SemVer")
    return {"coordinate": str(item["coordinate"]), "version": version, "digest": item["digest"], "sha256": item["sha256"]}


def _normalize_image_id(value: str) -> str:
    digest = value.rsplit("@", 1)[-1]
    if not DIGEST.fullmatch(digest):
        raise BindingError(f"observed imageID is not digest-bound: {value!r}")
    return digest


def verify(prior_path: Path, candidate_path: Path, evidence: dict[str, Any]) -> dict[str, Any]:
    prior, candidate = load(prior_path), load(candidate_path)
    architecture = str(evidence.get("architecture") or "")
    expected = {
        "install": image_binding(prior, architecture),
        "candidate": image_binding(candidate, architecture),
        "rollback": image_binding(prior, architecture),
    }
    expected_charts = {"install": chart_binding(prior), "candidate": chart_binding(candidate)}
    if evidence.get("candidateLockDigest") != bytes_digest(candidate_path):
        raise BindingError("candidate lock digest does not match exact lock bytes")
    if evidence.get("priorLockDigest") != bytes_digest(prior_path):
        raise BindingError("prior lock digest does not match exact lock bytes")
    signatures = evidence.get("lockValidation") or {}
    if signatures.get("prior") != "verified" or signatures.get("candidate") != "verified":
        raise BindingError("both platform-lock signatures must be verified")
    if evidence.get("charts") != expected_charts:
        raise BindingError("observed install/candidate chart version or package bytes are not lock-bound")
    phases = evidence.get("phases") or {}
    for phase in PHASES:
        observed = phases.get(phase) or {}
        if _normalize_image_id(str(observed.get("imageID") or "")) != expected[phase]["digest"]:
            raise BindingError(f"{phase} imageID does not match the lock's platform digest")
        runtime = observed.get("runtimeIdentity") or {}
        if runtime.get("imageDigest") != expected[phase]["digest"]:
            raise BindingError(f"{phase} runtime image identity mismatch")
        if runtime.get("sourceRevision") != expected[phase]["sourceRevision"]:
            raise BindingError(f"{phase} runtime source identity mismatch")
        if expected[phase]["sourceRevision"][:8] not in str(runtime.get("observedVersion") or ""):
            raise BindingError(f"{phase} runtime version does not identify the expected source")
        lock_digest = evidence["priorLockDigest"] if phase in ("install", "rollback") else evidence["candidateLockDigest"]
        if runtime.get("platformLockDigest") != lock_digest:
            raise BindingError(f"{phase} runtime lock identity mismatch")
    declared = str((((candidate.get("components") or {}).get("honua-server") or {}).get("schemaVersions") or {}).get("database") or "")
    observed_schema = str((evidence.get("schema") or {}).get("observed") or "")
    if not declared or observed_schema != declared:
        raise BindingError(f"observed schema {observed_schema!r} is not exactly candidate-declared {declared!r}")
    journal = evidence.get("schema") or {}
    declared_journal = (((candidate.get("components") or {}).get("honua-server") or {}).get("migrationJournalSha256"))
    if not DIGEST.fullmatch(str(declared_journal)):
        raise BindingError("candidate lock declares no exact migrationJournalSha256")
    # The digest is recomputed from the observed script names; a reported hash alone proves nothing.
    observed_journal_digest = journal_digest(journal.get("journal"))
    if (journal.get("declaredJournalSha256") != declared_journal or journal.get("journalSha256") != observed_journal_digest
            or observed_journal_digest != declared_journal):
        raise BindingError("migration journal does not match the candidate-declared migration set")
    rollback = phases["rollback"]
    if rollback.get("databaseSchema") != declared:
        raise BindingError("rollback did not retain candidate schema B")
    if not all((evidence.get("seededData") or {}).get(key) is True for key in ("checksumsMatched", "rollbackQueryPassed")):
        raise BindingError("seeded data or rollback query proof failed")
    return {
        "schema": "honua.upgrade-lock-receipt/v1",
        "fromLockDigest": evidence["priorLockDigest"],
        "toLockDigest": evidence["candidateLockDigest"],
        "lockValidation": signatures,
        "charts": expected_charts,
        "phases": phases,
        "schemaBinding": journal,
        "seededData": evidence["seededData"],
        "timings": evidence.get("timings") or {},
        "workflow": evidence.get("workflow") or {},
        "classification": "exact-lock-upgrade-rollback-certified",
    }


def journal_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="upgrade_lock_binding.py journal-digest",
        description="Write the canonical migration journal and print its sha256 digest.")
    parser.add_argument("journal", type=Path, help="JSON array of observed schema_versions script names")
    parser.add_argument("--canonical-output", type=Path, help="where to write the canonical journal bytes")
    args = parser.parse_args(argv)
    try:
        scripts = observed_journal(args.journal.read_text(encoding="utf-8"))
        canonical = canonical_journal_bytes(scripts)
        if args.canonical_output:
            args.canonical_output.write_bytes(canonical)
    except (OSError, UnicodeDecodeError, BindingError) as exc:
        print(f"migration journal: FAIL: {exc}", file=sys.stderr)
        return 1
    print("sha256:" + hashlib.sha256(canonical).hexdigest())
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["journal-digest"]:
        return journal_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prior-lock", required=True, type=Path)
    parser.add_argument("--candidate-lock", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        receipt = verify(args.prior_lock, args.candidate_lock, evidence)
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, KeyError, json.JSONDecodeError, BindingError) as exc:
        print(f"exact-lock upgrade: FAIL: {exc}")
        return 1
    print("exact-lock upgrade: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

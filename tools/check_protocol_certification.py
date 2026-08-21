#!/usr/bin/env python3
"""Fail-closed evaluator for honua.protocol-certification/v1 ledgers.

The producer owns collection. This tool owns release semantics: normalized cell uniqueness,
maturity/addressability honesty, tier scope, candidate binding, freshness, and required outcomes.
It intentionally uses only the Python standard library so inability to install a schema package
cannot turn a release gate into a false pass.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_ID = "honua.protocol-certification/v1"
MATURITIES = {"supported", "preview", "experimental", "roadmap", "deprecated", "internal"}
RESULTS = {"pass", "fail", "skip", "not-addressable"}
TIERS = {"pr", "nightly", "release"}
TIER_RANK = {"pr": 0, "nightly": 1, "release": 2}
FACETS = {
    "positive", "negative", "boundary", "auth", "pagination", "limit", "crs-axis",
    "media-schema", "cancellation-idempotency", "recovery", "metadata", "range-efficiency",
}
CELL_FIELDS = {
    "capability_key", "surface", "operation", "maturity", "canonical_client", "client_lane",
    "client_version", "deployment_target", "required_tier", "licensed",
    "addressable_by_client", "addressability_reason", "result", "skip_reason",
    "scenario_facets", "contract_revision", "auth_policy_revision", "source_sha",
    "image_digest", "fixture_revision", "evidence_uri", "started_at", "completed_at",
}
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REQUIREMENTS_SCHEMA_ID = "honua.protocol-certification-requirements/v1"
REQUIREMENTS_PATH = Path(__file__).resolve().parents[1] / "certification" / "protocol-certification-requirements.v1.json"
REQUIREMENT_FIELDS = {
    "capability_key", "surface", "operation", "maturity", "canonical_client", "client_lane",
    "client_version", "deployment_target", "required_tier", "licensed", "addressable_by_client",
    "addressability_reason", "scenario_facets", "contract_revision", "auth_policy_revision",
}


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def load_ledger(path: str | Path) -> tuple[dict | None, str | None]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"ledger unavailable or invalid JSON: {exc}"
    if not isinstance(value, dict):
        return None, "ledger root must be an object"
    return value, None


def _requirement_signature(value: dict) -> tuple[object, ...]:
    return tuple(
        tuple(value[field]) if field == "scenario_facets" and isinstance(value.get(field), list)
        else value.get(field)
        for field in sorted(REQUIREMENT_FIELDS)
    )


def _in_scope(cell: dict, tier: str) -> bool:
    maturity = cell.get("maturity")
    if maturity in {"roadmap", "experimental", "internal"}:
        return False
    if tier == "release":
        return maturity in {"supported", "deprecated"}
    if maturity not in {"supported", "preview", "deprecated"}:
        return False
    required_tier = cell.get("required_tier")
    return required_tier in TIER_RANK and TIER_RANK[required_tier] <= TIER_RANK[tier]


def evaluate(
    ledger: dict | None,
    tier: str,
    *,
    expected_source_sha: str | None = None,
    expected_image_digest: str | None = None,
    now: datetime | None = None,
    requirements: dict | None = None,
) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    findings: list[dict[str, str]] = []

    def fail(check: str, why: str) -> None:
        findings.append({"check": check, "status": "fail", "why": why})

    if tier not in TIERS:
        fail("tier", f"unknown tier {tier!r}; expected one of {sorted(TIERS)}")
    if ledger is None:
        fail("ledger", "certification ledger is unavailable")
        return _report(tier, [], findings)
    if ledger.get("schema") != SCHEMA_ID:
        fail("schema", f"schema must be {SCHEMA_ID!r}")
    if requirements is None:
        requirements, requirements_error = load_ledger(REQUIREMENTS_PATH)
        if requirements_error:
            fail("requirements", f"owned requirements unavailable: {requirements_error}")
            requirements = {}
    if requirements.get("schema") != REQUIREMENTS_SCHEMA_ID:
        fail("requirements", f"owned requirements schema must be {REQUIREMENTS_SCHEMA_ID!r}")
    owned_revision = requirements.get("revision")
    owned_complete = requirements.get("complete")
    owned_rows = requirements.get("requirements")
    if not isinstance(owned_revision, str) or not owned_revision.strip():
        fail("requirements", "owned requirements must identify a non-empty revision")
    if ledger.get("requirements_revision") != owned_revision:
        fail("requirements_revision", "ledger requirements revision does not match repository-owned requirements")
    if not isinstance(owned_complete, bool):
        fail("requirements", "owned requirements complete flag must be boolean")
    if ledger.get("requirements_complete") != owned_complete:
        fail("requirements_complete", "ledger completeness claim does not match repository-owned requirements")
    if tier == "release" and owned_complete is not True:
        fail("requirements_complete", "release certification requires a complete repository-owned denominator")
    if not isinstance(owned_rows, list) or not owned_rows:
        fail("requirements", "owned requirements must contain a non-empty requirements array")
        owned_rows = []

    generated_at = _timestamp(ledger.get("generated_at"))
    if generated_at is None:
        fail("generated_at", "generated_at must be a timezone-aware ISO-8601 timestamp")
    elif generated_at > now:
        fail("generated_at", "generated_at cannot be in the future")

    candidate = ledger.get("candidate")
    if not isinstance(candidate, dict):
        fail("candidate", "candidate must be an object")
        candidate = {}
    candidate_sha = candidate.get("source_sha")
    candidate_digest = candidate.get("image_digest")
    cut_at = _timestamp(candidate.get("cut_at"))
    if not isinstance(candidate_sha, str) or not SHA_RE.fullmatch(candidate_sha):
        fail("candidate.source_sha", "candidate source_sha must be 7-40 lowercase hex characters")
    if not isinstance(candidate_digest, str) or not DIGEST_RE.fullmatch(candidate_digest):
        fail("candidate.image_digest", "candidate image_digest must be a sha256 digest")
    if cut_at is None:
        fail("candidate.cut_at", "candidate cut_at must be a timezone-aware ISO-8601 timestamp")
    elif cut_at > now:
        fail("candidate.cut_at", "candidate cut_at cannot be in the future")
    if expected_source_sha and candidate_sha != expected_source_sha:
        fail("candidate.source_sha", f"ledger candidate {candidate_sha!r} does not match expected {expected_source_sha!r}")
    if expected_image_digest and candidate_digest != expected_image_digest:
        fail("candidate.image_digest", f"ledger candidate {candidate_digest!r} does not match expected {expected_image_digest!r}")

    cells = ledger.get("cells")
    if not isinstance(cells, list) or not cells:
        fail("cells", "cells must be a non-empty array")
        return _report(tier, [], findings)

    owned_signatures = {_requirement_signature(row) for row in owned_rows if isinstance(row, dict)}
    ledger_signatures = {_requirement_signature(row) for row in cells if isinstance(row, dict)}
    if len(owned_signatures) != len(owned_rows):
        fail("requirements", "owned requirements contain invalid or duplicate normalized cells")
    if len(ledger_signatures) != len(cells):
        fail("requirements_denominator", "ledger contains invalid or duplicate requirement definitions")
    missing_requirements = owned_signatures - ledger_signatures
    unexpected_requirements = ledger_signatures - owned_signatures
    if missing_requirements or unexpected_requirements:
        fail(
            "requirements_denominator",
            f"ledger cell definitions differ from owned requirements "
            f"(missing={len(missing_requirements)}, unexpected={len(unexpected_requirements)})",
        )

    seen: set[tuple[object, ...]] = set()
    supported_groups: dict[tuple[object, ...], list[dict]] = {}
    scoped: list[dict] = []
    for index, raw in enumerate(cells):
        prefix = f"cells[{index}]"
        if not isinstance(raw, dict):
            fail(prefix, "cell must be an object")
            continue
        missing = sorted(CELL_FIELDS - raw.keys())
        extra = sorted(raw.keys() - CELL_FIELDS)
        if missing:
            fail(prefix, f"missing required fields: {', '.join(missing)}")
        if extra:
            fail(prefix, f"unknown fields: {', '.join(extra)}")
        if missing:
            continue

        key = (
            raw["surface"], raw["operation"], raw["canonical_client"],
            raw["client_version"], raw["deployment_target"],
        )
        if key in seen:
            fail(prefix, f"duplicate normalized cell key: {key}")
        seen.add(key)

        if raw["maturity"] not in MATURITIES:
            fail(prefix, f"unknown maturity {raw['maturity']!r}")
        if raw["required_tier"] not in TIERS:
            fail(prefix, f"unknown required_tier {raw['required_tier']!r}")
        if raw["result"] not in RESULTS:
            fail(prefix, f"unknown result {raw['result']!r}")
        if not isinstance(raw["licensed"], bool) or not isinstance(raw["addressable_by_client"], bool):
            fail(prefix, "licensed and addressable_by_client must be booleans")
        facets = raw["scenario_facets"]
        if not isinstance(facets, list) or not facets or len(facets) != len(set(facets)):
            fail(prefix, "scenario_facets must be a non-empty unique array")
        elif unknown := sorted(set(facets) - FACETS):
            fail(prefix, f"unknown scenario facets: {', '.join(unknown)}")

        if raw["addressable_by_client"]:
            if raw["result"] == "not-addressable":
                fail(prefix, "addressable cell cannot have result=not-addressable")
        else:
            if raw["result"] != "not-addressable":
                fail(prefix, "non-addressable cell must have result=not-addressable")
            if not isinstance(raw["addressability_reason"], str) or not raw["addressability_reason"].strip():
                fail(prefix, "non-addressable cell requires addressability_reason")
        if raw["result"] == "skip" and (not isinstance(raw["skip_reason"], str) or not raw["skip_reason"].strip()):
            fail(prefix, "skipped cell requires skip_reason")

        if raw["maturity"] in {"supported", "deprecated"}:
            group = (raw["capability_key"], raw["surface"], raw["operation"], raw["deployment_target"])
            supported_groups.setdefault(group, []).append(raw)

        if not _in_scope(raw, tier):
            continue
        scoped.append(raw)
        if raw["source_sha"] != candidate_sha:
            fail(prefix, f"cell source_sha {raw['source_sha']!r} does not match ledger candidate")
        if raw["image_digest"] != candidate_digest:
            fail(prefix, f"cell image_digest {raw['image_digest']!r} does not match ledger candidate")
        if not raw["addressable_by_client"]:
            continue
        if raw["result"] != "pass":
            fail(prefix, f"required addressable {tier} cell result is {raw['result']!r}, expected 'pass'")

        completed = _timestamp(raw["completed_at"])
        started = _timestamp(raw["started_at"])
        if completed is None or started is None or completed < started:
            fail(prefix, "required cell needs valid started_at <= completed_at timestamps")
        required_provenance = ("source_sha", "fixture_revision", "evidence_uri")
        for field in required_provenance:
            if not isinstance(raw[field], str) or not raw[field].strip():
                fail(prefix, f"required cell needs non-empty {field}")
        if started is not None and started > now:
            fail(prefix, "started_at cannot be in the future")
        if completed is not None and completed > now:
            fail(prefix, "completed_at cannot be in the future")

        if completed is not None and tier == "nightly" and (now - completed).total_seconds() > 168 * 3600:
            fail(prefix, "nightly evidence is older than 7 days")
        if completed is not None and raw["licensed"] and (now - completed).total_seconds() > 72 * 3600:
            fail(prefix, "licensed evidence is older than 72 hours")
        if tier == "release":
            if started is not None and cut_at is not None and started < cut_at:
                fail(prefix, "release evidence started before candidate cut")

    for group, rows in supported_groups.items():
        if tier == "release" and not any(row.get("addressable_by_client") for row in rows):
            fail("addressability", f"supported operation has no addressable canonical client: {group}")

    if not scoped:
        fail("denominator", f"no certification cells are in scope for tier {tier!r}")
    return _report(tier, scoped, findings)


def _report(tier: str, scoped: list[dict], findings: list[dict]) -> dict:
    counts = {result: 0 for result in RESULTS}
    for cell in scoped:
        result = cell.get("result")
        if result in counts:
            counts[result] += 1
    return {
        "schema": "honua.protocol-certification-report/v1",
        "tier": tier,
        "overall_status": "fail" if findings else "pass",
        "required_cells": len(scoped),
        "counts": counts,
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--tier", required=True, choices=sorted(TIERS))
    parser.add_argument("--expected-source-sha")
    parser.add_argument("--expected-image-digest")
    parser.add_argument("--now", help="ISO-8601 evaluation time; defaults to current UTC")
    parser.add_argument("--report", help="write the machine-readable decision report here")
    args = parser.parse_args(argv)

    ledger, load_error = load_ledger(args.matrix)
    now = _timestamp(args.now) if args.now else datetime.now(timezone.utc)
    if args.now and now is None:
        raise SystemExit("--now must be a timezone-aware ISO-8601 timestamp")
    report = evaluate(
        ledger,
        args.tier,
        expected_source_sha=args.expected_source_sha,
        expected_image_digest=args.expected_image_digest,
        now=now,
    )
    if load_error:
        report["findings"].insert(0, {"check": "ledger", "status": "fail", "why": load_error})
        report["overall_status"] = "fail"
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["overall_status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

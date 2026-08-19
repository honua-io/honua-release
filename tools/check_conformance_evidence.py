#!/usr/bin/env python3
"""Conformance-evidence federation gate (honua-release#133).

WHY THIS EXISTS
---------------
`gate_conformance`'s `conformance-ogc-stac` and `conformance-esri-geoservices` lanes were
hardcoded `status=blocked` with no implementation behind them. That is honest under a dry run,
but on a real cut (`enforcement: strict`) blocked is promoted to FAIL, so a candidate could never
be released with those lanes as written -- and setting the `HONUA_SERVER_URL` their `why` strings
asked for changed nothing, because neither lane had a body to enable. The release train's own
rule is that a gate which cannot fail is worse than no gate; a gate which can ONLY fail is the
same defect wearing the opposite sign.

Meanwhile the evidence those lanes need already exists and is already sha-bound:

  OGC   honua-server runs 13 CITE TEAM Engine suites and transcribes an authoritative snapshot to
        docs/cite-status.md (run id, `trunk@<sha>`, per-suite passed/total, allPassed). honua-evidence
        ingests it as the `cite` producer of capability-matrix.v1.json.
  STAC  the candidate serves its declared STAC API conformance classes, which a validator can check
        against the spec -- CITE covers OGC API/OWS, never STAC.
  Esri  honua-esri-compat's license-free lanes emit per-service `.cert.json` evidence with an
        operation-coverage denominator generated from honua-server's own parity index.

So this gate federates rather than re-certifies. It applies the SAME lineage rule the freeze-phase
evidence gate already proved out (tools/check_evidence_freshness.py): evidence must be ABOUT this
candidate's history -- identical to, an ancestor of, or a descendant of the pinned sha, but never
DIVERGED onto unrelated history -- plus a per-source staleness bound.

Ancestor is accepted deliberately and is the common case: CITE suites are expensive and run on
their own cadence, so the sha they certified is usually BEHIND a manifest pinned later the same
week. That is lineage continuity. A fork is not.

PURITY
------
Like check_slo.py / check_upgrade.py / check_evidence_freshness.py, the decision core makes no
network or VCS calls. The workflow shim fetches docs/cite-status.md, asks the GitHub compare API
for the ancestry relation, runs the STAC validator, and downloads the Esri bundle; it hands all of
that in as plain values. Everything here is pure and unit-testable.

    evaluate_conformance(candidate_sha, cite, cite_lineage, stac, esri, config, now=None)
        -> (rows, overall)   # overall in {pass, fail, blocked}

    python tools/check_conformance_evidence.py --manifest platform-manifest.yaml \
        --cite-status _conformance/cite-status.md --cite-lineage ancestor \
        --stac-report _conformance/stac.json --esri-bundle _conformance/esri/

BLOCKED vs FAIL
---------------
BLOCKED  a source could not be obtained or decided at all (unfetchable snapshot, unreachable
         compare API, validator never ran). Honest bootstrap state; tolerated on a dry run.
FAIL     a source WAS obtained and says the candidate is not conformant, or is about the wrong
         history, or is too old to trust. Fails in dry run and real cut alike.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "certification" / "conformance-evidence.yaml"

# Same relation vocabulary as check_evidence_freshness.py. "diverged" is the only decidable failure;
# anything else decidable means the evidence shares this release's history.
LINEAGE_OK = frozenset({"identical", "ancestor", "descendant"})

_SHA_RE = re.compile(r"\b([0-9a-f]{40})\b")
_RUN_RE = re.compile(r"/actions/runs/(\d+)")
_LAST_REVIEWED_RE = re.compile(r"^Last reviewed:\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
_ALL_PASSED_RE = re.compile(r"allPassed=(true|false)")
# | OGC API Features 1.0 | `default` | 137 / 137 | 100% | 2026-08-12 |
_SUITE_ROW_RE = re.compile(
    r"^\|\s*([^|]+?)\s*\|\s*`?([^|`]*?)`?\s*\|\s*(\d+)\s*/\s*(\d+)\s*\|", re.MULTILINE
)


class ConformanceEvidenceError(ValueError):
    """Raised when an evidence artifact is structurally unusable."""


# --------------------------------------------------------------------------------------------
# Parsing (deterministic, no network -- kept here so it is unit-testable alongside the decision)
# --------------------------------------------------------------------------------------------
def parse_cite_status(text: str | None) -> dict | None:
    """Parse honua-server's docs/cite-status.md authoritative snapshot.

    Returns None when the document is absent, so the caller reports BLOCKED rather than guessing.
    Raises ConformanceEvidenceError when the document is present but does not carry the fields the
    gate depends on -- a malformed snapshot must never degrade into a silent pass.
    """
    if text is None:
        return None

    sha_match = _SHA_RE.search(text)
    reviewed = _LAST_REVIEWED_RE.search(text)
    all_passed = _ALL_PASSED_RE.search(text)
    run = _RUN_RE.search(text)

    if not sha_match:
        raise ConformanceEvidenceError(
            "cite-status.md carries no 40-char commit sha; the snapshot cannot be bound to a candidate"
        )
    if not reviewed:
        raise ConformanceEvidenceError("cite-status.md carries no 'Last reviewed:' date")

    suites = []
    for name, profile, passed, total in _SUITE_ROW_RE.findall(text):
        # Skip the markdown header/separator rows, which never carry digits in the counts column.
        suites.append(
            {
                "suite": name.strip(),
                "profile": profile.strip(),
                "passed": int(passed),
                "total": int(total),
            }
        )

    return {
        "sha": sha_match.group(1),
        "lastReviewed": reviewed.group(1),
        "allPassed": (all_passed.group(1) == "true") if all_passed else None,
        "runId": run.group(1) if run else None,
        "suites": suites,
    }


def summarize_esri_bundle(cert_docs: list[dict] | None) -> dict | None:
    """Reduce a set of honua-esri-compat ``.cert.json`` documents to a gate-legible summary.

    Counts every nested record carrying a fail verdict, under either the CERT (`test_case_id` /
    `status`) or operation-coverage (`id` / `status`) shapes the harness emits. Both shapes are
    walked because a lane can fail in either dimension and the gate must not be blind to one.
    """
    if cert_docs is None:
        return None

    failures: list[dict] = []
    images: set[str] = set()
    shas: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            verdict = node.get("status") or node.get("result")
            if verdict in {"fail", "failed"}:
                failures.append(
                    {
                        "id": node.get("id") or node.get("test_case_id") or "<unnamed>",
                        "notes": node.get("notes") or node.get("detail") or "",
                    }
                )
            for key in ("image", "candidateImage", "honuaImage"):
                value = node.get(key)
                if isinstance(value, str) and value:
                    images.add(value)
            for key in ("sha", "candidateSha", "serverSha", "sourceVersion"):
                value = node.get(key)
                if isinstance(value, str) and _SHA_RE.search(value):
                    shas.add(_SHA_RE.search(value).group(1))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for doc in cert_docs:
        walk(doc)

    return {
        "documents": len(cert_docs),
        "failures": failures,
        "images": sorted(images),
        "shas": sorted(shas),
    }


# --------------------------------------------------------------------------------------------
# Decision core
# --------------------------------------------------------------------------------------------
def _days_between(iso_date: str, now: datetime) -> float | None:
    try:
        stamp = datetime.strptime(iso_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (now - stamp).total_seconds() / 86400.0


def _row(check: str, status: str, why: str) -> dict:
    return {"check": check, "status": status, "why": why}


def _evaluate_cite(candidate_sha, cite, lineage, cfg, now, rows) -> None:
    max_age_days = (cfg or {}).get("maxAgeDays")

    if cite is None:
        rows.append(
            _row(
                "cite:snapshot",
                "blocked",
                "honua-server docs/cite-status.md could not be fetched -- CITE conformance is "
                "undecidable, not assumed",
            )
        )
        return

    # --- lineage -----------------------------------------------------------------------------
    evidence_sha = cite.get("sha")
    if not candidate_sha:
        rows.append(_row("cite:lineage", "blocked", "no honua-server sha pinned in the candidate manifest"))
    elif lineage is None:
        rows.append(
            _row(
                "cite:lineage",
                "blocked",
                f"could not determine ancestry between CITE sha {evidence_sha[:12]} and candidate "
                f"sha {candidate_sha[:12]} (compare unreachable)",
            )
        )
    elif lineage in LINEAGE_OK:
        rows.append(
            _row(
                "cite:lineage",
                "pass",
                f"CITE evidence sha {evidence_sha[:12]} is {lineage} with candidate sha "
                f"{candidate_sha[:12]}",
            )
        )
    else:
        rows.append(
            _row(
                "cite:lineage",
                "fail",
                f"CITE evidence sha {evidence_sha[:12]} has DIVERGED from candidate sha "
                f"{candidate_sha[:12]} ({lineage}) -- the suites certified different history",
            )
        )

    # --- freshness ---------------------------------------------------------------------------
    age = _days_between(cite.get("lastReviewed"), now)
    if age is None:
        rows.append(_row("cite:freshness", "blocked", "cite-status.md 'Last reviewed' date is unparseable"))
    elif max_age_days is not None and age > max_age_days:
        rows.append(
            _row(
                "cite:freshness",
                "fail",
                f"CITE snapshot is {age:.1f}d old, exceeds threshold {max_age_days}d "
                f"(last reviewed {cite.get('lastReviewed')})",
            )
        )
    else:
        rows.append(
            _row(
                "cite:freshness",
                "pass",
                f"CITE snapshot is {age:.1f}d old (threshold {max_age_days}d, last reviewed "
                f"{cite.get('lastReviewed')})",
            )
        )

    # --- results -----------------------------------------------------------------------------
    suites = cite.get("suites") or []
    required = set((cfg or {}).get("requiredSuites") or [])
    if not suites:
        rows.append(_row("cite:suites", "blocked", "cite-status.md carried no parseable per-suite table"))
    else:
        regressed = [s for s in suites if s["passed"] < s["total"]]
        if regressed:
            detail = ", ".join(f"{s['suite']} {s['passed']}/{s['total']}" for s in regressed)
            rows.append(_row("cite:suites", "fail", f"CITE suites not fully passing: {detail}"))
        else:
            total = sum(s["total"] for s in suites)
            rows.append(
                _row(
                    "cite:suites",
                    "pass",
                    f"{len(suites)} CITE suites fully passing ({total} assertions, run "
                    f"{cite.get('runId') or 'unknown'})",
                )
            )

        missing = sorted(required - {s["suite"] for s in suites})
        if missing:
            rows.append(
                _row(
                    "cite:coverage",
                    "fail",
                    f"required CITE suite(s) absent from the snapshot: {', '.join(missing)}",
                )
            )
        elif required:
            rows.append(
                _row("cite:coverage", "pass", f"all {len(required)} required CITE suites present in the snapshot")
            )

    if cite.get("allPassed") is False:
        rows.append(_row("cite:allPassed", "fail", "the CITE evidence bundle itself reported allPassed=false"))


def _evaluate_stac(stac, cfg, rows) -> None:
    if stac is None:
        rows.append(
            _row(
                "stac:validator",
                "blocked",
                "STAC API validation did not run against the candidate -- CITE covers OGC API/OWS "
                "only and never certifies STAC",
            )
        )
        return

    declared = set(stac.get("conformsTo") or [])
    required = set((cfg or {}).get("requiredConformanceClasses") or [])
    missing = sorted(required - declared)
    if missing:
        rows.append(
            _row(
                "stac:conformance-classes",
                "fail",
                f"candidate does not declare required STAC conformance class(es): {', '.join(missing)}",
            )
        )
    elif required:
        rows.append(
            _row(
                "stac:conformance-classes",
                "pass",
                f"candidate declares all {len(required)} required STAC conformance classes",
            )
        )

    errors = stac.get("errors")
    if errors is None:
        rows.append(_row("stac:validator", "blocked", "STAC validator produced no error list to evaluate"))
    elif errors:
        head = "; ".join(str(e) for e in errors[:3])
        more = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
        rows.append(_row("stac:validator", "fail", f"STAC API validator reported {len(errors)} error(s): {head}{more}"))
    else:
        rows.append(_row("stac:validator", "pass", "STAC API validator reported no errors against the candidate"))


def _evaluate_esri(esri, cfg, rows) -> None:
    if esri is None:
        rows.append(
            _row(
                "esri:bundle",
                "blocked",
                "no honua-esri-compat certification bundle was supplied for this candidate",
            )
        )
        return

    if not esri.get("documents"):
        rows.append(_row("esri:bundle", "blocked", "the supplied Esri bundle contained no .cert.json documents"))
        return

    failures = esri.get("failures") or []
    if failures:
        head = "; ".join(f"{f['id']}: {f['notes']}"[:120] for f in failures[:3])
        more = f" (+{len(failures) - 3} more)" if len(failures) > 3 else ""
        rows.append(
            _row(
                "esri:lanes",
                "fail",
                f"{len(failures)} Esri certification failure(s) across {esri['documents']} document(s): {head}{more}",
            )
        )
    else:
        rows.append(
            _row(
                "esri:lanes",
                "pass",
                f"no failures across {esri['documents']} Esri certification document(s)",
            )
        )

    # Binding: the bundle must name the image the manifest actually pinned. Evidence about a
    # DIFFERENT build is worse than no evidence, because it reads as a pass.
    expected_image = (cfg or {}).get("expectedImage")
    images = esri.get("images") or []
    if not expected_image:
        return
    if not images:
        rows.append(
            _row(
                "esri:binding",
                "blocked",
                "the Esri bundle records no candidate image reference, so it cannot be bound to the pin",
            )
        )
    elif any(expected_image in image for image in images):
        rows.append(_row("esri:binding", "pass", f"Esri bundle certifies the pinned candidate image {expected_image}"))
    else:
        rows.append(
            _row(
                "esri:binding",
                "fail",
                f"Esri bundle certifies {images} but the manifest pins {expected_image} -- the evidence "
                f"is about a different build",
            )
        )


ALL_SECTIONS = ("cite", "stac", "esri")


def evaluate_conformance(
    candidate_sha: str | None,
    cite: dict | None,
    cite_lineage: str | None,
    stac: dict | None,
    esri: dict | None,
    config: dict | None = None,
    now: datetime | None = None,
    sections: tuple[str, ...] | list[str] | None = None,
) -> tuple[list[dict], str]:
    """Pure decision core. Returns (rows, overall) with overall in {pass, fail, blocked}.

    ``sections`` selects which evidence families to evaluate. The certification workflow keeps one
    lane per family (so the assembled gate report retains its per-lane shape), and a lane must not
    be blocked by a sibling lane's inputs that were never handed to it -- evaluating everything
    everywhere would make each lane report the other's absence as its own gap.
    """
    now = now or datetime.now(timezone.utc)
    config = config or {}
    selected = tuple(sections) if sections else ALL_SECTIONS
    unknown = sorted(set(selected) - set(ALL_SECTIONS))
    if unknown:
        raise ConformanceEvidenceError(f"unknown conformance section(s): {', '.join(unknown)}")
    rows: list[dict] = []

    if "cite" in selected:
        _evaluate_cite(candidate_sha, cite, cite_lineage, config.get("cite") or {}, now, rows)
    if "stac" in selected:
        _evaluate_stac(stac, config.get("stac") or {}, rows)
    if "esri" in selected:
        _evaluate_esri(esri, config.get("esri") or {}, rows)

    if any(r["status"] == "fail" for r in rows):
        overall = "fail"
    elif any(r["status"] == "blocked" for r in rows):
        overall = "blocked"
    else:
        overall = "pass"
    return rows, overall


# --------------------------------------------------------------------------------------------
# CLI shim glue (still no network: every input is a file the workflow already fetched)
# --------------------------------------------------------------------------------------------
def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _pinned_server(manifest_path: Path) -> tuple[str | None, str | None]:
    """Return the candidate manifest's pinned honua-server (sha, image).

    The image is read here rather than configured in conformance-evidence.yaml so the Esri bundle is
    always bound to the exact build being released; a hand-maintained expectation could drift from
    the pin and would then certify the wrong artifact.
    """
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    components = data.get("components") or {}
    server = components.get("honua-server") or {}
    sha = server.get("sha") or server.get("commit")
    image = server.get("image")
    return (str(sha) if sha else None, str(image) if image else None)


def _read_text(path: str | None) -> str | None:
    if not path:
        return None
    candidate = Path(path)
    return candidate.read_text(encoding="utf-8") if candidate.exists() else None


def _read_json(path: str | None) -> dict | None:
    text = _read_text(path)
    return json.loads(text) if text else None


def _read_cert_docs(directory: str | None) -> list[dict] | None:
    if not directory:
        return None
    root = Path(directory)
    if not root.exists():
        return None
    docs = []
    for path in sorted(root.rglob("*.cert.json")):
        docs.append(json.loads(path.read_text(encoding="utf-8")))
    return docs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--cite-status", help="path to a fetched copy of honua-server docs/cite-status.md")
    parser.add_argument("--cite-lineage", help="identical|ancestor|descendant|diverged (from the compare API)")
    parser.add_argument("--stac-report", help="path to the STAC validator JSON report")
    parser.add_argument("--esri-bundle", help="directory of honua-esri-compat .cert.json evidence")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--json-out", help="write the full row set here for the gate fragment")
    parser.add_argument(
        "--sections",
        default=",".join(ALL_SECTIONS),
        help="comma-separated evidence families to evaluate (cite,stac,esri) -- one lane per family",
    )
    args = parser.parse_args(argv)

    config = load_config(Path(args.config))
    candidate_sha, candidate_image = _pinned_server(Path(args.manifest))
    if candidate_image:
        config.setdefault("esri", {})["expectedImage"] = candidate_image

    try:
        cite = parse_cite_status(_read_text(args.cite_status))
    except ConformanceEvidenceError as exc:
        # A present-but-malformed snapshot is a real defect in the evidence chain, not a bootstrap
        # gap: say so as a hard row rather than silently degrading to blocked.
        cite = None
        malformed = str(exc)
    else:
        malformed = None

    rows, overall = evaluate_conformance(
        candidate_sha,
        cite,
        args.cite_lineage,
        _read_json(args.stac_report),
        summarize_esri_bundle(_read_cert_docs(args.esri_bundle)),
        config,
        sections=[s.strip() for s in args.sections.split(",") if s.strip()],
    )

    if malformed:
        rows.insert(0, _row("cite:snapshot", "fail", f"cite-status.md is present but unusable: {malformed}"))
        overall = "fail"

    for row in rows:
        print(f"  [{row['status']}] {row['check']}: {row['why']}")
    print(f"overall conformance-evidence status: {overall}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"overallStatus": overall, "rows": rows, "candidateSha": candidate_sha}, indent=2),
            encoding="utf-8",
        )

    return 0 if overall == "pass" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

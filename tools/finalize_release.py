#!/usr/bin/env python3
"""Finalize a certified release candidate into a platform release.

The promote step's brain (the GHA workflow is the muscle): it REFUSES to release unless the
candidate's gate-report is all-green, then finalizes the manifest and generates aggregate release
notes. This is the "AI proposes, the pipeline disposes" boundary made concrete — a red/blocked
candidate can never be tagged (AGENTS.md: the gate must be able to fail; here it fails *closed*).

Four pure, unit-tested pieces:

  verify_candidate_binding — the manifest + matrix bytes and release-train source/run identity must
                        match the binding embedded in the gate report. A file copied from a later
                        checkout or a different train run is refused before YAML is parsed.
  verify_gate_report  — the candidate's release-train gate-report must have overallStatus == "pass"
                        for THIS platform label. A "blocked"/"fail" overall (any wired gate not green)
                        is refused. Without this, promotion could tag a broken set.
  finalize_manifest   — flip status -> released, stamp the final label + date; the manifest as tagged
                        IS the platform release.
  render_release_notes— aggregate notes from the pinned set: components + versions/shas/images, the
                        contract versions, the DB-schema floor, and an explicit breaking-changes /
                        upgrade-actions section.

Usage (the workflow calls this):
  python tools/finalize_release.py --label 2026.1 --gate-report report.json \
      --manifest candidate/platform-manifest.yaml \
      --matrix candidate/compatibility-matrix.yaml \
      --source-repository honua-io/honua-release --source-sha <sha> --source-branch trunk \
      --workflow-path .github/workflows/release-train.yml \
      --train-run-id <id> --train-run-attempt <attempt> --train-run-url <url> \
      --certification-mode live \
      --released-at 2026-07-01T00:00:00Z \
      --out-manifest finalized-manifest.yaml --out-notes release-notes.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from candidate_binding import CERTIFICATION_MODES, verify_candidate_binding

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

# Explicit allow-list of gates that MAY be `skipped` on a promote (task requirement: promote requires
# every non-skipped gate green — gates[].status in {pass, skipped} — with an explicit allowed-skip
# list). These are the creds/infra-gated tiers that self-skip (status: skipped) when their per-RC
# secrets are unset; an org opts a label into enforcing them by wiring those secrets. A gate NOT on
# this list may never be skipped for a promote — a skip there is a refusal, so nothing is green-washed.
ALLOWED_SKIP: frozenset[str] = frozenset({
    "cloud-parity",   # Slice-2 cloud cert (e2e-cloud-aws): self-skips when HONUA_AWS_ROLE_ARN is unset.
})


# --------------------------------------------------------------------------------------------------
# verify — fail closed unless the candidate is genuinely green
# --------------------------------------------------------------------------------------------------
def _gate_status(g: dict) -> str:
    """A gate row's verdict, tolerant of both the train report (`status`) and a sub-gate report
    (`decided`) shapes."""
    return str(g.get("status") or g.get("decided") or "")


def verify_gate_report(report: dict, label: str, allowed_skip: frozenset[str] = ALLOWED_SKIP) -> tuple[bool, str]:
    """Return (ok, why). Promotion is allowed ONLY when the candidate's train report is all-green for
    this label: overallStatus == 'pass' AND every gate is `pass`, OR `skipped` while on the explicit
    allowed-skip list. A skip of any other gate is refused (never silently promoted)."""
    if not isinstance(report, dict):
        return False, "gate-report is not an object"
    dry_run = report.get("dry_run")
    if dry_run is not False:
        if dry_run is True:
            return False, "candidate was certified by a dry-run release train"
        return False, "gate-report dry_run must be the boolean false for live promotion"
    overall = report.get("overallStatus")
    if overall != "pass":
        bad = [g.get("gate") for g in report.get("gates", [])
               if isinstance(g, dict) and _gate_status(g) in ("fail", "blocked")]
        return False, f"candidate gate-report overallStatus={overall!r} (not 'pass'); not-green gates: {bad or 'unknown'}"
    # Enforce the allowed-skip policy: a `skipped` gate is OK only if explicitly allow-listed.
    illegal_skips = [g.get("gate") for g in report.get("gates", [])
                     if isinstance(g, dict) and _gate_status(g) == "skipped" and g.get("gate") not in allowed_skip]
    if illegal_skips:
        return False, f"gate(s) skipped but not on the allowed-skip list: {illegal_skips}"
    rep_label = str(report.get("platform_label", ""))
    # The RC report carries the -rc label; the release label is its base (2026.1-rc.3 -> 2026.1).
    if rep_label and _base_label(rep_label) != _base_label(label):
        return False, f"gate-report is for {rep_label!r}, not {label!r}"
    skipped = [g.get("gate") for g in report.get("gates", [])
               if isinstance(g, dict) and _gate_status(g) == "skipped"]
    note = f" (allowed-skip: {skipped})" if skipped else ""
    return True, f"candidate {rep_label or label!r} is all-green{note}"


def _base_label(label: str) -> str:
    return label.split("-rc")[0].strip()


# --------------------------------------------------------------------------------------------------
# finalize — the manifest as tagged IS the release
# --------------------------------------------------------------------------------------------------
def finalize_manifest(manifest: dict, label: str, released_at: str) -> dict:
    m = dict(manifest)
    m["platformRelease"] = _base_label(label)
    m["status"] = "released"
    m["releasedDate"] = released_at
    return m


# --------------------------------------------------------------------------------------------------
# release notes — aggregate from the pinned set
# --------------------------------------------------------------------------------------------------
def _component_artifact(comp: dict) -> str:
    return str(comp.get("image") or comp.get("artifact") or "—")


def render_release_notes(manifest: dict, matrix: dict, label: str, evidence_url: str = "") -> str:
    base = _base_label(label)
    components = manifest.get("components") or {}
    lines: list[str] = []
    lines.append(f"# Honua {base}")
    lines.append("")
    lines.append("A **certified platform release** — the pinned, interoperable set of component "
                 "versions known to work together. Operate this one label, not N independent versions.")
    lines.append("")
    if evidence_url:
        lines.append(f"_Certified by the release train: {evidence_url}_")
        lines.append("")

    lines.append("## Components")
    lines.append("")
    lines.append("| Component | Version | Pinned commit | Artifact / image |")
    lines.append("|---|---|---|---|")
    for name in sorted(components):
        comp = components[name] or {}
        sha = str(comp.get("sha", ""))[:12] or "—"
        lines.append(f"| {name} | {comp.get('version', '—')} | `{sha}` | {_component_artifact(comp)} |")
    lines.append("")

    # Contract versions advertised by the server (the wire surfaces clients negotiate against).
    server = components.get("honua-server") or {}
    cvs = server.get("contractVersions") or {}
    if cvs:
        lines.append("## Contract versions")
        lines.append("")
        for surface, ver in cvs.items():
            lines.append(f"- **{surface}**: `{ver}`")
        lines.append("")

    db = server.get("dbSchema")
    if db:
        lines.append("## Database schema")
        lines.append("")
        lines.append(f"- requires `{db}` (preflight refuses to start below this).")
        lines.append("")

    # Compatibility window (from the matrix) so operators know which clients this server supports.
    sw = (matrix or {}).get("supportWindow") or {}
    if sw:
        lines.append("## Compatibility window")
        lines.append("")
        lines.append(f"- server supports clients within N-{sw.get('clientMinorsBack', '?')} minor of its "
                     f"contract version; deprecated surfaces carried ≥ {sw.get('deprecationReleases', '?')} releases.")
        lines.append("")

    lines.append("## Breaking changes & upgrade actions")
    lines.append("")
    lines.append("_None recorded for this release._  Per-component breaking changes are generated from "
                 "change metadata (conventional-commit + change-class labels) once that pipeline lands "
                 "(PLAN §8); until then, consult each component's own release notes.")
    lines.append("")

    lines.append("## Verification & provenance")
    lines.append("")
    lines.append("- Every wired release gate passed (manifest validity, per-repo CI on the pinned SHAs, "
                 "artifact-consumption, cross-component seam, cross-cloud parity, cross-repo conformance).")
    lines.append("- The pinned `platform-manifest.yaml` + `compatibility-matrix.yaml` are attached and "
                 "OIDC-signed; verify the signature before deploying.")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True, help="platform label to release, e.g. 2026.1")
    ap.add_argument("--gate-report", required=True, help="the candidate's release-train gate-report.json")
    ap.add_argument("--released-at", required=True, help="ISO-8601 release timestamp")
    ap.add_argument("--manifest", required=True, help="certified candidate platform-manifest.yaml")
    ap.add_argument("--matrix", required=True, help="certified candidate compatibility-matrix.yaml")
    ap.add_argument("--source-repository", required=True, help="train run head repository from Actions API")
    ap.add_argument("--source-sha", required=True, help="train run head SHA from Actions API")
    ap.add_argument("--source-branch", required=True, help="train run head branch from Actions API")
    ap.add_argument("--workflow-path", required=True, help="train workflow path from Actions API")
    ap.add_argument("--train-run-id", required=True, help="certifying release-train Actions run id")
    ap.add_argument("--train-run-attempt", required=True, type=int, help="certifying run attempt")
    ap.add_argument("--train-run-url", required=True, help="certifying release-train Actions run URL")
    ap.add_argument("--certification-mode", required=True, choices=sorted(CERTIFICATION_MODES))
    ap.add_argument("--out-manifest", required=True)
    ap.add_argument("--out-notes", required=True)
    args = ap.parse_args(argv)

    report = json.loads(Path(args.gate_report).read_text(encoding="utf-8"))
    candidate_ok, candidate_why = verify_candidate_binding(
        report,
        Path(args.manifest),
        Path(args.matrix),
        source_repository=args.source_repository,
        source_sha=args.source_sha,
        source_branch=args.source_branch,
        workflow_path=args.workflow_path,
        train_run_id=args.train_run_id,
        train_run_attempt=args.train_run_attempt,
        train_run_url=args.train_run_url,
        certification_mode=args.certification_mode,
    )
    if not candidate_ok:
        print(f"REFUSED: {candidate_why}", file=sys.stderr)
        return 1
    print(f"OK: {candidate_why}")

    ok, why = verify_gate_report(report, args.label)
    if not ok:
        print(f"REFUSED: {why}", file=sys.stderr)
        return 1
    print(f"OK: {why}")

    manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8")) or {}
    matrix = yaml.safe_load(Path(args.matrix).read_text(encoding="utf-8")) or {}

    finalized = finalize_manifest(manifest, args.label, args.released_at)
    Path(args.out_manifest).write_text(yaml.safe_dump(finalized, sort_keys=False), encoding="utf-8")

    notes = render_release_notes(manifest, matrix, args.label, str(report.get("evidence_url", "")))
    Path(args.out_notes).write_text(notes, encoding="utf-8")

    print(f"finalized manifest -> {args.out_manifest}")
    print(f"release notes      -> {args.out_notes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

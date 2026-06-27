#!/usr/bin/env python3
"""Finalize a certified release candidate into a platform release.

The promote step's brain (the GHA workflow is the muscle): it REFUSES to release unless the
candidate's gate-report is all-green, then finalizes the manifest and generates aggregate release
notes. This is the "AI proposes, the pipeline disposes" boundary made concrete — a red/blocked
candidate can never be tagged (AGENTS.md: the gate must be able to fail; here it fails *closed*).

Three pure, unit-tested pieces:

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
      --released-at 2026-07-01T00:00:00Z \
      --out-manifest finalized-manifest.yaml --out-notes release-notes.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------------------------------
# verify — fail closed unless the candidate is genuinely green
# --------------------------------------------------------------------------------------------------
def verify_gate_report(report: dict, label: str) -> tuple[bool, str]:
    """Return (ok, why). Promotion is allowed ONLY when the candidate's train report is all-green
    for this label."""
    if not isinstance(report, dict):
        return False, "gate-report is not an object"
    overall = report.get("overallStatus")
    if overall != "pass":
        bad = [g.get("gate") for g in report.get("gates", [])
               if isinstance(g, dict) and g.get("decided") in ("fail", "blocked")]
        return False, f"candidate gate-report overallStatus={overall!r} (not 'pass'); not-green gates: {bad or 'unknown'}"
    rep_label = str(report.get("platform_label", ""))
    # The RC report carries the -rc label; the release label is its base (2026.1-rc.3 -> 2026.1).
    if rep_label and _base_label(rep_label) != _base_label(label):
        return False, f"gate-report is for {rep_label!r}, not {label!r}"
    return True, f"candidate {rep_label or label!r} is all-green"


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
    ap.add_argument("--manifest", default=str(REPO_ROOT / "platform-manifest.yaml"))
    ap.add_argument("--matrix", default=str(REPO_ROOT / "compatibility-matrix.yaml"))
    ap.add_argument("--out-manifest", required=True)
    ap.add_argument("--out-notes", required=True)
    args = ap.parse_args(argv)

    report = json.loads(Path(args.gate_report).read_text(encoding="utf-8"))
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

#!/usr/bin/env python3
"""Plan or apply an atomic protocol-certification convergence rebind.

PLAN is the default and does not modify tracked files.  APPLY changes only the
reviewable repository half; ledger aggregation and repository-variable updates
are deliberately emitted as post-merge commands because the squash SHA cannot
exist before merge.
"""

from __future__ import annotations

import argparse
import base64
import copy
import difflib
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
REVISIONS = Path("certification/sources/source-revisions.v1.json")
CATALOG = Path("certification/protocol-certification-requirements.v1.json")
MANIFEST = Path("platform-manifest.yaml")
CALLERS = (
    Path(".github/workflows/pr-protocol-certification.yml"),
    Path(".github/workflows/nightly-protocol-certification.yml"),
    Path(".github/workflows/release-train.yml"),
)
PIN_RE = re.compile(r"(honua-io/honua-release/\.github/workflows/gate-protocol-certification\.yml@)[0-9a-f]{40}([^\n]*)")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REBIND_COMMENT = "# REBIND: advance to this PR's squash SHA post-merge"

# Only files that are literal upstream snapshots belong here.  The other files
# under certification/sources are release-owned governance inputs.
VENDORED: dict[str, tuple[tuple[str, str], ...]] = {
    "server": (("docs/gis/data/capability-matrix.v1.json", "certification/sources/server/capability-matrix.v1.json"),),
    "server-certification": (("docs/gis/data/protocol-harness-assignments.v1.json", "certification/sources/server/protocol-harness-assignments.v1.json"),),
    "sdk-js": (("config/protocol-certification.v1.json", "certification/sources/sdk-js/protocol-certification.v1.json"), ("config/sdk-coverage.v1.json", "certification/sources/sdk-js/sdk-coverage.v1.json")),
    "sdk-python": (("conformance/protocol-certification.v1.json", "certification/sources/sdk-python/protocol-certification.v1.json"), ("compatibility/sdk-coverage.v1.json", "certification/sources/sdk-python/sdk-coverage.v1.json")),
    "sdk-dotnet": (("contracts/sdk-certification.v1.json", "certification/sources/sdk-dotnet/sdk-certification.v1.json"), ("contracts/sdk-coverage.v1.json", "certification/sources/sdk-dotnet/sdk-coverage.v1.json")),
}


class GitHub(Protocol):
    def head(self, repository: str) -> str: ...
    def content(self, repository: str, path: str, revision: str) -> bytes: ...


class GhCli:
    def _json(self, endpoint: str) -> Any:
        result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True)
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise Finding(f"upstream fetch mismatch: gh api {endpoint}: {detail}")
        return json.loads(result.stdout)

    def head(self, repository: str) -> str:
        repo = self._json(f"repos/{repository}")
        branch = repo["default_branch"]
        sha = self._json(f"repos/{repository}/commits/{branch}")["sha"]
        return checked_sha(sha, f"{repository} {branch} HEAD")

    def content(self, repository: str, path: str, revision: str) -> bytes:
        obj = self._json(f"repos/{repository}/contents/{path}?ref={revision}")
        if obj.get("type") != "file" or obj.get("encoding") != "base64":
            raise Finding(f"upstream fetch mismatch: {repository}/{path}@{revision} is not a base64 file")
        data = base64.b64decode(obj["content"], validate=False)
        if hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest() != obj.get("sha"):
            raise Finding(f"upstream fetch mismatch: Git blob SHA disagrees for {repository}/{path}@{revision}")
        return data


class Finding(RuntimeError):
    pass


def checked_sha(value: str, label: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise Finding(f"{label} is not a full lowercase commit SHA: {value!r}")
    return value


def load_json(root: Path, path: Path) -> Any:
    return json.loads((root / path).read_text(encoding="utf-8"))


def manifest_value(text: str, component: str, field: str = "sha") -> str:
    match = re.search(rf"^  {re.escape(component)}:\n(?:(?:    |      ).*\n)*?    {re.escape(field)}: [\"']?([^\s\"']+)", text, re.MULTILINE)
    if not match:
        raise Finding(f"manifest target missing for components.{component}.{field}")
    return match.group(1)


def scalar(text: str, name: str) -> str:
    match = re.search(rf"^\s*{re.escape(name)}:\s*[\"']?([^\s\"']+)", text, re.MULTILINE)
    if not match:
        raise Finding(f"manifest value missing: {name}")
    return match.group(1)


def targets(root: Path, gh: GitHub) -> tuple[dict[str, str], dict[str, str]]:
    manifest = (root / MANIFEST).read_text(encoding="utf-8")
    revisions = load_json(root, REVISIONS)["sources"]
    frozen = {
        "server": scalar(manifest, "serverCertificationProducerSha"),
        "server-certification": scalar(manifest, "serverCertificationProducerSha"),
        "sdk-dotnet": manifest_value(manifest, "honua-sdk-dotnet"),
        "sdk-python": manifest_value(manifest, "honua-sdk-python"),
        "sdk-js": manifest_value(manifest, "honua-sdk-js"),
        "geospatial-grpc": manifest_value(manifest, "geospatial-grpc"),
        "geospatial-mcp": manifest_value(manifest, "geospatial-mcp"),
    }
    result: dict[str, str] = {}
    rules: dict[str, str] = {}
    for name, item in revisions.items():
        if name in frozen:
            result[name] = checked_sha(frozen[name], f"manifest target for {name}")
            rules[name] = "manifest/frozen"
        else:
            result[name] = gh.head(item["repository"])
            rules[name] = "producer default-branch HEAD"
    return result, rules


def fetch_snapshots(root: Path, gh: GitHub, revisions: dict[str, Any], pins: dict[str, str]) -> dict[str, bytes]:
    fetched: dict[str, bytes] = {}
    for source, mappings in VENDORED.items():
        repository = revisions[source]["repository"]
        for upstream, local in mappings:
            data = gh.content(repository, upstream, pins[source])
            try:
                json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Finding(f"upstream fetch mismatch: {repository}/{upstream}@{pins[source]} is invalid JSON: {exc}") from exc
            fetched[local] = data
    return fetched


def run_catalog(root: Path, receipt_min: str) -> None:
    generator = root / "certification/generate-protocol-requirements.py"
    text = generator.read_text(encoding="utf-8")
    text, count = re.subn(r'("receipt_schema_min":\s*)"v[12]"', rf'\1"{receipt_min}"', text)
    if count != 1:
        raise Finding("catalog generator has no unique receipt_schema_min assignment")
    generator.write_text(text, encoding="utf-8")
    subprocess.run([sys.executable, str(generator)], cwd=root, check=True, capture_output=True, text=True)
    validation = subprocess.run([sys.executable, "certification/validate-protocol-requirements.py"], cwd=root, capture_output=True, text=True)
    if validation.returncode:
        raise Finding(f"validator failure:\n{validation.stdout}{validation.stderr}")


def prepare(root: Path, gh: GitHub, receipt_min_arg: str) -> tuple[dict[str, Any], dict[str, bytes], str]:
    source_doc = load_json(root, REVISIONS)
    pins, rules = targets(root, gh)
    snapshots = fetch_snapshots(root, gh, source_doc["sources"], pins)
    old_catalog = load_json(root, CATALOG)
    receipt_min = old_catalog["receipt_schema_min"] if receipt_min_arg == "keep" else receipt_min_arg
    with tempfile.TemporaryDirectory(prefix="convergence-rebind-") as tmp:
        trial = Path(tmp) / "repo"
        shutil.copytree(root, trial, ignore=shutil.ignore_patterns(".git", "rebind-plan.json", "__pycache__"))
        revised = copy.deepcopy(source_doc)
        for name, pin in pins.items():
            revised["sources"][name]["commit"] = pin
        (trial / REVISIONS).write_text(json.dumps(revised, indent=2) + "\n", encoding="utf-8")
        for path, data in snapshots.items():
            (trial / path).write_bytes(data)
        run_catalog(trial, receipt_min)
        new_catalog_bytes = (trial / CATALOG).read_bytes()
        new_generator_bytes = (trial / "certification/generate-protocol-requirements.py").read_bytes()
    changes = []
    for name, item in source_doc["sources"].items():
        changes.append({"source": name, "repository": item["repository"], "current": item["commit"], "target": pins[name], "rule": rules[name]})
    old_text = json.dumps(old_catalog, indent=2).splitlines()
    new_catalog = json.loads(new_catalog_bytes)
    new_text = json.dumps(new_catalog, indent=2).splitlines()
    diff = list(difflib.unified_diff(old_text, new_text, lineterm=""))
    manifest = (root / MANIFEST).read_text(encoding="utf-8")
    ledger = load_json(root, CATALOG).get("revision")
    added = sum(line.startswith("+") and not line.startswith("+++") for line in diff)
    removed = sum(line.startswith("-") and not line.startswith("---") for line in diff)
    vendored_changes = sorted(path for path, data in snapshots.items() if (root / path).read_bytes() != data)
    plan = {
        "schema": "honua.convergence-rebind-plan/v1",
        "mode": "plan",
        "sources": changes,
        "catalog": {"path": str(CATALOG), "current_cells": len(old_catalog["requirements"]), "proposed_cells": len(new_catalog["requirements"]), "additions": added, "deletions": removed, "diff_lines": len(diff), "changed": old_catalog != new_catalog, "vendored_files_changed": vendored_changes},
        "receipt_schema_min": {"current": old_catalog["receipt_schema_min"], "proposed": receipt_min},
        "evidence_reaggregation": {"repository": scalar(manifest, "repository"), "requirements_catalog_revision": new_catalog["revision"], "requirements_source_revision": "<THIS_PR_SQUASH_SHA>", "ledger_path": scalar(manifest, "path")},
        "bindings": {
            "PROTOCOL_CERTIFICATION_MATRIX_COMMIT": {"current": scalar(manifest, "commit"), "proposed": "<HONUA_EVIDENCE_AGGREGATION_COMMIT>"},
            "PROTOCOL_CERTIFICATION_MATRIX_SHA256": {"current": scalar(manifest, "sha256"), "proposed": "sha256:<HONUA_EVIDENCE_LEDGER_SHA256>"},
            "PROTOCOL_CERTIFICATION_REQUIREMENTS_SOURCE_REVISION": {"current": scalar(manifest, "requirementsSourceRevision"), "proposed": "<THIS_PR_SQUASH_SHA>"},
        },
        "gate_workflow_pins": [{"path": str(path), "current": PIN_RE.search((root / path).read_text()).group(0), "proposed_comment": REBIND_COMMENT} for path in CALLERS],
    }
    payloads = dict(snapshots)
    revised = copy.deepcopy(source_doc)
    for name, pin in pins.items(): revised["sources"][name]["commit"] = pin
    payloads[str(REVISIONS)] = (json.dumps(revised, indent=2) + "\n").encode()
    payloads[str(CATALOG)] = new_catalog_bytes
    payloads["certification/generate-protocol-requirements.py"] = new_generator_bytes
    return plan, payloads, ledger


def post_merge_commands(root: Path = ROOT) -> str:
    manifest = (root / MANIFEST).read_text(encoding="utf-8")
    candidate_source_sha = checked_sha(manifest_value(manifest, "honua-server"), "candidate source SHA")
    candidate_image_digest = manifest_value(manifest, "honua-server", "digest")
    candidate_cut_at = scalar(manifest, "candidateCutAt")
    return f"""```bash
# Run after this PR is squash-merged.
RELEASE_SHA=<THIS_PR_SQUASH_SHA>
gh workflow run aggregate.yml -R honua-io/honua-evidence -f requirements_revision="$RELEASE_SHA" -f candidate_source_sha="{candidate_source_sha}" -f candidate_image_digest="{candidate_image_digest}" -f candidate_cut_at="{candidate_cut_at}"
# Wait for aggregation to finish, verify its ledger binds $RELEASE_SHA, then fill these exact values.
EVIDENCE_SHA=<HONUA_EVIDENCE_AGGREGATION_COMMIT>
LEDGER_SHA256=sha256:<HONUA_EVIDENCE_LEDGER_SHA256>
gh variable set PROTOCOL_CERTIFICATION_MATRIX_COMMIT --repo honua-io/honua-release --body "$EVIDENCE_SHA"
gh variable set PROTOCOL_CERTIFICATION_MATRIX_SHA256 --repo honua-io/honua-release --body "$LEDGER_SHA256"
gh variable set PROTOCOL_CERTIFICATION_REQUIREMENTS_SOURCE_REVISION --repo honua-io/honua-release --body "$RELEASE_SHA"
# Replace each REBIND placeholder in a follow-up PR with $RELEASE_SHA.
```"""


def human(plan: dict[str, Any], root: Path = ROOT) -> str:
    lines = ["CONVERGENCE REBIND PLAN", "", "Vendored source pins:"]
    for row in plan["sources"]:
        lines.append(f"  {row['source']}: {row['current'][:8]} -> {row['target'][:8]} ({row['rule']})")
    cat = plan["catalog"]
    lines += ["", f"Catalog: {cat['current_cells']} -> {cat['proposed_cells']} cells; +{cat['additions']}/-{cat['deletions']} ({cat['diff_lines']} diff lines)", f"Vendored files changed: {', '.join(cat['vendored_files_changed']) or 'none'}", f"receipt_schema_min: {plan['receipt_schema_min']['current']} -> {plan['receipt_schema_min']['proposed']}", "", "Ledger re-aggregation:", f"  {plan['evidence_reaggregation']}", "", "Bindings:"]
    for name, values in plan["bindings"].items(): lines.append(f"  {name}: {values['current']} -> {values['proposed']}")
    lines += ["", "Gate workflow pins:"]
    for pin in plan["gate_workflow_pins"]: lines.append(f"  {pin['path']}: {pin['current']} -> {pin['proposed_comment']}")
    lines += ["", "Post-merge commands:", post_merge_commands(root)]
    return "\n".join(lines)


def apply(root: Path, plan: dict[str, Any], payloads: dict[str, bytes]) -> None:
    before = subprocess.run(["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True, text=True).stdout
    dirty = [line for line in before.splitlines() if not line.endswith(" rebind-plan.json")]
    if dirty:
        raise Finding("APPLY requires a clean worktree")
    for path, data in payloads.items(): (root / path).write_bytes(data)
    for path in CALLERS:
        text = (root / path).read_text(encoding="utf-8")
        text, count = PIN_RE.subn(lambda match: f"{match.group(1)}{match.group(0).split('@', 1)[1][:40]} {REBIND_COMMENT}", text)
        if count != 1: raise Finding(f"gate pin mismatch: expected exactly one pin in {path}, found {count}")
        (root / path).write_text(text, encoding="utf-8")
    run_catalog(root, plan["receipt_schema_min"]["proposed"])
    expected = {str(REVISIONS), str(CATALOG), "certification/generate-protocol-requirements.py", *payloads.keys(), *(str(p) for p in CALLERS)}
    changed = set(subprocess.run(["git", "diff", "--name-only"], cwd=root, check=True, capture_output=True, text=True).stdout.splitlines())
    unexpected = changed - expected
    if unexpected: raise Finding(f"catalog regeneration changed unplanned files: {sorted(unexpected)}")
    receipt = "\n".join((
        "## Convergence rebind receipt",
        "",
        "Related to #191",
        "Related to #181",
        "",
        "| Coordinate | Old | New |",
        "|---|---|---|",
        *(f"| `{row['source']}` | `{row['current']}` | `{row['target']}` ({row['rule']}) |" for row in plan["sources"]),
        *(f"| `{name}` | `{values['current']}` | `{values['proposed']}` |" for name, values in plan["bindings"].items()),
        "",
        f"Receipt schema minimum: `{plan['receipt_schema_min']['current']}` → `{plan['receipt_schema_min']['proposed']}`.",
        "",
        "The repository-side changes are complete. Ledger aggregation and variable changes remain post-merge operations:",
        "",
        post_merge_commands(root),
        "",
    ))
    (root / "rebind-receipt.md").write_text(receipt, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt-min", choices=("keep", "v1", "v2"), default="keep")
    parser.add_argument("--plan-output", type=Path, default=Path("rebind-plan.json"))
    args = parser.parse_args(argv)
    try:
        plan, payloads, _ = prepare(ROOT, GhCli(), args.receipt_min)
        args.plan_output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        print(human(plan, ROOT))
        if args.apply:
            apply(ROOT, plan, payloads)
            print("\nAPPLY complete. Commit/push these repository-only changes as a PR; do not update variables before squash merge.")
        return 0
    except (Finding, subprocess.CalledProcessError) as exc:
        print(f"REBIND ABORTED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

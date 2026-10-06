#!/usr/bin/env python3
"""Plan or apply an atomic protocol-certification convergence rebind.

PLAN is the default and does not modify tracked files. APPLY stages regenerated
sources and the catalog. FINALIZE binds a verified evidence ledger and advances
all reusable-workflow pins in the same review branch. No workflow runs APPLY or
FINALIZE any more: ruling R18 forbids a hand rebind, convergence-rebind.yml only
plans, and no workflow reads the PROTOCOL_CERTIFICATION_* repository variables.

PLAN, APPLY, and FINALIZE refuse while a .NET, Python, or JavaScript component
SHA is not the published clientArtifacts sourceSha. That refusal leaves
protocolCertification.ledger pending; it does not invent an evidence commit
or digest. The cut selects certifiable published SDKs before a rebind exists.

NIGHTLY-STAGE and NIGHTLY-BIND are the same convergence for the candidate the
nightly resolved (honua-release#386, ruling R18): stage regenerates the catalog
at the candidate's pins on the nightly's own commit, and bind verifies the ledger
honua-evidence aggregated from tonight's producer runs and writes
protocolCertification.ledger into the candidate manifest. Nothing is hand-bound;
no repository variable is read or written.
"""

from __future__ import annotations

import argparse
import base64
import copy
import difflib
import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

ROOT = Path(__file__).resolve().parents[1]
REVISIONS = Path("certification/sources/source-revisions.v1.json")
CATALOG = Path("certification/protocol-certification-requirements.v1.json")
MANIFEST = Path("platform-manifest.yaml")
# The only caller: the nightly's train certifies the ledger bound into its candidate manifest. The
# PR and standalone nightly callers that read the PROTOCOL_CERTIFICATION_* variables are retired.
CALLERS = (Path(".github/workflows/release-train.yml"),)
PIN_RE = re.compile(r"(honua-io/honua-release/\.github/workflows/gate-protocol-certification\.yml@)[0-9a-f]{40}([^\n]*)")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REBIND_COMMENT = "pin to the staged catalog commit in the reviewed rebind PR"

# Only files that are literal upstream snapshots belong here.  The other files
# under certification/sources are release-owned governance inputs.
# Catalog producer, manifest component, and the clientArtifacts key whose
# sourceSha is the published package. A rebind may target only a SHA that is
# already both the component pin and the shipping bytes.
SDK_PRODUCERS = (
    ("sdk-dotnet", "honua-sdk-dotnet", "honua-sdk-dotnet"),
    ("sdk-python", "honua-sdk-python", "honua-sdk-python-wheel"),
    ("sdk-js", "honua-sdk-js", "honua-sdk-js"),
)

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

    def tree(self, repository: str, revision: str) -> list[tuple[str, str]]:
        tree = self._json(f"repos/{repository}/git/trees/{revision}?recursive=1")
        if tree.get("truncated"):
            raise Finding(f"upstream fetch mismatch: {repository}@{revision} tree listing is truncated")
        return [(row["path"], row["sha"]) for row in tree["tree"] if row.get("type") == "blob"]

    def commits(self, repository: str, branch: str, path: str, since: str) -> list[str]:
        rows = self._json(f"repos/{repository}/commits?sha={branch}&path={path}&since={since}&per_page=100")
        return [checked_sha(row.get("sha"), f"{repository} commit") for row in rows]

    def raw(self, repository: str, path: str, revision: str) -> bytes:
        """File bytes of any size, checked against the size and Git blob id GitHub reports for the path."""
        meta = self._json(f"repos/{repository}/contents/{path}?ref={revision}")
        result = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{repository}/contents/{path}?ref={revision}"],
            capture_output=True)
        if result.returncode:
            detail = result.stderr.decode(errors="replace").strip()
            raise Finding(f"upstream fetch mismatch: raw {repository}/{path}@{revision}: {detail}")
        data = result.stdout
        if meta.get("type") != "file" or meta.get("size") != len(data) \
                or hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest() != meta.get("sha"):
            raise Finding(f"upstream fetch mismatch: size or Git blob SHA disagrees for {repository}/{path}@{revision}")
        return data

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


def load_manifest(root: Path) -> dict[str, Any]:
    return yaml.safe_load((root / MANIFEST).read_text(encoding="utf-8"))


def sdk_pin_divergence(manifest: dict[str, Any]) -> list[str]:
    components = manifest.get("components") or {}
    artifacts = manifest.get("clientArtifacts") or {}
    findings: list[str] = []
    for source, component, artifact in SDK_PRODUCERS:
        component_body = components.get(component) if isinstance(components.get(component), dict) else {}
        published = artifacts.get(artifact) if isinstance(artifacts.get(artifact), dict) else {}
        component_sha = str((component_body or {}).get("sha") or "")
        published_sha = str((published or {}).get("sourceSha") or "")
        if SHA_RE.fullmatch(component_sha) and component_sha == published_sha:
            continue
        package = (published or {}).get("package") or artifact
        version = (published or {}).get("version") or "missing"
        findings.append(
            f"{source}: components.{component}.sha={component_sha or 'missing'} "
            f"clientArtifacts.{artifact}.sourceSha={published_sha or 'missing'} "
            f"({package} {version})"
        )
    return findings


def require_published_sdk_pins(manifest: dict[str, Any]) -> None:
    findings = sdk_pin_divergence(manifest)
    if not findings:
        return
    raise Finding(
        "SDK producer pins are not the published artifacts; "
        "protocolCertification.ledger stays pending until the cut selects "
        "certifiable published SDKs and rebinds. "
        + " | ".join(findings)
    )


def require_plan_targets_published(manifest: dict[str, Any], plan: dict[str, Any]) -> None:
    require_published_sdk_pins(manifest)
    artifacts = manifest.get("clientArtifacts") or {}
    by_source = {source: artifact for source, _component, artifact in SDK_PRODUCERS}
    for row in plan.get("sources") or []:
        artifact_name = by_source.get(row.get("source"))
        if artifact_name is None:
            continue
        published = artifacts.get(artifact_name) if isinstance(artifacts.get(artifact_name), dict) else {}
        published_sha = (published or {}).get("sourceSha")
        if row.get("target") != published_sha:
            raise Finding(
                f"rebind plan target for {row.get('source')} is {row.get('target')}, "
                f"not published clientArtifacts.{artifact_name}.sourceSha {published_sha}"
            )


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


def replace_scalar(text: str, name: str, current: str, value: str) -> str:
    pattern = re.compile(
        rf"^(\s*{re.escape(name)}:\s*)([\"']?){re.escape(current)}([\"']?)(.*)$",
        re.MULTILINE,
    )
    text, count = pattern.subn(lambda match: f"{match.group(1)}{match.group(2)}{value}{match.group(3)}{match.group(4)}", text)
    if count != 1:
        raise Finding(f"manifest value is not unique: {name}")
    return text


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
    # Refuse before any upstream fetch. A catalog staged from a non-shipping
    # component SHA cannot be bound to the bytes customers install.
    require_published_sdk_pins(load_manifest(root))
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
        "evidence_reaggregation": {"repository": scalar(manifest, "repository"), "requirements_catalog_revision": new_catalog["revision"], "requirements_source_revision": "<STAGED_CATALOG_COMMIT>", "ledger_path": scalar(manifest, "path")},
        "bindings": {
            "PROTOCOL_CERTIFICATION_MATRIX_COMMIT": {"current": scalar(manifest, "commit"), "proposed": "<HONUA_EVIDENCE_AGGREGATION_COMMIT>"},
            "PROTOCOL_CERTIFICATION_MATRIX_SHA256": {"current": scalar(manifest, "sha256"), "proposed": "sha256:<HONUA_EVIDENCE_LEDGER_SHA256>"},
            "PROTOCOL_CERTIFICATION_REQUIREMENTS_SOURCE_REVISION": {"current": scalar(manifest, "requirementsSourceRevision"), "proposed": "<STAGED_CATALOG_COMMIT>"},
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


def human(plan: dict[str, Any], root: Path = ROOT) -> str:
    lines = ["CONVERGENCE REBIND PLAN", "", "Vendored source pins:"]
    for row in plan["sources"]:
        lines.append(f"  {row['source']}: {row['current'][:8]} -> {row['target'][:8]} ({row['rule']})")
    cat = plan["catalog"]
    lines += ["", f"Catalog: {cat['current_cells']} -> {cat['proposed_cells']} cells; +{cat['additions']}/-{cat['deletions']} ({cat['diff_lines']} diff lines)", f"Vendored files changed: {', '.join(cat['vendored_files_changed']) or 'none'}", f"receipt_schema_min: {plan['receipt_schema_min']['current']} -> {plan['receipt_schema_min']['proposed']}", "", "Ledger re-aggregation:", f"  {plan['evidence_reaggregation']}", "", "Bindings:"]
    for name, values in plan["bindings"].items(): lines.append(f"  {name}: {values['current']} -> {values['proposed']}")
    lines += ["", "Gate workflow pins:"]
    for pin in plan["gate_workflow_pins"]: lines.append(f"  {pin['path']}: {pin['current']} -> {pin['proposed_comment']}")
    lines += ["", "Apply mode stages the catalog; the workflow then aggregates, verifies, and finalizes one review PR. Repository variables activate automatically only after merge."]
    return "\n".join(lines)


def apply(root: Path, plan: dict[str, Any], payloads: dict[str, bytes], candidate: frozenset[str] = frozenset()) -> None:
    """Stage the catalog. `candidate` names the resolved candidate files a nightly has already
    written into the checkout; any other uncommitted change refuses."""
    require_plan_targets_published(load_manifest(root), plan)
    before = subprocess.run(["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True, text=True).stdout
    dirty = [line for line in before.splitlines() if not line.endswith(" rebind-plan.json") and line[3:] not in candidate]
    if dirty:
        raise Finding("APPLY requires a clean worktree")
    for path, data in payloads.items(): (root / path).write_bytes(data)
    run_catalog(root, plan["receipt_schema_min"]["proposed"])
    expected = {str(REVISIONS), str(CATALOG), "certification/generate-protocol-requirements.py", *payloads.keys(), *candidate}
    changed = set(subprocess.run(["git", "diff", "--name-only"], cwd=root, check=True, capture_output=True, text=True).stdout.splitlines())
    unexpected = changed - expected
    if unexpected: raise Finding(f"catalog regeneration changed unplanned files: {sorted(unexpected)}")


def finalize(root: Path, plan: dict[str, Any], requirements_revision: str, evidence_commit: str, ledger_sha256: str) -> None:
    # Check the published-pin convergence before accepting an evidence digest,
    # so a divergent snapshot cannot be marked bound.
    require_plan_targets_published(load_manifest(root), plan)
    requirements_revision = checked_sha(requirements_revision, "requirements revision")
    evidence_commit = checked_sha(evidence_commit, "evidence commit")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", ledger_sha256):
        raise Finding(f"ledger sha256 is not canonical: {ledger_sha256!r}")
    manifest_path = root / MANIFEST
    manifest = manifest_path.read_text(encoding="utf-8")
    bindings = plan["bindings"]
    manifest = replace_scalar(manifest, "commit", bindings["PROTOCOL_CERTIFICATION_MATRIX_COMMIT"]["current"], evidence_commit)
    manifest = replace_scalar(manifest, "requirementsSourceRevision", bindings["PROTOCOL_CERTIFICATION_REQUIREMENTS_SOURCE_REVISION"]["current"], requirements_revision)
    manifest = replace_scalar(manifest, "sha256", bindings["PROTOCOL_CERTIFICATION_MATRIX_SHA256"]["current"], ledger_sha256)
    # A working snapshot can explicitly invalidate its old ledger while awaiting
    # candidate evidence. Only FINALIZE restores bound alongside all three pins.
    ledger_status = yaml.safe_load(manifest)["protocolCertification"]["ledger"]["status"]
    if ledger_status not in {"pending", "bound"}:
        raise Finding(f"invalid ledger status: {ledger_status!r}")
    if ledger_status == "pending":
        manifest = replace_scalar(manifest, "status", "pending", "bound")
    manifest_path.write_text(manifest, encoding="utf-8")
    for path in CALLERS:
        text = (root / path).read_text(encoding="utf-8")
        text, count = PIN_RE.subn(lambda match: f"{match.group(1)}{requirements_revision}{match.group(2)}", text)
        if count != 1:
            raise Finding(f"gate pin mismatch: expected exactly one pin in {path}, found {count}")
        (root / path).write_text(text, encoding="utf-8")
    bindings["PROTOCOL_CERTIFICATION_MATRIX_COMMIT"]["proposed"] = evidence_commit
    bindings["PROTOCOL_CERTIFICATION_MATRIX_SHA256"]["proposed"] = ledger_sha256
    bindings["PROTOCOL_CERTIFICATION_REQUIREMENTS_SOURCE_REVISION"]["proposed"] = requirements_revision
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
        "The ledger was aggregated and byte-verified before this PR was opened. This PR must be merged with a merge commit (not squash- or rebase-merged) so the staged catalog commit remains reachable from trunk. On merge, the trusted activation workflow verifies that ancestry and copies these exact three values from the merged manifest into repository variables.",
        "",
        "Closes #191",
        "Refs #180 #181 #182 #187 #188",
        "",
    ))
    (root / "rebind-receipt.md").write_text(receipt, encoding="utf-8")


GRPC_OPERATIONS = Path("certification/sources/geospatial-grpc/operations.v1.json")
GRPC_RULING = "grpc-2026.1-implemented-rpcs"
GRPC_PROGRAM = "src/Honua.Server/Program.cs"
MAP_GRPC_RE = re.compile(r"MapGrpcService<([A-Za-z0-9_.]+)>")
VERIFIED_RE = re.compile(r'("verified_server_commits":\s*\[[^\]]*?)(\s*\])')


def grpc_surface(gh: Any, repository: str, sha: str) -> str:
    """What the gRPC scope ruling was checked against at one server commit: the Program.cs block
    that maps the geospatial.v1 services (with its capability-flag conditions) and the Git blob
    of every mapped service's source file."""
    lines = gh.content(repository, GRPC_PROGRAM, sha).decode("utf-8").splitlines()
    mapped = [index for index, line in enumerate(lines) if "MapGrpcService<" in line]
    if not mapped:
        raise Finding(f"{repository}@{sha}:{GRPC_PROGRAM} maps no gRPC service")
    block = lines[mapped[0]:mapped[-1] + 1]
    tree = gh.tree(repository, sha)
    files = {}
    for name in sorted({value.rsplit(".", 1)[-1] for value in MAP_GRPC_RE.findall("\n".join(block))}):
        hits = [(path, blob) for path, blob in tree if path.startswith("src/") and path.rsplit("/", 1)[-1] == f"{name}.cs"]
        if len(hits) != 1:
            raise Finding(f"{repository}@{sha}: mapped gRPC service {name} resolves to {len(hits)} source files")
        files[hits[0][0]] = hits[0][1]
    return hashlib.sha256(json.dumps({"block": block, "files": files}, sort_keys=True).encode()).hexdigest()


def verify_grpc_scope(root: Path, gh: Any, server_sha: str) -> dict[str, str] | None:
    """Re-check the gRPC scope ruling for the candidate server the way its evidence was gathered.

    The ruling holds for the candidate when its implemented gRPC surface is byte-identical to a
    commit the ruling was verified at; the candidate is then recorded in verified_server_commits.
    Any other surface refuses: which RPCs that revision implements must be re-ruled.
    """
    path = root / GRPC_OPERATIONS
    text = path.read_text(encoding="utf-8")
    ruling = next(r for r in json.loads(text)["rulings"] if r["id"] == GRPC_RULING)
    verified = ruling["verified_server_commits"]
    if server_sha in verified:
        return None
    repository = "honua-io/honua-server"
    candidate = grpc_surface(gh, repository, server_sha)
    match = next((commit for commit in reversed(verified) if grpc_surface(gh, repository, commit) == candidate), None)
    if match is None:
        raise Finding(f"the {GRPC_RULING!r} ruling cannot carry to server {server_sha}: its gRPC surface "
                      f"differs from every verified commit {verified}; re-check which RPCs it implements")
    updated, count = VERIFIED_RE.subn(lambda m: f'{m.group(1)},\n                "{server_sha}"{m.group(2)}', text)
    after = json.loads(updated) if count == 1 else {}
    if [r for r in after.get("rulings", []) if r["id"] == GRPC_RULING][:1] != [{**ruling, "verified_server_commits": [*verified, server_sha]}]:
        raise Finding(f"could not record {server_sha} in {GRPC_OPERATIONS} verified_server_commits")
    path.write_text(updated, encoding="utf-8")
    return {"server_commit": server_sha, "same_surface_as": match, "surface_sha256": candidate}


NIGHTLY_CANDIDATE_FILES = frozenset({str(MANIFEST), "compatibility-matrix.yaml", str(GRPC_OPERATIONS)})
LEDGER_SCHEMA = "honua.protocol-certification/v1"
# R40: a desktop cell's release bucket and client driver are part of the requirement it answers.
LEDGER_IDENTITY = ("capability_key", "surface", "operation", "canonical_client", "client_lane",
                   "client_version", "deployment_target", "release_bucket", "client_driver")
EVIDENCE_URI = "https://evidence.honua.io/data/sha256/"
DISPATCH_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def nightly_stage(root: Path, gh: GitHub) -> dict[str, Any]:
    """Regenerate the catalog at the resolved candidate's pins over the nightly's checkout."""
    manifest = load_manifest(root)
    if manifest["protocolCertification"]["ledger"].get("status") != "pending":
        raise Finding("NIGHTLY-STAGE expects the resolved candidate's ledger to be pending")
    grpc_scope = verify_grpc_scope(root, gh, manifest["components"]["honua-server"]["sha"])
    plan, payloads, _ = prepare(root, gh, "keep")
    plan["mode"] = "nightly-stage"
    plan["grpc_scope_verification"] = grpc_scope
    apply(root, plan, payloads, NIGHTLY_CANDIDATE_FILES)
    return plan


def selects(entry: dict[str, Any], row: dict[str, Any]) -> bool:
    """The catalog's production rule: a disposition owns a cell by client lane (glob) or deployment
    target, less the lanes it hands to another disposition (except_client_lanes, glob)."""
    if any(fnmatch.fnmatchcase(str(row.get("client_lane")), pattern) for pattern in entry.get("except_client_lanes", [])):
        return False
    return any(fnmatch.fnmatchcase(str(row.get("client_lane")), pattern) for pattern in entry.get("client_lanes", [])) \
        or row.get("deployment_target") in entry.get("deployment_targets", [])


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def receipt_digest(receipt: Any) -> str | None:
    """The digest honua-evidence content-addresses a receipt by (check_protocol_certification)."""
    if not isinstance(receipt, dict):
        return None
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def dispatched_run(run: dict[str, Any], dispatch_id: str) -> dict[str, Any]:
    """The identity a receipt must carry to be evidence from `run`, as this nightly dispatched it."""
    return {"repository": run.get("repository"), "workflow": run.get("workflow"), "run_id": run.get("run_id"),
            "run_attempt": run.get("run_attempt"), "dispatch_id": dispatch_id}


def nightly_candidate(manifest: dict[str, Any]) -> dict[str, str]:
    server = manifest["components"]["honua-server"]
    return {"source_sha": server["sha"], "image_digest": server["digest"],
            "cut_at": manifest["protocolCertification"]["candidateCutAt"]}


def verify_nightly_ledger(ledger: dict[str, Any], catalog: dict[str, Any], manifest: dict[str, Any],
                          requirements_revision: str, runs: list[dict[str, Any]], dispatch_id: str) -> dict[str, int]:
    """Refuse a ledger that is not tonight's evidence for this candidate, else count its results.

    Every pass or fail must come from tonight's run of the producer that owns its lane, at that
    producer's pin, about this candidate image, started after the cut: nothing is carried from an
    older ledger. A producer whose lanes hold no observation at all was skipped and refuses too.

    `runs` is what this nightly's own dispatch step recorded and `dispatch_id` the correlation id it
    passed every producer (R20/R21). Pins, candidate and cut cannot tell tonight's run from another
    run of the same producer at the same pins, and the content-addressed evidence_uri names no run,
    so each receipt carries the run that observed it (identity.producer_run) inside the bytes its
    evidence_digest addresses. A cell without it, or naming any other run, attempt or dispatch, is
    refused: a same-pin post-cut run this nightly did not dispatch never binds.
    """
    candidate = nightly_candidate(manifest)
    problems: list[str] = []
    if not isinstance(dispatch_id, str) or not DISPATCH_ID_RE.fullmatch(dispatch_id):
        problems.append(f"dispatch id {dispatch_id!r} is not a nightly correlation id")
    if ledger.get("schema") != LEDGER_SCHEMA:
        problems.append(f"schema {ledger.get('schema')!r}")
    if ledger.get("requirements_source_revision") != requirements_revision:
        problems.append(f"requirements_source_revision {ledger.get('requirements_source_revision')!r} is not {requirements_revision}")
    if ledger.get("requirements_revision") != catalog.get("revision") or ledger.get("requirements_complete") is not True:
        problems.append("requirements revision or completeness differs from the staged catalog")
    if ledger.get("candidate") != candidate:
        problems.append(f"candidate {ledger.get('candidate')!r} is not {candidate}")
    cut = _timestamp(candidate["cut_at"])
    succeeded = {row["producer"]: row for row in runs if row.get("conclusion") == "success"}
    owners: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in (entry for entry in catalog["production"]["producers"] if entry["cells"]):
        run = succeeded.get(entry["producer"])
        if run is None:
            problems.append(f"producer {entry['producer']} has no successful run tonight")
            continue
        if run.get("head_sha") != catalog["source_revisions"][entry["source_revision_key"]]["commit"]:
            problems.append(f"producer {entry['producer']} ran at {run.get('head_sha')}, not its staged pin")
        if run.get("dispatch_id") != dispatch_id:
            problems.append(f"producer {entry['producer']} run {run.get('run_id')} was not dispatched by {dispatch_id}")
        owners.append((entry, run))
    cells = [cell for cell in ledger.get("cells") or [] if isinstance(cell, dict)]
    expected = sorted(tuple(str(row.get(key)) for key in LEDGER_IDENTITY) for row in catalog["requirements"])
    actual = sorted(tuple(str(cell.get(key)) for key in LEDGER_IDENTITY) for cell in cells)
    if actual != expected:
        problems.append(f"cells do not match the staged catalog ({len(actual)} cells for {len(expected)} requirements)")
    counts: dict[str, int] = {}
    observed: set[str] = set()
    for cell in cells:
        result = str(cell.get("result"))
        counts[result] = counts.get(result, 0) + 1
        label = f"{cell.get('client_lane')} {cell.get('operation')}"
        if cell.get("source_sha") not in (None, candidate["source_sha"]):
            problems.append(f"{label}: source_sha {cell.get('source_sha')} is not the candidate")
        if result not in ("pass", "fail"):
            continue
        run = next((run for entry, run in owners if selects(entry, cell)), None)
        if run is None:
            problems.append(f"{label}: {result} from no producer dispatched tonight")
            continue
        observed.add(run["producer"])
        if cell.get("source_sha") != candidate["source_sha"] or cell.get("image_digest") != candidate["image_digest"]:
            problems.append(f"{label}: not about candidate {candidate['source_sha']} {candidate['image_digest']}")
        if cell.get("producer_source_sha") != run["head_sha"]:
            problems.append(f"{label}: producer_source_sha {cell.get('producer_source_sha')} is not {run['producer']} pin {run['head_sha']}")
        started = _timestamp(cell.get("started_at"))
        if started is None or cut is None or started < cut:
            problems.append(f"{label}: started_at {cell.get('started_at')!r} predates the candidate cut")
        receipt = cell.get("evidence_receipt")
        identity = receipt.get("identity") if isinstance(receipt, dict) else None
        if not isinstance(identity, dict) or "producer_run" not in identity:
            problems.append(f"{label}: {result} carries no producer run identity")
            continue
        if identity.get("client_driver") != cell.get("client_driver"):
            problems.append(f"{label}: receipt driver {identity.get('client_driver')!r} is not the cell driver "
                            f"{cell.get('client_driver')!r}")
        digest = receipt_digest(receipt)
        if cell.get("evidence_digest") != digest:
            problems.append(f"{label}: evidence_digest is not the digest of its receipt")
        elif cell.get("evidence_uri") != EVIDENCE_URI + digest.removeprefix("sha256:"):
            problems.append(f"{label}: evidence_uri does not address its receipt")
        expected = dispatched_run(run, dispatch_id)
        if identity["producer_run"] != expected:
            problems.append(f"{label}: producer run {identity['producer_run']!r} is not {run['producer']} run "
                            f"{run.get('run_id')} attempt {run.get('run_attempt')} dispatched by {dispatch_id}")
    for producer in sorted(set(succeeded) - observed):
        problems.append(f"producer {producer} contributed no observation for its lanes")
    if problems:
        shown = " | ".join(problems[:40]) + (f" | ... {len(problems) - 40} more" if len(problems) > 40 else "")
        raise Finding(f"nightly ledger refused; protocolCertification.ledger stays pending: {shown}")
    return counts


def locate_nightly_ledger(gh: Any, repository: str, path: str, requirements_revision: str,
                          candidate: dict[str, str], since: str) -> tuple[str, bytes]:
    """The one honua-evidence trunk commit since `since` whose ledger is for this staging and candidate."""
    matches = []
    for commit in gh.commits(repository, "trunk", path, since):
        data = gh.raw(repository, path, commit)
        document = json.loads(data)
        if document.get("requirements_source_revision") == requirements_revision and document.get("candidate") == candidate:
            matches.append((commit, data))
    if len(matches) != 1:
        raise Finding(f"expected one {repository} ledger commit for requirements {requirements_revision} "
                      f"and candidate {candidate['source_sha']} since {since}, found {len(matches)}")
    return matches[0]


def bind_nightly_ledger(root: Path, evidence_commit: str, ledger_bytes: bytes, requirements_revision: str) -> dict[str, Any]:
    """Write the verified ledger coordinates into the candidate manifest."""
    path = root / MANIFEST
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    ledger = manifest["protocolCertification"]["ledger"]
    if ledger.get("status") != "pending":
        raise Finding("NIGHTLY-BIND expects a pending ledger")
    ledger.update({
        "status": "bound",
        "commit": checked_sha(evidence_commit, "evidence commit"),
        "requirementsSourceRevision": checked_sha(requirements_revision, "requirements revision"),
        "sha256": "sha256:" + hashlib.sha256(ledger_bytes).hexdigest(),
    })
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return ledger


def nightly_bind(root: Path, gh: Any, requirements_revision: str, runs: list[dict[str, Any]], since: str,
                 dispatch_id: str) -> dict[str, Any]:
    manifest = load_manifest(root)
    ledger = manifest["protocolCertification"]["ledger"]
    commit, data = locate_nightly_ledger(gh, ledger["repository"], ledger["path"], requirements_revision,
                                         nightly_candidate(manifest), since)
    counts = verify_nightly_ledger(json.loads(data), load_json(root, CATALOG), manifest, requirements_revision, runs,
                                   dispatch_id)
    return {"ledger": bind_nightly_ledger(root, commit, data, requirements_revision), "results": counts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--requirements-revision")
    parser.add_argument("--evidence-commit")
    parser.add_argument("--ledger-sha256")
    parser.add_argument("--receipt-min", choices=("keep", "v1", "v2"), default="keep")
    parser.add_argument("--plan-output", type=Path, default=Path("rebind-plan.json"))
    parser.add_argument("--nightly-stage", action="store_true")
    parser.add_argument("--nightly-bind", action="store_true")
    parser.add_argument("--runs", type=Path, help="producer runs written by nightly_receipts.py wait-protocol")
    parser.add_argument("--since", help="ISO-8601 time the evidence aggregation was dispatched")
    parser.add_argument("--dispatch-id", help="correlation id the nightly passed every producer it dispatched")
    args = parser.parse_args(argv)
    try:
        if sum((args.apply, args.finalize, args.nightly_stage, args.nightly_bind)) > 1:
            raise Finding("--apply, --finalize, --nightly-stage and --nightly-bind are mutually exclusive")
        if args.nightly_stage:
            plan = nightly_stage(ROOT, GhCli())
            args.plan_output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
            print(human(plan, ROOT))
            print("\nNIGHTLY-STAGE complete. Commit the staged catalog with the candidate before dispatching producers.")
            return 0
        if args.nightly_bind:
            if not (args.requirements_revision and args.runs and args.since and args.dispatch_id):
                raise Finding("--nightly-bind requires --requirements-revision, --runs, --since and --dispatch-id")
            result = nightly_bind(ROOT, GhCli(), args.requirements_revision,
                                  json.loads(args.runs.read_text(encoding="utf-8")), args.since, args.dispatch_id)
            print(json.dumps(result, indent=2))
            return 0
        if args.finalize:
            if not all((args.requirements_revision, args.evidence_commit, args.ledger_sha256)):
                raise Finding("--finalize requires --requirements-revision, --evidence-commit, and --ledger-sha256")
            if not args.plan_output.is_file():
                raise Finding(f"--finalize requires the staged plan at {args.plan_output}")
            plan = json.loads(args.plan_output.read_text(encoding="utf-8"))
            finalize(ROOT, plan, args.requirements_revision, args.evidence_commit, args.ledger_sha256)
            print("FINALIZE complete. Open the single reviewed rebind PR with rebind-receipt.md as its body.")
            return 0
        plan, payloads, _ = prepare(ROOT, GhCli(), args.receipt_min)
        args.plan_output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        print(human(plan, ROOT))
        if args.apply:
            apply(ROOT, plan, payloads)
            print("\nAPPLY complete. Commit and push the staged catalog before evidence aggregation.")
        return 0
    except (Finding, subprocess.CalledProcessError) as exc:
        print(f"REBIND ABORTED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

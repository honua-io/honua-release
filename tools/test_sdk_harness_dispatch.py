"""sdk-harness-trunk-pinned-package-2026-10-27: SDK producers run their trunk harness against the pinned
published package, bound by digest; mcp and the SDKs receive the governed requirements reference."""
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest
import yaml

import nightly_receipts as nr

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / "certification/protocol-certification-requirements.v1.json").read_text())
MANIFEST = yaml.safe_load((ROOT / "platform-manifest.yaml").read_text())
SERVER = "a" * 40
DIGEST = "sha256:" + "b" * 64
REQUIREMENTS_REF = "c" * 40
DISPATCH_ID = "nightly-certification-77-1"


def night_manifest():
    manifest = copy.deepcopy(MANIFEST)
    manifest["components"]["honua-server"].update(
        sha=SERVER, digest=DIGEST, image=f"ghcr.io/honua-io/honua-server@{DIGEST}")
    manifest["protocolCertification"]["candidateCutAt"] = "2026-10-27T00:00:00Z"
    return manifest


def night_catalog():
    catalog = copy.deepcopy(CATALOG)
    for key in ("server", "server-certification"):
        catalog["source_revisions"][key]["commit"] = SERVER
    return catalog


class Api:
    """Every workflow declares every input it is sent; dispatches answer with run ids."""

    def __init__(self, declared=None):
        self.dispatches, self.declared, self.next_run = [], declared or {}, 100

    def __call__(self, method, path, body=None):
        if "/contents/.github/workflows/" in path:
            repository = "/".join(path.split("/")[1:3])
            workflow = path.split("/contents/.github/workflows/")[1].split("?")[0]
            names = self.declared.get((repository, workflow))
            if names is None:
                names = [nr.DISPATCH_ID_INPUT, *sorted(self.pending_inputs(repository, workflow))]
            text = yaml.safe_dump({"on": {"workflow_dispatch": {"inputs": {name: {"required": False} for name in names}}}})
            return {"type": "file", "encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if path.endswith("/commits/trunk"):
            return {"sha": "f" * 40}
        if method == "POST" and path.endswith("/git/refs"):
            return {}
        if "/git/ref/tags/nightly-candidate/" in path:
            sha = path.rsplit("/", 1)[1]
            return {"ref": f"refs/tags/nightly-candidate/{sha}", "object": {"type": "commit", "sha": sha}}
        if path.endswith("/dispatches"):
            self.next_run += 1
            repository = "/".join(path.split("/")[1:3])
            workflow = path.split("/workflows/")[1].split("/")[0]
            self.dispatches.append({"repository": repository, "workflow": workflow, **body})
            return {"workflow_run_id": self.next_run}
        if "/actions/runs/" in path:
            run_id = int(path.rsplit("/", 1)[1])
            row = self.dispatches[run_id - 101]
            sha = row["ref"].rsplit("/", 1)[1]
            return {"id": run_id, "event": "workflow_dispatch", "path": f".github/workflows/{row['workflow']}",
                    "head_sha": sha}
        raise AssertionError(f"unexpected {method} {path}")

    def pending_inputs(self, repository, workflow):
        entry = next(e for e in CATALOG["production"]["producers"]
                     if e["repository"] == repository and e["workflow"] == workflow)
        names = set(entry["inputs"])
        if entry["producer"] in {"honua-server-cite", "server-protocol-harness", "honua-sdk-js"}:
            names.add("requirements_context")
        return names


def by_producer(api):
    return {(row["repository"], row["workflow"]): row for row in api.dispatches}


def dispatch(catalog=None, manifest=None, requirements_ref=REQUIREMENTS_REF, api=None):
    api = api or Api()
    runs = nr.dispatch_protocol(api, catalog or night_catalog(), manifest or night_manifest(), DISPATCH_ID,
                                requirements_ref)
    return api, runs


@pytest.mark.parametrize("producer,artifact,harness", [
    ("honua-sdk-dotnet", "honua-sdk-dotnet", "sdk-dotnet-harness"),
    ("honua-sdk-python", "honua-sdk-python-wheel", "sdk-python-harness"),
])
def test_sdk_harness_runs_at_trunk_harness_against_the_pinned_package(producer, artifact, harness):
    api, runs = dispatch()
    entry = next(e for e in CATALOG["production"]["producers"] if e["producer"] == producer)
    sent = by_producer(api)[(entry["repository"], entry["workflow"])]
    pinned = MANIFEST["clientArtifacts"][artifact]
    harness_sha = CATALOG["source_revisions"][harness]["commit"]
    assert sent["ref"] == f"refs/tags/nightly-candidate/{harness_sha}"
    assert {key: sent["inputs"][key] for key in ("sdk_package", "sdk_package_version", "sdk_package_digest",
                                                  "sdk_source_sha", "requirements_ref", "requirements_revision")} == {
        "sdk_package": pinned["package"], "sdk_package_version": pinned["version"],
        "sdk_package_digest": pinned["digest"], "sdk_source_sha": pinned["sourceSha"],
        "requirements_ref": REQUIREMENTS_REF, "requirements_revision": CATALOG["revision"]}
    assert pinned["sourceSha"] == CATALOG["source_revisions"][entry["source_revision_key"]]["commit"]
    assert next(row for row in runs if row["producer"] == producer)["head_sha"] == harness_sha


def test_mcp_receives_the_governed_requirements_reference():
    api, _ = dispatch()
    sent = by_producer(api)[("honua-io/geospatial-mcp", "protocol-certification.yml")]
    assert sent["inputs"]["requirements_ref"] == REQUIREMENTS_REF
    assert sent["inputs"]["requirements_revision"] == CATALOG["revision"]


def test_a_night_without_the_requirements_reference_refuses_before_any_dispatch():
    api = Api()
    with pytest.raises(ValueError, match="governed placeholder"):
        dispatch(requirements_ref=None, api=api)
    assert api.dispatches == []


@pytest.mark.parametrize("change,problem", [
    (lambda m: m["clientArtifacts"]["honua-sdk-dotnet"].update(digest="sha256:XYZ"), "canonical digest"),
    (lambda m: m["clientArtifacts"]["honua-sdk-dotnet"].pop("digest"), "canonical digest"),
    (lambda m: m["clientArtifacts"]["honua-sdk-python-wheel"].update(sourceSha="9" * 40), "not the published package source"),
])
def test_a_package_pin_that_is_not_the_governed_published_artifact_refuses(change, problem):
    manifest = night_manifest()
    change(manifest)
    api = Api()
    with pytest.raises(ValueError, match=problem):
        dispatch(manifest=manifest, api=api)
    assert api.dispatches == []


def test_a_harness_producer_without_a_harness_revision_refuses():
    catalog = night_catalog()
    del catalog["source_revisions"]["sdk-dotnet-harness"]
    api = Api()
    with pytest.raises(ValueError, match="no harness revision"):
        dispatch(catalog=catalog, api=api)
    assert api.dispatches == []


def test_a_harness_workflow_without_the_package_inputs_refuses_before_any_dispatch():
    declared = {("honua-io/honua-sdk-python", "conformance.yml"): ["nightly_dispatch_id", "server_image"]}
    api = Api(declared)
    with pytest.raises(LookupError, match="does not declare"):
        dispatch(api=api)
    assert api.dispatches == []


def test_the_ruling_is_recorded_in_the_decision_overrides():
    overrides = json.loads((ROOT / "docs/2026.1-release-decision-overrides.json").read_text())
    ruling = overrides["rulings"]["sdk-harness-trunk-pinned-package-2026-10-27"]
    assert "founder" in ruling["owner"].lower()
    assert ruling["producers"] == ["honua-sdk-dotnet", "honua-sdk-python"]
    producers = {e["producer"]: e for e in CATALOG["production"]["producers"]}
    assert {name for name, e in producers.items() if e.get("harness_revision_key")} == set(ruling["producers"])

"""The devkit gate must reject substituted bytes and incomplete execution evidence."""
import hashlib
import copy
import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gp_devkit_artifacts as gate

SOURCE = "1" * 40
IMAGE = "sha256:" + "2" * 64
VERSION = "1.2.3-preview.1"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def evidence(tmp_path):
    package = tmp_path / f"Honua.Geoprocessing.Cli.{VERSION}.nupkg"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Honua.Geoprocessing.Cli.nuspec", f'''<package><metadata>
<id>Honua.Geoprocessing.Cli</id><version>{VERSION}</version>
<repository url="https://github.com/honua-io/honua-server" commit="{SOURCE}" />
</metadata></package>''')
        archive.writestr("tools/net10.0/any/honua-gp.dll", b"qualified devkit")
    root = tmp_path / "assembly"
    payload = {"workspace.py": "3" * 64}
    assembly = {"schema": "honua.gp.workspace-assembly/v1", "qualified": True,
                "image": IMAGE, "nupkg": {"version": VERSION, "sha256": gate.sha256(package)},
                "sdk": {"version": "10.0.302", "archiveSha256": "4" * 64},
                "pythonBase": "python@sha256:" + "5" * 64,
                "nuget": {"package/1.0": {"contentHash": "locked"}},
                "wheels": {"wheels": [{"sha256": "6" * 64}]},
                "bootstrap": {"files": payload, "devkitVersion": VERSION}}
    installed = {"image": IMAGE, "payload": payload, "devkitVersion": VERSION,
                 "checks": ["installed-bootstrap", "payload-hashes", "installed-serve", "human-binding",
                            "delegated-bind-denied", "workspace-grant", "immutable-source", "conditional-source",
                            "run-discovery", "sigterm-shutdown"]}
    fixtures = {"image": IMAGE, "devkitSha256": hashlib.sha256(b"qualified devkit").hexdigest(), "runs": []}
    for index, language in enumerate(("python", "dotnet")):
        run = {"language": language}
        for step, (name, status) in enumerate((("failed", "failed"), ("corrected", "succeeded"), ("cancelled", "cancelled"))):
            run[name] = {"schema": "honua.gp.workspace-run/v1", "runId": f"{index * 3 + step + 1:032x}",
                         "image": IMAGE, "network": "none", "status": status,
                         "snapshotDigest": f"{index * 3 + step + 1:064x}", "artifacts": []}
        output = root / "fixture-qualification/runs" / run["corrected"]["runId"] / "output/result/artifacts/features.geojson"
        write(output, {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": None, "properties": {}}]})
        artifact = {"path": "result/artifacts/features.geojson", "sha256": gate.sha256(output), "bytes": output.stat().st_size}
        run["corrected"]["artifacts"] = [artifact]
        run["geoJsonArtifact"] = artifact | {"sourceSnapshotDigest": run["corrected"]["snapshotDigest"], "absolutePath": "/ignored"}
        fixtures["runs"].append(run)
    write(root / "assembly.json", assembly)
    write(root / "installed-qualification/qualification.json", installed)
    write(root / "fixture-qualification/qualification.json", fixtures)
    return root, package


def test_binds_both_language_outputs_and_package(evidence):
    root, package = evidence
    candidate = gate.qualify(root, package, SOURCE)
    assert candidate["package"]["sha256"] == gate.sha256(package)
    assert candidate["imageConfigDigest"] == IMAGE
    assert {a["language"] for a in candidate["qualification"]["geoJsonArtifacts"]} == {"python", "dotnet"}


def test_requires_the_reviewed_python_digest_not_just_any_digest(evidence):
    root, package = evidence
    expected = "python@sha256:" + "5" * 64
    assert gate.qualify(root, package, SOURCE, expected_python_image=expected)["pythonBase"] == expected
    with pytest.raises(gate.ArtifactError, match="reviewed workflow input"):
        gate.qualify(root, package, SOURCE, expected_python_image="python@sha256:" + "6" * 64)


@pytest.mark.parametrize("receipt_name", ["failed", "corrected", "cancelled", "all"])
def test_rejects_relabelled_or_partly_reused_language_executions(evidence, receipt_name):
    root, package = evidence
    path = root / "fixture-qualification/qualification.json"
    fixtures = gate.load(path)
    python_run, dotnet_run = fixtures["runs"]
    if receipt_name == "all":
        fixtures["runs"][1] = copy.deepcopy(python_run) | {"language": "dotnet"}
    else:
        dotnet_run[receipt_name] = copy.deepcopy(python_run[receipt_name])
    write(path, fixtures)
    with pytest.raises(gate.ArtifactError, match="distinct across both languages"):
        gate.qualify(root, package, SOURCE)


@pytest.mark.parametrize("file, mutate, error", [
    ("assembly.json", lambda d: d.update(qualified=False), "completed qualification"),
    ("assembly.json", lambda d: d["nupkg"].update(sha256="0" * 64), "package bytes"),
    ("assembly.json", lambda d: d.update(nuget={}), "dependency provenance"),
    ("assembly.json", lambda d: d.update(pythonBase="python:latest"), "immutable Python"),
    ("assembly.json", lambda d: d["sdk"].update(archiveSha256=""), "SDK archive"),
    ("assembly.json", lambda d: d["bootstrap"].update(files={}), "payload/version"),
    ("installed-qualification/qualification.json", lambda d: d["checks"].remove("delegated-bind-denied"), "transport qualification"),
    ("installed-qualification/qualification.json", lambda d: d.update(image="sha256:" + "9" * 64), "another image"),
    ("installed-qualification/qualification.json", lambda d: d.update(payload={}), "payload differs"),
    ("fixture-qualification/qualification.json", lambda d: d["runs"].pop(), "Both Python and .NET"),
    ("fixture-qualification/qualification.json", lambda d: d.update(devkitSha256="0" * 64), "devkit assembly"),
    ("fixture-qualification/qualification.json", lambda d: d["runs"][1]["corrected"].update(status="failed"), "corrected success"),
    ("fixture-qualification/qualification.json", lambda d: d["runs"][0]["cancelled"].update(runId=d["runs"][0]["failed"]["runId"]), "Distinct actual"),
    ("fixture-qualification/qualification.json", lambda d: d["runs"][1]["failed"].update(image="sha256:" + "9" * 64), "exact image"),
    ("fixture-qualification/qualification.json", lambda d: d["runs"][1]["failed"].update(network="host"), "immutable snapshots"),
    ("fixture-qualification/qualification.json", lambda d: d["runs"][0]["failed"].update(snapshotDigest=d["runs"][0]["corrected"]["snapshotDigest"]), "Correction must change"),
    ("fixture-qualification/qualification.json", lambda d: d["runs"][0]["cancelled"].update(artifacts=[{}]), "cancellation"),
    ("fixture-qualification/qualification.json", lambda d: d["runs"][0]["failed"].update(artifacts=[{}]), "failure"),
    ("fixture-qualification/qualification.json", lambda d: d["runs"][0]["geoJsonArtifact"].update(sourceSnapshotDigest="0" * 64), "corrected execution"),
    ("fixture-qualification/qualification.json", lambda d: d["runs"][0]["geoJsonArtifact"].update(path="../features.geojson"), "inside its actual run"),
    ("fixture-qualification/qualification.json", lambda d: d["runs"][0]["geoJsonArtifact"].update(sha256="0" * 64), "declared successful"),
])
def test_rejects_incomplete_or_substituted_evidence(evidence, file, mutate, error):
    root, package = evidence
    document = gate.load(root / file)
    mutate(document)
    write(root / file, document)
    with pytest.raises(gate.ArtifactError, match=error):
        gate.qualify(root, package, SOURCE)


def test_rejects_package_from_another_source(evidence):
    root, package = evidence
    with pytest.raises(gate.ArtifactError, match="source binding"):
        gate.qualify(root, package, "0" * 40)


def test_rejects_changed_artifact_bytes(evidence):
    root, package = evidence
    output = next(root.rglob("features.geojson"))
    output.write_bytes(b"changed")
    with pytest.raises(gate.ArtifactError, match="GeoJSON bytes changed"):
        gate.qualify(root, package, SOURCE)


def test_rejects_symlinked_output_directory(evidence, tmp_path):
    root, package = evidence
    output = next(root.rglob("features.geojson"))
    original = output.parent
    moved = tmp_path / "external-output"
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)
    with pytest.raises(gate.ArtifactError, match="escaped"):
        gate.qualify(root, package, SOURCE)


def test_published_manifest_must_contain_qualified_image(evidence):
    root, package = evidence
    candidate = gate.qualify(root, package, SOURCE)
    manifest = json.dumps({"mediaType": "application/vnd.oci.image.manifest.v1+json", "config": {"digest": IMAGE}}).encode()
    reference = "ghcr.io/honua-io/honua-gp-workspace@sha256:" + hashlib.sha256(manifest).hexdigest()
    release = gate.published(candidate, reference, manifest)
    assert release["sandboxImage"] == reference
    assert f"gp-devkit-v{VERSION}" in release["package"]["url"]
    with pytest.raises(gate.ArtifactError, match="manifest digest"):
        gate.published(candidate, reference, manifest + b" ")
    candidate["imageConfigDigest"] = "sha256:" + "0" * 64
    with pytest.raises(gate.ArtifactError, match="not the locally qualified"):
        gate.published(candidate, reference, manifest)


def test_rejects_unqualified_image_index(evidence):
    root, package = evidence
    candidate = gate.qualify(root, package, SOURCE)
    manifest = json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json", "config": {"digest": IMAGE}}).encode()
    reference = "ghcr.io/honua-io/honua-gp-workspace@sha256:" + hashlib.sha256(manifest).hexdigest()
    with pytest.raises(gate.ArtifactError, match="platform manifest"):
        gate.published(candidate, reference, manifest)

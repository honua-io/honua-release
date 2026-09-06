"""Package inventory regressions with hashes computed from independent archives."""
import base64
import copy
import hashlib
import io
import json
import tarfile
import zipfile

import pytest
import yaml

import generate_platform_lock as generator
import platform_lock_bundle as bundle
import verify_client_artifacts as verifier


@pytest.fixture
def inputs(tmp_path):
    archives = {}
    clients = {}
    for name, package, version, revision in (
        ("renamed-primary", "@honua/sdk-js", "1.2.3", "a" * 40),
        ("mcp", "@honua/mcp-server", "4.5.6", "b" * 40),
    ):
        stream = io.BytesIO()
        metadata = json.dumps({"name": package, "version": version}).encode()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            member = tarfile.TarInfo("package/package.json")
            member.size = len(metadata)
            archive.addfile(member, io.BytesIO(metadata))
        archives[name] = stream.getvalue()
        clients[name] = {
            "ecosystem": "npm", "package": package, "version": version,
            "sourceSha": revision, "repository": "honua-io/sdk",
            "publicationState": "published",
            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(archives[name]).digest()).decode(),
        }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("Honua.Sdk.nuspec", '<package><metadata><id>Honua.Sdk</id>'
                         '<version>1.6.0</version></metadata></package>')
    archives["dotnet"] = stream.getvalue()
    clients["dotnet"] = {
        "ecosystem": "nuget", "package": "Honua.Sdk", "version": "1.6.0",
        "sourceSha": "c" * 40, "repository": "honua-io/honua-sdk-dotnet",
        "publicationState": "published", "registry": "github-packages",
        "digest": "sha256:" + hashlib.sha256(archives["dotnet"]).hexdigest(),
    }
    manifest = {
        "platformRelease": "2026.1.0-rc.1", "status": "rc",
        "components": {
            "sdk": {"repository": "https://github.com/honua-io/sdk", "sha": "d" * 40,
                    "artifact": "npm:@honua/sdk-js", "version": "1.2.3", "lifecycleStatus": "GA"},
            "honua-sdk-dotnet": {"repository": "https://github.com/honua-io/honua-sdk-dotnet",
                    "sha": "e" * 40, "artifact": "nuget:Honua.Sdk", "version": "1.6.0",
                    "lifecycleStatus": "GA"},
        },
        "clientArtifacts": clients,
    }
    return tmp_path, manifest, archives


def generate(inputs):
    root, manifest, _ = inputs
    path = root / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    matrix = root / "matrix.yaml"
    matrix.write_text("contracts: {}\n", encoding="utf-8")
    return generator.generate(path, matrix)


def test_all_published_packages_reach_lock_and_bom_with_exact_bytes(inputs):
    draft = generate(inputs)
    _, manifest, archives = inputs
    assert not any("$.clientArtifacts." in error for error in draft.unresolved)
    assert not any("artifacts[0].sha256" in error for error in draft.unresolved)
    assert len(draft.lock["components"]["sdk"]["artifacts"]) == 2
    assert draft.lock["components"]["sdk"]["source"]["revision"] == "d" * 40
    assert draft.lock["components"]["honua-sdk-dotnet"]["source"]["revision"] == "e" * 40
    bom = bundle.build_bom(draft.lock)
    assert len(bom["components"]) == 3
    for name, published in manifest["clientArtifacts"].items():
        npm = published["ecosystem"] == "npm"
        if npm:
            verifier._verify_npm_archive(archives[name], published["package"], published["version"])
        else:
            verifier._verify_nuget(archives[name], published["package"], published["version"])
        entries = [entry for entry in bom["components"] if any(
            prop == {"name": "honua:coordinate", "value": published["package"]}
            for prop in entry["properties"])]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["version"] == published["version"]
        assert entry["hashes"] == [{"alg": "SHA-512" if npm else "SHA-256", "content":
            (hashlib.sha512 if npm else hashlib.sha256)(archives[name]).hexdigest()}]
        assert {"name": "honua:artifactSourceRevision", "value": published["sourceSha"]} in entry["properties"]
    assert draft.unresolved  # Candidate metadata is still required; package seeding does not certify it.


@pytest.mark.parametrize("field,value", [("version", "9.9.9"), ("artifactSourceRevision", "f" * 40)])
def test_conflicting_primary_identity_is_refused_without_mixing_bytes(inputs, field, value):
    inputs[1]["components"]["sdk"][field] = value
    draft = generate(inputs)
    assert any("published identity conflicts" in error for error in draft.unresolved)
    primary = draft.lock["components"]["sdk"]["artifacts"][0]
    assert "integrity" not in primary
    assert primary["version" if field == "version" else "sourceRevision"] == value


def test_conflicting_nuget_hash_does_not_overwrite_component_evidence(inputs):
    digest = "sha256:" + "f" * 64
    inputs[1]["components"]["honua-sdk-dotnet"].update(
        artifactSha256=digest, artifactSourceRevision="c" * 40)
    draft = generate(inputs)
    assert any("conflicts with component artifact: sha256" in error for error in draft.unresolved)
    assert draft.lock["components"]["honua-sdk-dotnet"]["artifacts"][0]["sha256"] == digest


@pytest.mark.parametrize("mutation,reason", [
    (lambda m: m["clientArtifacts"]["mcp"].pop("repository"), "exactly one component"),
    (lambda m: m["clientArtifacts"]["mcp"].update(repository="honua-io/missing"), "exactly one component"),
    (lambda m: m["components"].update(other={**m["components"]["sdk"], "artifact": "npm:other"}), "exactly one component"),
    (lambda m: m["clientArtifacts"]["mcp"].pop("integrity"), "incomplete published identity"),
    (lambda m: m["clientArtifacts"].update(duplicate=copy.deepcopy(m["clientArtifacts"]["mcp"])), "duplicate published"),
    (lambda m: m["clientArtifacts"].update(mcp=None), "must be a mapping"),
])
def test_unresolved_package_inventory_never_silently_disappears(inputs, mutation, reason):
    mutation(inputs[1])
    draft = generate(inputs)
    assert any("$.clientArtifacts." in error and reason in error for error in draft.unresolved)


def test_secondary_cannot_be_assigned_to_an_unrelated_component(inputs):
    inputs[1]["clientArtifacts"]["renamed-primary"]["repository"] = "honua-io/honua-sdk-dotnet"
    assert any("$.clientArtifacts.renamed-primary" in error for error in generate(inputs).unresolved)

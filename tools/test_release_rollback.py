import json
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
import release_rollback as rollback  # noqa: E402


def _write(path: Path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _fixture(tmp_path: Path, compatible=True, name="prod"):
    lock_a = {
        "platform": {"id": "honua-2026.1-rc.1"},
        "components": {
            "server": {"artifact": "sha256:" + "a" * 64},
            "worker": {"artifact": "sha256:" + "b" * 64},
        },
        "contentDigests": {"config": "sha256:" + "c" * 64, "capability": "sha256:" + "d" * 64},
        "schema": "106", "rollbackCompatibility": {"schemaVersions": ["107"] if compatible else ["106"]},
    }
    lock_b = {
        "platform": {"id": "honua-2026.1-rc.2"},
        "components": {
            "server": {"artifact": "sha256:" + "e" * 64},
            "worker": {"artifact": "sha256:" + "f" * 64},
        },
        "contentDigests": {"config": "sha256:" + "1" * 64, "capability": "sha256:" + "2" * 64},
        "schema": "107", "rollbackCompatibility": {"schemaVersions": ["107"]},
    }
    a, b = _write(tmp_path / f"{name}-a.json", lock_a), _write(tmp_path / f"{name}-b.json", lock_b)
    planes = [
        {"id": "serving-east", "kind": "serving", "providerId": "deploy/east", "lockPath": "/components/server/artifact"},
        {"id": "serving-west", "kind": "serving", "providerId": "deploy/west", "lockPath": "/components/server/artifact"},
        {"id": "worker-default", "kind": "worker", "providerId": "queue/default", "lockPath": "/components/worker/artifact"},
        {"id": "config", "kind": "config", "providerId": "projection/config", "lockPath": "/contentDigests/config"},
        {"id": "capability", "kind": "capability", "providerId": "projection/capability", "lockPath": "/contentDigests/capability"},
    ]
    state = {"planes": {plane["providerId"]: {"kind": plane["kind"], "value": rollback.pointer(lock_b, plane["lockPath"])} for plane in planes}, "mutations": {}}
    state["planes"]["database"] = {"kind": "schema", "value": lock_b["schema"]}
    state_path = _write(tmp_path / f"{name}-provider.json", state)
    provider = Path(__file__).resolve().parent / "rollback_local_provider.py"
    env = {
        "name": name, "currentLockDigest": rollback.digest(b),
        "planes": planes,
        "schema": {"lockPath": "/schema", "providerId": "database", "compatibleVersions": ["107"] if compatible else ["106"]},
        "provider": {"command": [os.sys.executable, str(provider), "--state", str(state_path)]},
        "sourceInputs": {"candidateManifest": "sha256:" + "3" * 64, "compatibilityMatrix": "sha256:" + "4" * 64},
    }
    return a, b, _write(tmp_path / f"{name}-env.json", env)


def _run(tmp_path, a, b, env, **kwargs):
    return rollback.run(environment_path=env, from_path=b, to_path=a, store=tmp_path / "store",
                        receipt_path=tmp_path / "receipt.json", **kwargs)


def test_one_invocation_converges_every_lock_owned_plane(tmp_path):
    a, b, env = _fixture(tmp_path)
    result = _run(tmp_path, a, b, env)
    assert result["status"] == "Succeeded"
    assert {c["kind"] for c in result["children"]} == {"serving", "worker", "config", "capability", "schema"}
    assert all(c["state"] == "Verified" for c in result["children"])
    assert all(result["functionalSmoke"].values())


def test_controller_restart_resumes_same_parent_operation(tmp_path):
    a, b, env = _fixture(tmp_path)
    interrupted = _run(tmp_path, a, b, env, stop_after=2)
    assert interrupted["status"] == "Running"
    resumed = _run(tmp_path, a, b, env)
    assert resumed["id"] == interrupted["id"] and resumed["restartCount"] == 1
    assert resumed["status"] == "Succeeded"


def test_duplicate_invocation_folds_without_provider_mutations(tmp_path):
    a, b, env = _fixture(tmp_path)
    first = _run(tmp_path, a, b, env)
    duplicate = _run(tmp_path, a, b, env)
    assert duplicate["id"] == first["id"]
    assert duplicate["providerMutations"] == first["providerMutations"]


def test_existing_operation_cannot_be_retargeted(tmp_path):
    a, b, env = _fixture(tmp_path)
    _run(tmp_path, a, b, env, stop_after=1)
    changed = json.loads(a.read_text()); changed["components"]["server"]["artifact"] = "sha256:" + "9" * 64
    _write(a, changed)
    with pytest.raises(rollback.RollbackError, match="cannot be retargeted"):
        _run(tmp_path, a, b, env)


def test_target_failure_is_explicit_mixed_state_with_recovery(tmp_path):
    a, b, env = _fixture(tmp_path)
    value = json.loads(env.read_text())
    value["provider"]["command"][4:4] = ["--fail-provider", "deploy/west"]
    _write(env, value)
    result = _run(tmp_path, a, b, env)
    assert result["status"] == "ManualInterventionRequired"
    failed = next(c for c in result["children"] if c["id"] == "serving-west")
    assert failed["state"] == "Failed" and failed["recovery"]
    assert not all(result["functionalSmoke"].values())


def test_incompatible_forward_schema_never_claims_rollback(tmp_path):
    a, b, env = _fixture(tmp_path, compatible=False)
    result = _run(tmp_path, a, b, env)
    assert result["status"] == "ManualInterventionRequired"
    assert next(c for c in result["children"] if c["id"] == "schema")["state"] == "Failed"


def test_exact_from_lock_bytes_are_required(tmp_path):
    a, b, env = _fixture(tmp_path)
    b.write_text(b.read_text() + "\n")
    with pytest.raises(rollback.RollbackError, match="not on the declared"):
        _run(tmp_path, a, b, env)


def test_missing_worker_plane_cannot_reach_success(tmp_path):
    a, b, env = _fixture(tmp_path)
    value = json.loads(env.read_text()); value["planes"] = [p for p in value["planes"] if p["kind"] != "worker"]
    _write(env, value)
    with pytest.raises(rollback.RollbackError, match="worker"):
        _run(tmp_path, a, b, env)


def test_receipt_is_bound_to_both_exact_lock_digests(tmp_path):
    a, b, env = _fixture(tmp_path)
    _run(tmp_path, a, b, env)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["fromLockDigest"] == rollback.digest(b)
    assert receipt["toLockDigest"] == rollback.digest(a)
    assert receipt["rollbackClock"]["terminalAt"]
    assert receipt["sourceInputs"]["candidateManifest"].startswith("sha256:")


def test_provider_is_actuated_and_observed_before_success(tmp_path):
    a, b, env = _fixture(tmp_path)
    result = _run(tmp_path, a, b, env)
    assert all(child["providerEvidence"] for child in result["children"])
    provider_state = json.loads((tmp_path / "prod-provider.json").read_text())
    assert len(provider_state["mutations"]) == 5
    assert all(evidence["ok"] for evidence in result["functionalSmokeEvidence"].values())


def test_failed_functional_probe_cannot_claim_success(tmp_path):
    a, b, env = _fixture(tmp_path)
    value = json.loads(env.read_text())
    value["provider"]["command"][4:4] = ["--fail-probe", "worker"]
    _write(env, value)
    result = _run(tmp_path, a, b, env)
    assert result["status"] == "ManualInterventionRequired"
    assert result["functionalSmoke"]["worker"] is False
    assert next(child for child in result["children"] if child["kind"] == "worker")["state"] == "Failed"


def _retained_server_image(tmp_path, monkeypatch, registry_docker):
    """A second, earlier honua-server index served beside the fixture registry, so the retained
    lock is a distinct server identity rather than another component's image."""
    fixture = Path(__file__).resolve().parent / "fixtures/image-platforms/honua-server/index.json"
    index = json.loads(fixture.read_text())
    platforms = {}
    for child in index["manifests"]:
        architecture = child["platform"]["architecture"]
        if child["platform"]["os"] == "linux":
            child["digest"] = "sha256:" + hashlib.sha256(f"retained-{architecture}".encode()).hexdigest()
            platforms[architecture] = child["digest"]
    raw = json.dumps(index, indent=2).encode()
    image = dict(registry_docker["honua-server"], digest="sha256:" + hashlib.sha256(raw).hexdigest(),
                 platformDigests=platforms)
    (tmp_path / "retained-index.json").write_bytes(raw)
    binary = tmp_path / "retained-bin/docker"
    binary.parent.mkdir()
    fixture_docker = shutil.which("docker")
    binary.write_text(f"#!{sys.executable}\n"
                      "import os, sys\n"
                      f"if sys.argv[4:5] == [{image['coordinate'] + '@' + image['digest']!r}]:\n"
                      f"    sys.stdout.buffer.write(open({str(tmp_path / 'retained-index.json')!r}, 'rb').read())\n"
                      "    sys.exit(0)\n"
                      f"os.execv({fixture_docker!r}, [{fixture_docker!r}, *sys.argv[1:]])\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    return image


def test_certifier_consumes_exact_frozen_source_bytes(tmp_path, monkeypatch, registry_docker):
    manifest = tmp_path / "platform-manifest.yaml"
    matrix = tmp_path / "compatibility-matrix.yaml"
    manifest.write_text("platformRelease: 2026.1\n", encoding="utf-8")
    matrix.write_text("contracts: {}\n", encoding="utf-8")

    def sha(path):
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def exact_lock(image, schema):
        return {
            "sourceInputs": {
                "platformManifest": {"path": manifest.name, "sha256": sha(manifest)},
                "compatibilityMatrix": {"path": matrix.name, "sha256": sha(matrix)},
            },
            "components": {"honua-server": {
                "schemaVersions": {"database": schema},
                "artifacts": [image],
            }},
        }

    retained = _write(tmp_path / "retained.json", exact_lock(_retained_server_image(tmp_path, monkeypatch, registry_docker), "107"))
    candidate = _write(tmp_path / "candidate.json", exact_lock(registry_docker["honua-server"], "107"))
    output = tmp_path / "certification"
    script = Path(__file__).resolve().parent / "certify_release_rollback.py"
    result = subprocess.run([
        os.sys.executable, str(script), "--output", str(output), "--from-lock", str(retained),
        "--to-lock", str(candidate), "--candidate-manifest", str(manifest),
        "--compatibility-matrix", str(matrix),
    ], check=False, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr + result.stdout
    receipt = json.loads((output / "success-receipt.json").read_text())
    assert receipt["sourceInputs"]["platformManifest"] == sha(manifest)
    assert receipt["sourceInputs"]["compatibilityMatrix"] == sha(matrix)

    retained_value = json.loads(retained.read_text())
    retained_value["components"]["honua-server"]["schemaVersions"]["database"] = "106"
    _write(retained, retained_value)
    incompatible_output = tmp_path / "incompatible-certification"
    incompatible = subprocess.run([
        os.sys.executable, str(script), "--output", str(incompatible_output), "--from-lock", str(retained),
        "--to-lock", str(candidate), "--candidate-manifest", str(manifest),
        "--compatibility-matrix", str(matrix),
    ], check=False, text=True, capture_output=True)
    assert incompatible.returncode == 1
    refused = json.loads((incompatible_output / "success-receipt.json").read_text())
    assert refused["status"] == "ManualInterventionRequired"
    assert next(child for child in refused["children"] if child["kind"] == "schema")["state"] == "Failed"


@pytest.mark.parametrize("role", ["CANDIDATE", "RETAINED"])
def test_certifier_refuses_another_components_image_as_server(tmp_path, monkeypatch, registry_docker, role):
    """Both indexes verify against the registry; only the coordinate shows the console image is
    standing in for honua-server, so certification must refuse before any provider mutation."""
    manifest = tmp_path / "platform-manifest.yaml"
    matrix = tmp_path / "compatibility-matrix.yaml"
    manifest.write_text("platformRelease: 2026.1\n", encoding="utf-8")
    matrix.write_text("contracts: {}\n", encoding="utf-8")
    images = {"CANDIDATE": registry_docker["honua-server"],
              "RETAINED": _retained_server_image(tmp_path, monkeypatch, registry_docker)}
    images[role] = registry_docker["honua-console"]
    locks = {}
    for name, image in images.items():
        locks[name] = _write(tmp_path / f"{name.lower()}.json", {
            "sourceInputs": {"platformManifest": {"sha256": rollback.digest(manifest)},
                             "compatibilityMatrix": {"sha256": rollback.digest(matrix)}},
            "components": {"honua-server": {"schemaVersions": {"database": "107"}, "artifacts": [image]}},
        })
    output = tmp_path / "certification"
    script = Path(__file__).resolve().parent / "certify_release_rollback.py"
    result = subprocess.run([
        os.sys.executable, str(script), "--output", str(output), "--from-lock", str(locks["RETAINED"]),
        "--to-lock", str(locks["CANDIDATE"]), "--candidate-manifest", str(manifest),
        "--compatibility-matrix", str(matrix),
    ], check=False, text=True, capture_output=True)
    assert result.returncode == 1
    assert f"ROLLBACK_{role}_IMAGE_COORDINATE_MISMATCH: honua-server: 'ghcr.io/honua-io/honua-console'" in (
        result.stdout + result.stderr)
    assert not (output / "success-receipt.json").exists()


# First-lock certification extends the retained-lock tests above without changing them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_rollback_target as targets
import certify_release_rollback as certification


def test_generated_registry_lock_certifies_one_operation(tmp_path, registry_docker):
    import generate_platform_lock as generator
    root = Path(__file__).resolve().parents[1]
    manifest, matrix = root / "platform-manifest.yaml", root / "compatibility-matrix.yaml"
    draft = generator.generate(manifest, matrix, image_inspector=generator.registry_image_platform_digests)
    for name, expected in registry_docker.items():
        image = next(artifact for artifact in draft.lock["components"][name]["artifacts"] if artifact["kind"] == "image")
        assert image["platformDigests"] == expected["platformDigests"]
    # This exercises generated identities locally. Unresolved release fields still block signing.
    candidate = _write(tmp_path / "candidate.json", draft.lock)
    target = tmp_path / "retained/platform-lock.json"
    report = targets.resolve(candidate, target, "honua-io/honua-release", lambda *args: "[[]]")
    report.update(schema="honua.rollback-gate/v1", overall_status="pending")
    report_path = _write(tmp_path / "resolution.json", report)
    output = tmp_path / "certification"
    assert certification.main(["--from-lock", str(target), "--to-lock", str(candidate),
                               "--candidate-manifest", str(manifest), "--compatibility-matrix", str(matrix),
                               "--output", str(output), "--gate-report", str(report_path)]) == 0
    receipt = json.loads((output / "success-receipt.json").read_text())
    serving = [child for child in receipt["children"] if child["kind"] in {"serving", "worker"}]
    assert all(child["observed"] == registry_docker["honua-server"]["platformDigests"]["amd64"] for child in serving)
    assert receipt["status"] == "Succeeded" and receipt["restartCount"] == 1
    assert all(receipt["functionalSmoke"].values())


def _release(tag, lock=False, published_at=None):
    return {"tag_name": tag, "draft": False, "published_at": published_at or tag,
            "assets": [{"name": "platform-lock.json"}] if lock else []}


def _target_fixture(tmp_path, pages, reject=False):
    candidate = _write(tmp_path / "candidate.json", {"platform": {"id": "honua-2026.1.1-rc.1"}})
    target = tmp_path / "retained" / "platform-lock.json"
    calls = []

    def command(*args):
        calls.append(args)
        if args[0] == "api":
            assert "--paginate" in args and "--slurp" in args
            return json.dumps(pages)
        if args[:2] == ("release", "download"):
            _write(target, {"platform": {"id": args[2]}})
        if args[0] == "attestation" and reject:
            raise targets.Finding("invalid signature")
        return "verified"

    return candidate, target, calls, command


@pytest.mark.parametrize("pages", [[[]], [[_release("honua-2026.1")]]])
def test_first_lock_detection_preserves_exact_candidate_bytes(tmp_path, pages):
    candidate, target, calls, command = _target_fixture(tmp_path, pages)
    report = targets.resolve(candidate, target, "honua-io/honua-release", command)
    assert report == {
        "first_lock_bearing_release": True, "no_earlier_lock_exists": True,
        "candidate_lock_digest": rollback.digest(candidate), "rollback_target_digest": rollback.digest(candidate),
        "retained_release": None, "scanned_releases": [r["tag_name"] for page in pages for r in page],
        "reason": "no_earlier_attested_platform_lock_exists",
    }
    assert target.read_bytes() == candidate.read_bytes()
    assert len(calls) == 1  # No download of an absent asset and no reconstructed lock.


def test_retained_lock_on_later_page_uses_ordinary_path(tmp_path):
    candidate, target, calls, command = _target_fixture(tmp_path, [
        [_release("honua-2026.2")], [_release("honua-2026.1", lock=True)]])
    report = targets.resolve(candidate, target, "honua-io/honua-release", command)
    assert report["first_lock_bearing_release"] is False
    assert report["no_earlier_lock_exists"] is False
    assert report["retained_release"] == "honua-2026.1"
    assert report["rollback_target_digest"] != report["candidate_lock_digest"]
    assert calls[-1][:2] == ("attestation", "verify")


def test_candidate_release_is_not_misidentified_as_an_earlier_lock(tmp_path):
    candidate, target, calls, command = _target_fixture(tmp_path, [[
        _release("honua-2026.1.1-rc.1", lock=True), _release("honua-2026.1")]])
    assert targets.resolve(candidate, target, "honua-io/honua-release", command)["first_lock_bearing_release"]


def test_candidate_ga_and_newer_release_are_not_rollback_targets(tmp_path):
    candidate, target, calls, command = _target_fixture(tmp_path, [[
        _release("honua-2026.2", lock=True, published_at="2026-09-13T00:00:00Z"),
        _release("honua-2026.1.1", lock=True, published_at="2026-09-12T00:00:00Z"),
        _release("honua-2026.1", lock=True, published_at="2026-09-11T00:00:00Z")]])
    report = targets.resolve(candidate, target, "honua-io/honua-release", command)
    assert report["retained_release"] == "honua-2026.1"
    download = next(call for call in calls if call[:2] == ("release", "download"))
    assert download[2] == "honua-2026.1"


def test_unpublished_candidate_uses_release_version_boundary(tmp_path):
    candidate, target, calls, command = _target_fixture(tmp_path, [[
        _release("honua-2026.2", lock=True), _release("honua-2026.1", lock=True)]])
    report = targets.resolve(candidate, target, "honua-io/honua-release", command)
    assert report["retained_release"] == "honua-2026.1"


def test_unverified_retained_lock_cannot_enable_self_rollback(tmp_path):
    candidate, target, calls, command = _target_fixture(tmp_path, [[_release("honua-2026.1", lock=True)]], reject=True)
    with pytest.raises(targets.Finding, match="ROLLBACK_RETAINED_ATTESTATION_FAILED"):
        targets.resolve(candidate, target, "honua-io/honua-release", command)
    assert target.read_bytes() != candidate.read_bytes()


def test_release_lookup_failure_is_a_named_finding(tmp_path, monkeypatch):
    def unavailable(*args):
        raise targets.Finding("ROLLBACK_RETAINED_LOOKUP_FAILED: unavailable")
    monkeypatch.setattr(targets, "resolve", unavailable)
    report = tmp_path / "gate-report.json"
    assert targets.main(["--candidate", "missing", "--target", "missing", "--repository", "honua-io/honua-release",
                         "--report", str(report)]) == 1
    result = json.loads(report.read_text())
    assert result["overall_status"] == "fail"
    assert "ROLLBACK_RETAINED_LOOKUP_FAILED" in result["finding"]
    assert "first_lock_bearing_release" not in result


@pytest.mark.parametrize("tamper", [False, True, "missing_image_digest", "wrong_architecture"])
def test_first_lock_report_and_real_operation(tmp_path, tamper, registry_docker):
    manifest = tmp_path / "platform-manifest.yaml"
    matrix = tmp_path / "compatibility-matrix.yaml"
    manifest.write_text("platformRelease: 2026.1.1-rc.1\n")
    matrix.write_text("contracts: {}\n")
    candidate = _write(tmp_path / "candidate.json", {
        "platform": {"id": "honua-2026.1.1-rc.1"},
        "sourceInputs": {"platformManifest": {"sha256": rollback.digest(manifest)},
                         "compatibilityMatrix": {"sha256": rollback.digest(matrix)}},
        "components": {"honua-server": {"schemaVersions": {"database": "107"},
            "artifacts": [registry_docker["honua-server"]]}},
    })
    if tamper == "missing_image_digest":
        value = json.loads(candidate.read_text())
        del value["components"]["honua-server"]["artifacts"][0]["platformDigests"]
        _write(candidate, value)
    if tamper == "wrong_architecture":
        value = json.loads(candidate.read_text())
        platforms = value["components"]["honua-server"]["artifacts"][0]["platformDigests"]
        platforms["amd64"], platforms["arm64"] = platforms["arm64"], platforms["amd64"]
        _write(candidate, value)
    target = tmp_path / "retained" / "platform-lock.json"
    report = targets.resolve(candidate, target, "honua-io/honua-release", lambda *args: "[[]]")
    report.update(schema="honua.rollback-gate/v1", overall_status="pending")
    report_path = _write(tmp_path / "resolution.json", report)
    if tamper is True:
        target.write_bytes(target.read_bytes() + b"\n")
    output = tmp_path / "certification"
    status = certification.main(["--from-lock", str(target), "--to-lock", str(candidate),
        "--candidate-manifest", str(manifest), "--compatibility-matrix", str(matrix),
        "--output", str(output), "--gate-report", str(report_path)])
    result = json.loads((output / "gate-report.json").read_text())
    assert result["first_lock_bearing_release"] is True
    assert result["no_earlier_lock_exists"] is True
    assert result["candidate_lock_digest"] == rollback.digest(candidate)
    if tamper:
        assert status == 1
        assert result["overall_status"] == "fail"
        finding = ("ROLLBACK_SELF_TARGET_MISMATCH" if tamper is True else
                   "ROLLBACK_CANDIDATE_IMAGE_PLATFORM_DIGEST_INVALID" if tamper == "wrong_architecture" else
                   "ROLLBACK_CANDIDATE_AMD64_IMAGE_DIGEST_MISSING")
        assert finding in result["finding"]
        if tamper == "wrong_architecture":
            assert "registry Linux architecture identities" in result["finding"]
            assert not (output / "success-provider.json").exists()
        return
    assert status == 0
    assert result["overall_status"] == "pass"
    assert result["post_rollback_state_equals_lock"] is True
    receipt = json.loads((output / "success-receipt.json").read_text())
    assert receipt["fromLockDigest"] == receipt["toLockDigest"] == rollback.digest(candidate)
    assert receipt["restartCount"] == 1
    assert len(receipt["providerMutations"]) == 5
    assert all(c["state"] == "Verified" and c["observed"] == c["expected"] for c in receipt["children"])
    assert all(receipt["functionalSmoke"].values())
    mixed = json.loads((output / "mixed-state-receipt.json").read_text())
    assert mixed["status"] == "ManualInterventionRequired"
    assert any(c["state"] == "Failed" for c in mixed["children"])


@pytest.mark.parametrize("label,accepted", [
    ("2026.1-rc.3", True),
    ("2026.1.2-rc.4", True),
    ("1.2.3-rc.1", True),
    ("2027.1-rc.1", False),
    ("2026.1", False),
    ("2026.1.2", False),
    ("honua-2026.1-rc.3", False),
    ("2026.1-beta.3", False),
    ("2026.1.2.3-rc.1", False),
    ("2026.1-rc.0", False),
    ("2026.1-rc.01", False),
    ("2026.1-rc.3-extra", False),
    ("prefix-2026.1-rc.3", False),
    ("2026.1-rc.3\n", False),
    ("invalid\n2026.1-rc.3", False),
])
def test_request_promotion_candidate_label_validation(tmp_path, label, accepted):
    import fetch_promotion_evidence as fetcher
    from datetime import datetime, timezone

    class NoRequests(fetcher.GitHub):
        def pages(self, path, key):
            return []

    promotions = tmp_path / "promotions"
    promotions.mkdir()
    _write(promotions / f"{label}.json", {
        "platformLabel": label, "rcTrainRunId": "123",
        "lock": {"burnStartedAt": "2026-09-28T00:00:00Z"},
    })
    selected = fetcher.candidates(promotions, NoRequests("honua-io/honua-release"),
                                  now=datetime(2026, 10, 2, tzinfo=timezone.utc), published=lambda _: False)
    assert selected == ([{"label": label, "record": f"certification/promotions/{label}.json",
                          "rcTrainRunId": "123"}] if accepted else [])


@pytest.mark.parametrize("candidate_tag,expected_tag", [
    ("honua-2026.1-rc.3", "honua-2026.1.0"),
    ("honua-2026.1.2-rc.4", "honua-2026.1.2"),
])
def test_rollback_uses_published_ga_tag(tmp_path, candidate_tag, expected_tag):
    assert targets.promoted_tag(candidate_tag) == expected_tag
    # Finding the actual GA publication excludes locks published after it.
    candidate, target, calls, command = _target_fixture(tmp_path, [[
        _release("honua-2026.2-rc.1", lock=True, published_at="2026-09-13T00:00:00Z"),
        _release(expected_tag, lock=True, published_at="2026-09-12T00:00:00Z"),
        _release("honua-2025.1.0", lock=True, published_at="2026-09-11T00:00:00Z"),
    ]])
    _write(candidate, {"platform": {"id": candidate_tag}})
    report = targets.resolve(candidate, target, "honua-io/honua-release", command)
    assert report["retained_release"] == "honua-2025.1.0"
    assert report["scanned_releases"] == ["honua-2025.1.0"]
    download = next(call for call in calls if call[:2] == ("release", "download"))
    assert download[2] == "honua-2025.1.0"

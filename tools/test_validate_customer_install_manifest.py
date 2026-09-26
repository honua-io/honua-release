"""The customer-install-manifest.json publication gate must be able to fail on every rule it claims."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_customer_install_manifest as vcim  # noqa: E402

DRIFTED_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "customer-install-manifest-drifted-pin.json"


@pytest.fixture(scope="module")
def committed():
    return (
        vcim.load_customer_manifest(vcim.CUSTOMER_MANIFEST_PATH),
        json.loads(vcim.SCHEMA_PATH.read_text(encoding="utf-8")),
        vcim._load_yaml(vcim.PLATFORM_MANIFEST_PATH),
        vcim._load_yaml(vcim.LEDGER_PATH),
    )


def _errors(committed, mutate=None, *, platform_mutate=None, ledger=None):
    document, schema, platform, committed_ledger = (copy.deepcopy(part) for part in committed)
    if mutate:
        mutate(document)
    if platform_mutate:
        platform_mutate(platform)
    return vcim.validate(document, schema, platform, committed_ledger if ledger is None else ledger)


def _candidate(committed):
    server = committed[2]["components"]["honua-server"]
    return server["digest"], server["sha"]


def test_committed_customer_manifest_passes(committed):
    assert _errors(committed) == []


def test_drifted_pin_fixture_fails_with_one_precise_message(committed):
    document = vcim.load_customer_manifest(DRIFTED_FIXTURE)
    _, schema, platform, ledger = committed
    errors = vcim.validate(document, schema, platform, ledger)
    assert errors == [
        "$.clients.honua-sdk.digest: 'sha256:" + "0" * 64 + "' drifted from platform-manifest.yaml "
        "clientArtifacts.honua-sdk-python-wheel.digest "
        f"{platform['clientArtifacts']['honua-sdk-python-wheel']['digest']!r}"
    ]


def test_cli_exits_nonzero_on_the_drifted_fixture(capsys):
    assert vcim.main([str(DRIFTED_FIXTURE)]) == 1
    assert "$.clients.honua-sdk.digest" in capsys.readouterr().err
    assert vcim.main([]) == 0


def test_fixture_differs_from_the_committed_manifest_only_in_the_drifted_pin(committed):
    fixture = vcim.load_customer_manifest(DRIFTED_FIXTURE)
    fixture["clients"]["honua-sdk"]["digest"] = committed[0]["clients"]["honua-sdk"]["digest"]
    assert fixture == committed[0]


@pytest.mark.parametrize(("mutate", "expected"), [
    (lambda d: d.pop("cleanWindowsQualification"), "'cleanWindowsQualification' is a required property"),
    (lambda d: d.update(exactCandidateQualification="false"), "$.exactCandidateQualification: 'false' is not of type 'boolean'"),
    (lambda d: d.update(schemaVersion=2), "$.schemaVersion: 1 was expected"),
    (lambda d: d.update(status="certified"), "$.status: 'certified' is not one of"),
    (lambda d: d.update(sourceManifest="other.yaml"), "$.sourceManifest: 'platform-manifest.yaml' was expected"),
    (lambda d: d.update(verifiedAt="2026-02-30"), "$.verifiedAt: '2026-02-30' is not a calendar date"),
    (lambda d: d["server"].update(image="ghcr.io/honua-io/honua-server:nightly"), "$.server.image: 'ghcr.io/honua-io/honua-server:nightly' does not match"),
    (lambda d: d["server"].update(sourceSha="5a657b9"), "$.server.sourceSha: '5a657b9' does not match"),
    (lambda d: d["clients"]["honua-sdk"].update(digest="sha256:abc"), "$.clients.honua-sdk.digest: 'sha256:abc' does not match"),
    (lambda d: d["clients"]["honua-sdk"].pop("filename"), "'filename' is a required property"),
    (lambda d: d["clients"]["mcp"].update(extra=True), "Additional properties are not allowed ('extra' was unexpected)"),
    (lambda d: d["supportingImages"].update(redis="redis:7.4-alpine"), "$.supportingImages.redis: 'redis:7.4-alpine' does not match"),
    (lambda d: d["alternativeClients"]["honua-sdk-js"].pop("integrity"), "'integrity' is a required property"),
])
def test_malformed_entries_fail_the_schema(committed, mutate, expected):
    assert any(expected in error for error in _errors(committed, mutate))


def test_pre_cut_rehearsal_cannot_claim_qualification(committed):
    errors = _errors(committed, lambda d: d.update(exactCandidateQualification=True))
    assert "$.status: a pre-cut-rehearsal profile cannot claim exact-candidate or clean-Windows qualification" in errors


def test_clean_windows_qualification_requires_exact_candidate(committed):
    errors = _errors(committed, lambda d: d.update(cleanWindowsQualification=True))
    assert any(error.startswith("$.cleanWindowsQualification: true requires exactCandidateQualification true") for error in errors)


def test_release_candidate_must_be_the_exact_certified_candidate(committed):
    errors = _errors(committed, lambda d: d.update(status="release-candidate", exactCandidateQualification=True))
    assert any(error.startswith("$.exactCandidateQualification: true but the server is not the certified candidate") for error in errors)


def test_release_candidate_matching_the_candidate_must_be_in_the_ledger(committed):
    digest, sha = _candidate(committed)

    def to_candidate(d):
        d.update(status="release-candidate", exactCandidateQualification=True)
        d["server"].update(image=f"ghcr.io/honua-io/honua-server@{digest}", sourceSha=sha,
                           manifestUrl=f"https://ghcr.io/v2/honua-io/honua-server/manifests/{digest}")

    errors = _errors(committed, to_candidate)
    assert errors == [f"$.server.image: release-candidate digest {digest} is not recorded in any compatibility-ledger platform lock"]
    ledger = {"platformLocks": {"sha256:" + "a" * 64: {"platformLock": {"components": {"honua-server": {
        "source": {"revision": sha}, "artifacts": [{"kind": "image", "digest": digest}]}}}}}}
    assert _errors(committed, to_candidate, ledger=ledger) == []


def test_rehearsal_server_may_differ_from_the_candidate_but_never_half_match(committed):
    digest, sha = _candidate(committed)

    def same_sha_other_digest(d):
        d["server"]["sourceSha"] = sha

    errors = _errors(committed, same_sha_other_digest)
    assert any(error.startswith("$.server: half-matches the certified candidate") for error in errors)

    def same_digest_other_sha(d):
        d["server"].update(image=f"ghcr.io/honua-io/honua-server@{digest}",
                           manifestUrl=f"https://ghcr.io/v2/honua-io/honua-server/manifests/{digest}")

    errors = _errors(committed, same_digest_other_sha)
    assert any(error.startswith("$.server: half-matches the certified candidate") for error in errors)


def test_server_candidate_drift_in_the_platform_manifest_is_detected(committed):
    rehearsal = committed[0]["server"]

    def candidate_takes_rehearsal_digest(p):
        p["components"]["honua-server"]["digest"] = rehearsal["image"].split("@", 1)[1]

    errors = _errors(committed, platform_mutate=candidate_takes_rehearsal_digest)
    assert any(error.startswith("$.server: half-matches the certified candidate") for error in errors)


def test_server_image_repository_and_manifest_url_are_bound(committed):
    errors = _errors(committed, lambda d: d["server"].update(image=d["server"]["image"].replace("honua-server@", "other@")))
    assert any("$.server.image: repository 'ghcr.io/honua-io/other' differs" in error for error in errors)
    errors = _errors(committed, lambda d: d["server"].update(manifestUrl=d["server"]["manifestUrl"][:-1] + "0"))
    assert any(error.startswith("$.server.manifestUrl: must be") for error in errors)


def test_ledger_lock_naming_the_server_digest_must_agree_on_the_commit(committed):
    digest = committed[0]["server"]["image"].split("@", 1)[1]
    ledger = {"platformLocks": {"sha256:" + "b" * 64: {"platformLock": {"components": {"honua-server": {
        "source": {"revision": "c" * 40}, "artifacts": [{"kind": "image", "digest": digest}]}}}}}}
    errors = _errors(committed, ledger=ledger)
    assert errors == [f"$.server.sourceSha: {committed[0]['server']['sourceSha']} disagrees with compatibility-ledger "
                      f"platform lock honua-server source revision {'c' * 40} for digest {digest}"]


@pytest.mark.parametrize(("section", "key", "field", "value"), [
    ("clients", "honua-admin", "version", "0.1.9"),
    ("clients", "honua-admin", "sourceSha", "0" * 40),
    ("clients", "honua-admin", "repository", "honua-io/honua-admin"),
    ("clients", "honua-sdk", "filename", "honua_sdk-0.1.11-py3-none-any2.whl"),
    ("alternativeClients", "honua-sdk-js", "integrity", "sha512-AAAA"),
    ("alternativeClients", "honua-mcp-server", "sourceSha", "1" * 40),
    ("alternativeClients", "honua-sdk-dotnet", "digest", "sha256:" + "2" * 64),
    ("alternativeClients", "honua-sdk-dotnet", "registry", "nuget.org"),
    ("alternativeClients", "honua-sdk-js", "targets", ["node"]),
])
def test_every_copied_honua_identity_field_is_compared(committed, section, key, field, value):
    def drift(d):
        d[section][key][field] = value

    assert any(error.startswith(f"$.{section}.{key}.{field}: {value!r} drifted from platform-manifest.yaml clientArtifacts.")
               for error in _errors(committed, drift))


def test_platform_manifest_pin_moving_without_the_customer_copy_fails(committed):
    def bump(p):
        p["clientArtifacts"]["honua-admin-python-wheel"]["version"] = "0.1.9"

    assert any(error.startswith("$.clients.honua-admin.version: '0.1.8' drifted") for error in _errors(committed, platform_mutate=bump))


def test_honua_client_must_carry_its_source_identity(committed):
    errors = _errors(committed, lambda d: d["clients"]["honua-sdk"].pop("sourceSha"))
    assert any(error.startswith("$.clients.honua-sdk.sourceSha: required for a Honua client") for error in errors)


def test_unpinned_honua_client_fails_but_third_party_transport_does_not(committed):
    def add_unpinned(d):
        extra = copy.deepcopy(d["clients"]["mcp"])
        extra.update(package="honua-cli", filename="honua_cli-2.1.1-py3-none-any.whl",
                     downloadUrl="https://files.pythonhosted.org/packages/00/honua_cli-2.1.1-py3-none-any.whl",
                     metadataUrl="https://pypi.org/pypi/honua-cli/2.1.1/json")
        d["clients"]["honua-cli"] = extra

    assert _errors(committed, add_unpinned) == [
        "$.clients.honua-cli: Honua client pypi:honua-cli is not pinned in platform-manifest.yaml clientArtifacts"]
    assert not any("$.clients.mcp" in error for error in _errors(committed))


def test_pin_source_must_resolve(committed):
    errors = _errors(committed, lambda d: d["alternativeClients"]["honua-sdk-js"].update(
        pinSource="platform-manifest.yaml#clientArtifacts.honua-sdk-js-typo"))
    assert "$.alternativeClients.honua-sdk-js.pinSource: platform-manifest.yaml has no clientArtifacts.honua-sdk-js-typo" in errors


def test_pypi_urls_must_name_the_pinned_file_and_version(committed):
    errors = _errors(committed, lambda d: d["clients"]["mcp"].update(
        downloadUrl=d["clients"]["mcp"]["downloadUrl"].replace("mcp-2.1.1", "mcp-2.1.0")))
    assert "$.clients.mcp.downloadUrl: does not download 'mcp-2.1.1-py3-none-any.whl'" in errors
    errors = _errors(committed, lambda d: d["clients"]["honua-sdk"].update(metadataUrl="https://pypi.org/pypi/honua-sdk/0.1.10/json"))
    assert any(error.startswith("$.clients.honua-sdk.metadataUrl: must be 'https://pypi.org/pypi/honua-sdk/0.1.11/json'") for error in errors)


def test_client_listed_twice_fails(committed):
    def duplicate(d):
        d["alternativeClients"]["honua-sdk-python"] = {
            **{k: v for k, v in d["clients"]["honua-sdk"].items() if k in ("ecosystem", "package", "version", "digest", "repository", "sourceSha")},
            "publicationState": "published", "targets": ["python3"], "credentialsRequired": False,
            "pinSource": "platform-manifest.yaml#clientArtifacts.honua-sdk-python-wheel",
        }

    assert any(error.startswith("$.alternativeClients.honua-sdk-python: pypi:honua-sdk is already listed at $.clients.honua-sdk")
               for error in _errors(committed, duplicate))


def test_duplicate_json_keys_are_refused(tmp_path):
    path = tmp_path / "customer-install-manifest.json"
    path.write_text('{"schemaVersion": 1, "schemaVersion": 1}', encoding="utf-8")
    with pytest.raises(vcim.ManifestLoadError, match="duplicate JSON key 'schemaVersion'"):
        vcim.load_customer_manifest(path)
    assert vcim.main([str(path)]) == 2

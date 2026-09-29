"""#57: the anonymous first-publication preflight must not invent package bytes."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest
import yaml

import first_publication_preflight as preflight

WHEEL = b"pinned-wheel"
ARCHIVE = b"iac-archive"
NUPKG = b"nupkg-bytes"
SYMBOL = b"symbol-bytes"
GRPC = b"grpc-bytes"
BSR = b"bsr-bytes"
TARBALL_DEPS = {"@honua/sdk-js": "0.1.10-beta.0", "maplibre-gl": "6.1.0"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tarball(dependencies: dict) -> bytes:
    output = io.BytesIO()
    document = json.dumps({"dependencies": dependencies}).encode()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path in (
            "package/templates/react-ts/package.json",
            "package/templates/vanilla-ts/package.json",
        ):
            info = tarfile.TarInfo(path)
            info.size = len(document)
            archive.addfile(info, io.BytesIO(document))
    return output.getvalue()


def _manifest() -> dict:
    return {
        "clientArtifacts": {
            "honua-sdk-dotnet": {
                "package": "Honua.Sdk",
                "version": "1.6.0",
                "registry": "github-packages",
            },
            "honua-sdk-js": {
                "package": "@honua/sdk-js",
                "version": "0.1.9-beta.0",
                "integrity": "sha512-test",
            },
            "honua-sdk-python-wheel": {
                "package": "honua-sdk",
                "version": "0.1.11",
                "filename": "honua_sdk-0.1.11.whl",
                "digest": "sha256:" + _sha(WHEEL),
            },
            "honua-admin-python-wheel": {
                "package": "honua-admin",
                "version": "0.1.8",
                "filename": "honua_admin-0.1.8.whl",
                "digest": "sha256:" + _sha(WHEEL),
            },
        },
        "components": {
            "honua-sdk-dotnet": {"version": "1.6.2"},
            "honua-iac": {
                "artifact": "archive:https://github.com/honua-io/honua-iac/archive/refs/tags/v0.1.0.tar.gz",
                "artifactSha256": "sha256:" + _sha(ARCHIVE),
            },
        },
    }


class World:
    def __init__(self, *, nuget_versions=None, dependencies=None, grpc_body=GRPC, bsr_body=BSR, sdk_next_status=404):
        self.sdk_next_status = sdk_next_status
        self.nuget_versions = nuget_versions or ["1.6.4", "1.10.0"]
        self.dependencies = dependencies or dict(TARBALL_DEPS)
        self.grpc_body = grpc_body
        self.bsr_body = bsr_body
        self.tarball = _tarball(self.dependencies)
        self.seen = []

    def get(self, url: str) -> preflight.Response:
        self.seen.append(url)
        status, body = self._response(url)
        return preflight.Response(status, body, url)

    def _response(self, url: str) -> tuple[int, bytes]:
        if url.endswith("/index.json") and "/v3-flatcontainer/" in url:
            package = url.split("/v3-flatcontainer/")[1].split("/")[0]
            if package.startswith("honua.mobile.") or package == "absent.sdk":
                return 404, b"{}"
            if package.startswith("honua.") or package == "geospatial.grpc":
                versions = ["1.0.0"] if package == "geospatial.grpc" else self.nuget_versions
                return 200, json.dumps({"versions": versions}).encode()
        if url.endswith("/honua.sdk/1.6.0/honua.sdk.1.6.0.nupkg"):
            return 404, b"missing"
        if url.endswith("/honua.sdk.studio/1.6.0/honua.sdk.studio.1.6.0.nupkg"):
            return 404, b"missing"
        if "/v3-flatcontainer/honua.sdk/" in url and url.endswith(".nupkg"):
            return 200, NUPKG
        if url.endswith(".snupkg") and "globalcdn.nuget.org" in url:
            return 200, SYMBOL
        if url.endswith(".snupkg"):
            return 404, b"no-flat-symbol"
        if url == preflight.GRPC_NUPKG_URL:
            return 200, self.grpc_body
        if url == preflight.BSR_ZIP_URL:
            return 200, self.bsr_body
        if url == "https://pypi.org/pypi/honua-esri-assess/json":
            return 404, b"missing"
        if url.startswith("https://pypi.org/pypi/") and url.endswith("/json"):
            package = url.split("/pypi/")[1].split("/")[0]
            version = {"honua-sdk": "0.1.11", "honua-admin": "0.1.8"}.get(package, "0.7.1")
            filename = {
                "honua-sdk": "honua_sdk-0.1.11.whl",
                "honua-admin": "honua_admin-0.1.8.whl",
            }.get(package, "honua_migrate-0.7.1.whl")
            body = json.dumps({
                "info": {"version": version},
                "releases": {version: [{
                    "filename": filename,
                    "url": f"https://files.pythonhosted.org/packages/aa/{filename}",
                    "digests": {"sha256": _sha(WHEEL)},
                }]},
            }).encode()
            return 200, body
        if url.startswith("https://files.pythonhosted.org/"):
            return 200, WHEEL
        if url == preflight.npm_version_url("@honua/sdk-js", "0.1.9-beta.0"):
            return 200, json.dumps({"dist": {"integrity": "sha512-test"}}).encode()
        if url == preflight.npm_version_url("@honua/sdk-js", "0.1.10-beta.0"):
            if self.sdk_next_status == 200:
                return 200, json.dumps({"dist": {"integrity": "sha512-next"}}).encode()
            return 404, b"missing"
        if url == preflight.npm_package_url("@honua-io/embed"):
            return 404, b"missing"
        if url == preflight.npm_package_url("create-honua-app"):
            body = json.dumps({
                "dist-tags": {"latest": "0.1.4"},
                "versions": {"0.1.4": {"dist": {
                    "tarball": "https://registry.npmjs.org/create-honua-app/-/create-honua-app-0.1.4.tgz",
                    "shasum": hashlib.sha1(self.tarball).hexdigest(),
                }}},
            }).encode()
            return 200, body
        if url.endswith("create-honua-app-0.1.4.tgz"):
            return 200, self.tarball
        if url.startswith("https://ghcr.io/token"):
            return 403, b'{"errors":[{"code":"DENIED"}]}'
        if url == "https://api.github.com/repos/honua-io/honua-qgis-plugin":
            return 404, b'{"message":"Not Found"}'
        if url == "https://plugins.qgis.org/plugins/honua/":
            return 404, b"missing"
        if url == "https://registry.terraform.io/v1/modules/honua-io":
            return 404, b'{"errors":["No module versions found"]}'
        if url.startswith("https://github.com/honua-io/honua-iac/archive/"):
            return 200, ARCHIVE
        raise AssertionError(f"unexpected URL {url}")


def _receipt(**world_kwargs) -> dict:
    world = World(**world_kwargs)
    return preflight.build_receipt(
        world,
        _manifest(),
        observed_at="2026-09-26T00:00:00Z",
        retained={"grpc_sha256": _sha(world.grpc_body), "bsr_sha256": _sha(world.bsr_body)},
    )


def _channel(receipt: dict, channel_id: str) -> dict:
    return next(channel for channel in receipt["channels"] if channel["id"] == channel_id)


def test_anonymous_probe_sends_no_authorization_header():
    assert "Authorization" not in preflight.ANONYMOUS_HEADERS
    request_headers = dict(preflight.ANONYMOUS_HEADERS)
    assert "authorization" not in {key.lower() for key in request_headers}


def test_fixture_world_names_the_operator_boundaries_without_invented_digests():
    receipt = _receipt()
    preflight.audit(receipt)
    newest = _channel(receipt, "nuget:Honua.Sdk-newest")
    assert newest["disposition"] == "published"
    assert newest["files"][0]["sha256"] == _sha(NUPKG)
    train = _channel(receipt, "train:honua-sdk-dotnet")
    assert train["disposition"] == "blocked-on-train-binding"
    assert "files" not in train
    assert train["client_artifact_version"] == "1.6.0"
    mobile = _channel(receipt, "nuget:Honua.Mobile.Sdk")
    assert mobile["disposition"] == "blocked-on-operator"
    assert "files" not in mobile
    kinds = {blocker["kind"] for blocker in receipt["blockers"]}
    assert kinds == {
        "blocked-on-train-binding",
        "blocked-on-operator",
        "blocked-on-candidate",
        "blocked-on-republish",
    }
    publications = {blocker["publication"] for blocker in receipt["blockers"]}
    assert "nuget.org Honua.Sdk 1.6.0" in publications
    assert "npmjs @honua-io/embed" in publications
    assert "oci://ghcr.io/honua-io/charts/honua" in publications
    assert "QGIS plugin honua" in publications
    embed = next(blocker for blocker in receipt["blockers"] if blocker["publication"] == "npmjs @honua-io/embed")
    assert "npm.pkg.github.com" in embed["boundary"]
    assert "NUGET_API_KEY" not in embed["boundary"]
    mobile_blocker = next(
        blocker for blocker in receipt["blockers"] if blocker["publication"].startswith("nuget.org Honua.Mobile")
    )
    assert "nuget.pkg.github.com/honua-io/index.json" in mobile_blocker["boundary"]
    helm = next(blocker for blocker in receipt["blockers"] if blocker["kind"] == "blocked-on-candidate")
    assert "v<semver>-aot" in helm["boundary"]
    assert _channel(receipt, "iac:git-archive")["disposition"] == "supported-by-git-url"
    assert _channel(receipt, "pypi:honua-esri-assess")["disposition"] == "absent-retired"
    assert not any(blocker["channels"] == ["iac:git-archive"] for blocker in receipt["blockers"])


def test_a_listed_index_does_not_become_the_pinned_package():
    receipt = _receipt()
    listed = _channel(receipt, "nuget:Honua.Sdk")
    assert listed["disposition"] == "listed"
    assert listed["versions"] == ["1.6.4", "1.10.0"]
    assert "files" not in listed
    assert "1.6.0" not in listed["versions"]


def test_published_without_downloaded_bytes_fails_audit():
    receipt = _receipt()
    channel = _channel(receipt, "pypi:honua-migrate")
    channel["files"] = []
    with pytest.raises(preflight.PreflightError, match="without downloaded bytes"):
        preflight.audit(receipt)


def test_listing_must_not_carry_a_digest():
    receipt = _receipt()
    _channel(receipt, "nuget:Honua.Sdk")["files"] = [{
        "filename": "invented.nupkg",
        "sha256": "ab" * 32,
        "url": "https://api.nuget.org/invented.nupkg",
    }]
    with pytest.raises(preflight.PreflightError, match="must not carry package bytes"):
        preflight.audit(receipt)


def test_retained_grpc_mismatch_is_not_recorded_as_published():
    world = World(grpc_body=b"different-bytes")
    with pytest.raises(preflight.PreflightError, match="does not match retained"):
        preflight.build_receipt(world, _manifest(), observed_at="2026-09-26T00:00:00Z")


def test_pypi_byte_mismatch_fails_closed():
    world = World()
    original = world._response

    def mismatch(url: str):
        if url.startswith("https://files.pythonhosted.org/"):
            return 200, b"not-the-wheel"
        return original(url)

    world._response = mismatch
    with pytest.raises(preflight.PreflightError, match="does not match the PyPI sha256"):
        preflight.build_receipt(
            world,
            _manifest(),
            observed_at="2026-09-26T00:00:00Z",
            retained={"grpc_sha256": _sha(GRPC), "bsr_sha256": _sha(BSR)},
        )


def test_template_pins_come_from_the_tarball_and_block_republish():
    receipt = _receipt()
    create = _channel(receipt, "npm:create-honua-app")
    assert create["disposition"] == "blocked-on-republish"
    assert create["files"][0]["sha256"] == _sha(world_tarball())
    assert create["templates"][0]["dependencies"] == TARBALL_DEPS
    blocker = next(item for item in receipt["blockers"] if item["kind"] == "blocked-on-republish")
    assert "0.1.10-beta.0" in blocker["boundary"]
    assert "GHSA-jrc7-96c5-q579" in blocker["boundary"]
    assert "honua-sdk-js" in blocker["boundary"]


def world_tarball() -> bytes:
    return _tarball(TARBALL_DEPS)


def test_a_fixed_template_is_not_a_republish_blocker():
    receipt = _receipt(dependencies={"@honua/sdk-js": "0.1.9-beta.0", "maplibre-gl": "6.9.0"})
    assert _channel(receipt, "npm:create-honua-app")["disposition"] == "published"
    assert not any(item["kind"] == "blocked-on-republish" for item in receipt["blockers"])


def test_a_resolvable_off_manifest_template_sdk_pin_still_blocks_republish():
    # The manifest pins 0.1.9-beta.0; a template pin that merely resolves on npm is off-train.
    receipt = _receipt(dependencies={"@honua/sdk-js": "0.1.10-beta.0", "maplibre-gl": "6.9.0"}, sdk_next_status=200)
    create = _channel(receipt, "npm:create-honua-app")
    assert create["sdk_pin_statuses"] == {"0.1.10-beta.0": 200}
    assert create["disposition"] == "blocked-on-republish"
    blocker = next(item for item in receipt["blockers"] if item["kind"] == "blocked-on-republish")
    assert "manifest pins @honua/sdk-js 0.1.9-beta.0" in blocker["boundary"]


def test_manifest_pin_appearing_on_nuget_is_not_silently_adopted():
    world = World(nuget_versions=["1.6.0", "1.10.0"])
    original = world._response

    def present(url: str):
        if url.endswith("/1.6.0/honua.sdk.1.6.0.nupkg") or url.endswith("/1.6.0/honua.sdk.studio.1.6.0.nupkg"):
            return 200, NUPKG
        return original(url)

    world._response = present
    with pytest.raises(preflight.PreflightError, match="record downloaded bytes and rebind"):
        preflight.build_receipt(
            world,
            _manifest(),
            observed_at="2026-09-26T00:00:00Z",
            retained={"grpc_sha256": _sha(GRPC), "bsr_sha256": _sha(BSR)},
        )


def test_drift_names_the_channel_and_ignores_observed_at():
    receipt = _receipt()
    other = json.loads(json.dumps(receipt))
    other["observed_at"] = "2026-09-27T00:00:00Z"
    preflight.compare(receipt, other)
    other["channels"][0]["http_status"] = 500
    with pytest.raises(preflight.PreflightError, match="drifted at"):
        preflight.compare(receipt, other)


def test_refuses_a_registry_host_outside_the_allowlist():
    with pytest.raises(preflight.PreflightError, match="allowlist"):
        preflight._require_host("https://evil.example/package.nupkg")


def test_committed_receipt_matches_the_manifest_pin_and_audits():
    manifest = yaml.safe_load((preflight.REPO_ROOT / "platform-manifest.yaml").read_text())
    receipt = preflight.load_receipt(preflight.RECEIPT_PATH)
    preflight.audit(receipt)
    train = _channel(receipt, "train:honua-sdk-dotnet")
    pinned = manifest["clientArtifacts"]["honua-sdk-dotnet"]
    assert train["client_artifact_version"] == pinned["version"]
    assert train["client_artifact_registry"] == pinned["registry"]
    assert train["disposition"] == "blocked-on-train-binding"
    archive = _channel(receipt, "iac:git-archive")
    assert archive["files"][0]["sha256"] == manifest["components"]["honua-iac"]["artifactSha256"].removeprefix("sha256:")
    sdk = _channel(receipt, "pypi:honua-sdk")
    pinned_python = manifest["clientArtifacts"]["honua-sdk-python-wheel"]
    match = next(item for item in sdk["files"] if item["filename"] == pinned_python["filename"])
    assert "sha256:" + match["sha256"] == pinned_python["digest"]
    grpc = _channel(receipt, "nuget:Geospatial.Grpc")
    assert grpc["files"][0]["sha256"] == preflight.GRPC_NUPKG_SHA256
    assert not any(channel.get("files") for channel in receipt["channels"] if channel["id"].startswith("nuget:Honua.Mobile"))

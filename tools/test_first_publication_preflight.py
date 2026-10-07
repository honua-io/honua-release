"""#57: the anonymous first-publication preflight must not invent package bytes."""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import tarfile
import zipfile

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
SDK_SOURCE = "a" * 40
MCP_SOURCE = "b" * 40


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tarball(dependencies: dict) -> bytes:
    output = io.BytesIO()
    document = json.dumps({"dependencies": dependencies}).encode()
    # Pin the gzip header mtime: "w:gz" stamps the current time, so two builds that straddle a
    # second boundary produced different bytes and the tarball-digest assertions flaked.
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed,             tarfile.open(fileobj=compressed, mode="w") as archive:
        for path in (
            "package/templates/react-ts/package.json",
            "package/templates/vanilla-ts/package.json",
        ):
            info = tarfile.TarInfo(path)
            info.size = len(document)
            archive.addfile(info, io.BytesIO(document))
    return output.getvalue()


def _npm_tarball(package: str, version: str) -> bytes:
    output = io.BytesIO()
    document = json.dumps({"name": package, "version": version}).encode()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed, \
            tarfile.open(fileobj=compressed, mode="w") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(document)
        archive.addfile(info, io.BytesIO(document))
    return output.getvalue()


def _sri(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


SDK_TARBALL = _npm_tarball("@honua/sdk-js", "0.1.9-beta.0")
MCP_TARBALL = _npm_tarball("@honua/mcp-server", "0.1.4-beta.0")


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
                "integrity": _sri(SDK_TARBALL),
                "sourceSha": SDK_SOURCE,
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
    def __init__(self, *, nuget_versions=None, dependencies=None, grpc_body=GRPC, bsr_body=BSR, sdk_next_status=404,
                 sdk_git_head=SDK_SOURCE, sdk_tarball=SDK_TARBALL):
        self.sdk_next_status = sdk_next_status
        self.sdk_git_head = sdk_git_head
        self.sdk_tarball = sdk_tarball
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
            return 200, json.dumps({"gitHead": self.sdk_git_head, "dist": {
                "integrity": _sri(SDK_TARBALL),
                "tarball": "https://registry.npmjs.org/@honua/sdk-js/-/sdk-js-0.1.9-beta.0.tgz",
            }}).encode()
        if url == "https://registry.npmjs.org/@honua/sdk-js/-/sdk-js-0.1.9-beta.0.tgz":
            return 200, self.sdk_tarball
        if url == preflight.npm_version_url("@honua/mcp-server", "0.1.4-beta.0"):
            return 200, json.dumps({"gitHead": MCP_SOURCE, "dist": {
                "integrity": _sri(MCP_TARBALL),
                "tarball": "https://registry.npmjs.org/@honua/mcp-server/-/mcp-server-0.1.4-beta.0.tgz",
            }}).encode()
        if url == "https://registry.npmjs.org/@honua/mcp-server/-/mcp-server-0.1.4-beta.0.tgz":
            return 200, MCP_TARBALL
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


def test_pinned_npm_sdk_is_downloaded_bytes_bound_to_its_source():
    receipt = _receipt()
    sdk = _channel(receipt, "npm:@honua/sdk-js")
    assert sdk["disposition"] == "published"
    assert sdk["evidence_class"] == "downloaded-bytes"
    assert sdk["source_sha"] == SDK_SOURCE
    assert sdk["files"] == [{
        "filename": "sdk-js-0.1.9-beta.0.tgz",
        "registry_integrity": _sri(SDK_TARBALL),
        "sha256": _sha(SDK_TARBALL),
        "url": "https://registry.npmjs.org/@honua/sdk-js/-/sdk-js-0.1.9-beta.0.tgz",
    }]
    assert "npm:@honua/mcp-server" not in {channel["id"] for channel in receipt["channels"]}
    preflight.audit(receipt, _manifest())


def test_pinned_npm_companion_is_downloaded_too():
    manifest = _manifest()
    manifest["clientArtifacts"]["honua-mcp-server"] = {
        "package": "@honua/mcp-server",
        "version": "0.1.4-beta.0",
        "integrity": _sri(MCP_TARBALL),
        "sourceSha": MCP_SOURCE,
        "filename": "mcp-server-0.1.4-beta.0.tgz",
    }
    world = World()
    receipt = preflight.build_receipt(
        world, manifest, observed_at="2026-10-04T00:00:00Z",
        retained={"grpc_sha256": _sha(world.grpc_body), "bsr_sha256": _sha(world.bsr_body)},
    )
    mcp = _channel(receipt, "npm:@honua/mcp-server")
    assert mcp["disposition"] == "published"
    assert mcp["source_sha"] == MCP_SOURCE
    assert mcp["files"][0]["sha256"] == _sha(MCP_TARBALL)
    preflight.audit(receipt, manifest)
    receipt["channels"] = [channel for channel in receipt["channels"] if channel["id"] != "npm:@honua/mcp-server"]
    with pytest.raises(preflight.PreflightError, match="npm:@honua/mcp-server receipt does not record"):
        preflight.audit(receipt, manifest)


def test_npm_git_head_other_than_the_pinned_source_fails_closed():
    with pytest.raises(preflight.PreflightError, match="gitHead .* not clientArtifacts.honua-sdk-js.sourceSha"):
        _receipt(sdk_git_head="c" * 40)


def test_npm_tarball_bytes_other_than_the_integrity_fail_closed():
    with pytest.raises(preflight.PreflightError, match="tarball does not match the npm integrity"):
        _receipt(sdk_tarball=_npm_tarball("@honua/sdk-js", "0.1.10-beta.0"))


def test_receipt_for_another_npm_pin_fails_the_manifest_audit():
    receipt = _receipt()
    manifest = _manifest()
    manifest["clientArtifacts"]["honua-sdk-js"]["sourceSha"] = "d" * 40
    with pytest.raises(preflight.PreflightError, match="npm:@honua/sdk-js receipt does not record"):
        preflight.audit(receipt, manifest)


def test_committed_receipt_matches_the_manifest_pin_and_audits():
    manifest = yaml.safe_load((preflight.REPO_ROOT / "platform-manifest.yaml").read_text())
    receipt = preflight.load_receipt(preflight.RECEIPT_PATH)
    preflight.audit(receipt)
    train = _channel(receipt, "train:honua-sdk-dotnet")
    pinned = manifest["clientArtifacts"]["honua-sdk-dotnet"]
    assert train["client_artifact_version"] == pinned["version"]
    assert train["client_artifact_registry"] == pinned["registry"]
    assert train["disposition"] == "published"
    assert train["evidence_class"] == "downloaded-bytes"
    sdk_file = next(item for item in train["files"] if item["filename"] == pinned["filename"])
    assert "sha256:" + sdk_file["sha256"] == pinned["digest"]
    assert not any(b["kind"] == "blocked-on-train-binding" for b in receipt["blockers"])
    archive = _channel(receipt, "iac:git-archive")
    assert archive["files"][0]["sha256"] == manifest["components"]["honua-iac"]["artifactSha256"].removeprefix("sha256:")
    sdk = _channel(receipt, "pypi:honua-sdk")
    pinned_python = manifest["clientArtifacts"]["honua-sdk-python-wheel"]
    match = next(item for item in sdk["files"] if item["filename"] == pinned_python["filename"])
    assert "sha256:" + match["sha256"] == pinned_python["digest"]
    grpc = _channel(receipt, "nuget:Geospatial.Grpc")
    assert grpc["files"][0]["sha256"] == preflight.GRPC_NUPKG_SHA256
    for row in ("honua-sdk-js", "honua-mcp-server"):
        pinned_npm = manifest["clientArtifacts"][row]
        npm = _channel(receipt, f"npm:{pinned_npm['package']}")
        assert npm["disposition"] == "published"
        assert npm["source_sha"] == pinned_npm["sourceSha"]
        assert npm["files"][0]["registry_integrity"] == pinned_npm["integrity"]
    assert not any(channel.get("files") for channel in receipt["channels"] if channel["id"].startswith("nuget:Honua.Mobile"))
    assert receipt["blockers"], "the committed receipt still names its real blockers"
    for channel_id in preflight.COMPONENT_CHANNELS:
        if preflight.COMPONENT_CHANNELS[channel_id] in preflight.experimental_components(manifest):
            assert _channel(receipt, channel_id)["disposition"] == preflight.DEFERRED_EXPERIMENTAL
    preflight.audit(receipt, manifest)


def _experimental_manifest() -> dict:
    manifest = _manifest()
    manifest["experimental"] = {
        "honua-mobile": {"status": "experimental", "sourcePinnedOnly": True},
        "honua-collect": {"status": "experimental", "sourcePinnedOnly": True},
    }
    return manifest


def _experimental_receipt(world=None) -> dict:
    world = world or World()
    return preflight.build_receipt(
        world,
        _experimental_manifest(),
        observed_at="2026-09-29T00:00:00Z",
        retained={"grpc_sha256": _sha(world.grpc_body), "bsr_sha256": _sha(world.bsr_body)},
    )


MOBILE_CHANNEL_IDS = (
    "npm:@honua-io/embed",
    "nuget:Honua.Mobile.Maui",
    "nuget:Honua.Mobile.Offline",
    "nuget:Honua.Mobile.Sdk",
)


def test_experimental_mobile_channels_are_deferred_not_blockers():
    manifest = _experimental_manifest()
    receipt = _experimental_receipt()
    preflight.audit(receipt, manifest)
    assert set(receipt["deferred_experimental_components"]) == {"honua-collect", "honua-mobile"}
    for channel_id in MOBILE_CHANNEL_IDS:
        channel = _channel(receipt, channel_id)
        assert channel["disposition"] == "deferred-experimental"
        assert channel["component"] == "honua-mobile"
        assert "experimental:" in channel["deferral_reason"]
        assert channel["http_status"] == 404
        assert "files" not in channel and "evidence_class" not in channel
    blocked = {channel_id for blocker in receipt["blockers"] for channel_id in blocker["channels"]}
    assert not blocked.intersection(MOBILE_CHANNEL_IDS)
    publications = {blocker["publication"] for blocker in receipt["blockers"]}
    assert "npmjs @honua-io/embed" not in publications
    assert not any(item.startswith("nuget.org Honua.Mobile") for item in publications)
    # The rest of the gate is untouched: its real blockers remain.
    assert "oci://ghcr.io/honua-io/charts/honua" in publications
    assert "nuget.org Honua.Sdk 1.6.0" in publications


def test_manifest_reason_is_carried_into_the_deferred_row():
    manifest = _experimental_manifest()
    manifest["experimental"]["honua-mobile"]["reason"] = "Deferred out of 2026.1 by operator ruling."
    world = World()
    receipt = preflight.build_receipt(
        world,
        manifest,
        observed_at="2026-09-29T00:00:00Z",
        retained={"grpc_sha256": _sha(world.grpc_body), "bsr_sha256": _sha(world.bsr_body)},
    )
    assert _channel(receipt, "npm:@honua-io/embed")["deferral_reason"] == "Deferred out of 2026.1 by operator ruling."


def test_component_moved_to_ga_makes_mobile_channels_required_and_blocked_again():
    manifest = _experimental_manifest()
    manifest["components"]["honua-mobile"] = manifest["experimental"].pop("honua-mobile")
    manifest["components"]["honua-mobile"]["status"] = "GA"
    world = World()
    receipt = preflight.build_receipt(
        world,
        manifest,
        observed_at="2026-09-29T00:00:00Z",
        retained={"grpc_sha256": _sha(world.grpc_body), "bsr_sha256": _sha(world.bsr_body)},
    )
    preflight.audit(receipt, manifest)
    assert receipt["deferred_experimental_components"].keys() == {"honua-collect"}
    for channel_id in MOBILE_CHANNEL_IDS:
        assert _channel(receipt, channel_id)["disposition"] == "blocked-on-operator"
    publications = {blocker["publication"] for blocker in receipt["blockers"]}
    assert "npmjs @honua-io/embed" in publications
    assert "nuget.org Honua.Mobile.Sdk, Honua.Mobile.Offline, Honua.Mobile.Maui" in publications
    # A GA mobile channel is required: dropping it from the receipt fails the audit.
    receipt["channels"] = [item for item in receipt["channels"] if item["id"] != "nuget:Honua.Mobile.Sdk"]
    receipt["blockers"] = [
        blocker for blocker in receipt["blockers"] if "nuget:Honua.Mobile.Sdk" not in blocker["channels"]
    ]
    with pytest.raises(preflight.PreflightError, match="omits"):
        preflight.audit(receipt)


def test_a_stale_deferral_fails_against_the_manifest():
    receipt = _experimental_receipt()
    manifest = _experimental_manifest()
    manifest["components"]["honua-mobile"] = manifest["experimental"].pop("honua-mobile")
    with pytest.raises(preflight.PreflightError, match="does not match the manifest"):
        preflight.audit(receipt, manifest)


def test_experimental_channel_is_never_published_ga_evidence():
    receipt = _experimental_receipt()
    channel = _channel(receipt, "nuget:Honua.Mobile.Sdk")
    channel["disposition"] = "published"
    channel["evidence_class"] = "downloaded-bytes"
    channel["files"] = [{"filename": "x.nupkg", "sha256": "ab" * 32, "url": "https://api.nuget.org/x.nupkg"}]
    with pytest.raises(preflight.PreflightError, match="must be deferred-experimental"):
        preflight.audit(receipt)
    channel["disposition"] = "deferred-experimental"
    with pytest.raises(preflight.PreflightError, match="not GA evidence"):
        preflight.audit(receipt)


def test_deferred_row_needs_an_experimental_component():
    receipt = _experimental_receipt()
    receipt["deferred_experimental_components"] = {}
    with pytest.raises(preflight.PreflightError, match="not an experimental component"):
        preflight.audit(receipt)


def test_a_listed_experimental_package_stays_deferred_and_records_the_listing():
    world = World()
    original = world._response

    def listed(url: str):
        if url.endswith("/honua.mobile.sdk/index.json"):
            return 200, json.dumps({"versions": ["0.1.0"]}).encode()
        return original(url)

    world._response = listed
    receipt = _experimental_receipt(world)
    channel = _channel(receipt, "nuget:Honua.Mobile.Sdk")
    assert channel["disposition"] == "deferred-experimental"
    assert channel["registry_versions"] == ["0.1.0"]
    assert "files" not in channel
    preflight.audit(receipt, _experimental_manifest())


def _nupkg(package_id: str, version: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            f"{package_id}.nuspec",
            '<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd"><metadata>'
            f"<id>{package_id}</id><version>{version}</version></metadata></package>",
        )
    return output.getvalue()


def _public_pin_world(version: str = "1.10.0", *, bodies=None, catalog_hash=None, versions=None):
    """Serve every PINNED_NUPKG_IDS entry at `version` with registration and catalog leaves."""
    world = World(nuget_versions=versions)
    bodies = dict(bodies or {})
    for package_id in preflight.PINNED_NUPKG_IDS:
        bodies.setdefault(package_id, _nupkg(package_id, version))
    original = world._response

    def present(url):
        for package_id, body in bodies.items():
            catalog_url = f"https://api.nuget.org/v3/catalog0/data/{package_id.lower()}.{version}.json"
            if url == preflight.nuget_nupkg_url(package_id, version):
                return 200, body
            if url == preflight.nuget_registration_url(package_id, version):
                return 200, json.dumps({
                    "catalogEntry": catalog_url,
                    "listed": True,
                    "packageContent": preflight.nuget_nupkg_url(package_id, version),
                }).encode()
            if url == catalog_url:
                digest = hashlib.sha512(_nupkg(package_id, version) if catalog_hash == "honest" else body).digest()
                return 200, json.dumps({
                    "id": package_id,
                    "version": version,
                    "listed": True,
                    "packageHashAlgorithm": "SHA512",
                    "packageHash": base64.b64encode(digest).decode("ascii"),
                }).encode()
        return original(url)

    world._response = present
    return world


def _public_pin_manifest(version: str = "1.10.0") -> dict:
    manifest = _manifest()
    manifest["clientArtifacts"]["honua-sdk-dotnet"].update(
        version=version, registry="nuget.org", digest="sha256:" + _sha(_nupkg("Honua.Sdk", version)))
    return manifest


def _public_pin_receipt(world, manifest) -> dict:
    return preflight.build_receipt(world, manifest, observed_at="2026-10-02T00:00:00Z",
                                   retained={"grpc_sha256": _sha(GRPC), "bsr_sha256": _sha(BSR)})


def test_explicit_public_nuget_pin_requires_downloaded_matching_bytes():
    receipt = _public_pin_receipt(_public_pin_world(), _public_pin_manifest())
    train = _channel(receipt, "train:honua-sdk-dotnet")
    assert train["disposition"] == "published"
    assert len(train["files"]) == 2
    assert not any(b["kind"] == "blocked-on-train-binding" for b in receipt["blockers"])
    manifest = _public_pin_manifest()
    manifest["clientArtifacts"]["honua-sdk-dotnet"]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(preflight.PreflightError, match="sha256"):
        _public_pin_receipt(_public_pin_world(), manifest)


def test_public_nuget_pin_refuses_a_train_missing_the_pin_on_any_sdk_package():
    world = _public_pin_world()
    original = world._response

    def lagging(url):
        if url == preflight.nuget_index_url("Honua.Sdk.Geometry"):
            return 200, json.dumps({"versions": ["1.6.4"]}).encode()
        return original(url)

    world._response = lagging
    with pytest.raises(preflight.PreflightError, match="Honua.Sdk.Geometry"):
        _public_pin_receipt(world, _public_pin_manifest())


def test_public_nuget_pin_refuses_studio_bytes_the_catalog_does_not_bind():
    corrupt = {"Honua.Sdk.Studio": b"nupkg-bytes"}
    with pytest.raises(preflight.PreflightError, match="catalog SHA-512"):
        _public_pin_receipt(_public_pin_world(bodies=corrupt, catalog_hash="honest"), _public_pin_manifest())


def test_public_nuget_pin_refuses_studio_bytes_that_are_not_the_named_package():
    wrong = {"Honua.Sdk.Studio": _nupkg("Honua.Sdk.Studio", "1.6.4")}
    with pytest.raises(preflight.PreflightError, match="nuspec does not identify"):
        _public_pin_receipt(_public_pin_world(bodies=wrong), _public_pin_manifest())
    with pytest.raises(preflight.PreflightError, match="not a valid .nupkg"):
        _public_pin_receipt(_public_pin_world(bodies={"Honua.Sdk.Studio": b"nupkg-bytes"}), _public_pin_manifest())


RATE_LIMIT_BODY = b'{"message":"API rate limit exceeded for 0.0.0.0."}'
ABUSE_BODY = b'{"message":"We have detected automated requests from this IP and have temporarily blocked it."}'
PERMISSION_BODY = b'{"message":"Resource not accessible by integration"}'


class _QgisTransport:
    def __init__(self, api_results, page_status=404):
        self.inner = World()
        self.api_results = list(api_results)
        self.page_status = page_status
        self.seen = []

    def get(self, url: str) -> preflight.Response:
        self.seen.append(url)
        if url == preflight.QGIS_REPO_URL:
            status, body = self.api_results.pop(0)
            return preflight.Response(status, body, url)
        if url == preflight.QGIS_PUBLIC_REPO_URL:
            return preflight.Response(self.page_status, b"html", url)
        return self.inner.get(url)


def _qgis_receipt(transport: _QgisTransport) -> dict:
    return preflight.build_receipt(
        transport,
        _manifest(),
        observed_at="2026-09-26T00:00:00Z",
        retained={"grpc_sha256": _sha(transport.inner.grpc_body), "bsr_sha256": _sha(transport.inner.bsr_body)},
    )


def test_qgis_rate_limit_uses_the_public_404_and_does_not_record_the_denial(monkeypatch):
    slept = []
    monkeypatch.setattr(preflight, "_sleep", slept.append)
    transport = _QgisTransport([(403, RATE_LIMIT_BODY)])
    channel = _channel(_qgis_receipt(transport), "qgis:honua-qgis-plugin")
    assert channel == _channel(_receipt(), "qgis:honua-qgis-plugin")
    assert channel["http_status"] == 404
    assert slept == []
    assert transport.api_results == []


def test_qgis_permission_denial_is_not_evidence_the_repo_is_private(monkeypatch):
    slept = []
    monkeypatch.setattr(preflight, "_sleep", slept.append)
    transport = _QgisTransport([(403, PERMISSION_BODY)], page_status=404)
    with pytest.raises(preflight.PreflightError, match="not evidence the repo is private"):
        _qgis_receipt(transport)
    assert slept == []
    assert preflight.QGIS_PUBLIC_REPO_URL not in transport.seen


def test_qgis_rate_limit_retries_until_the_api_answers(monkeypatch):
    slept = []
    monkeypatch.setattr(preflight, "_sleep", slept.append)
    transport = _QgisTransport([
        (403, RATE_LIMIT_BODY),
        (404, b'{"message":"Not Found"}'),
    ], page_status=503)
    channel = _channel(_qgis_receipt(transport), "qgis:honua-qgis-plugin")
    assert channel["http_status"] == 404
    assert channel["disposition"] == "blocked-on-operator"
    assert slept == [10]


def test_qgis_abuse_block_without_a_public_404_still_fails(monkeypatch):
    slept = []
    monkeypatch.setattr(preflight, "_sleep", slept.append)
    transport = _QgisTransport([(403, ABUSE_BODY)] * 6, page_status=403)
    with pytest.raises(preflight.PreflightError, match="not evidence the repo is private"):
        _qgis_receipt(transport)
    assert slept == [10, 30, 60, 120, 80]

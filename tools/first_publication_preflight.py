#!/usr/bin/env python3
"""Anonymous first-publication preflight for honua-release#57.

The probe sends no registry credential. A row is `published` only when this process
downloaded the bytes and hashed them. An index listing is not a byte receipt. A
missing public coordinate stays a named blocker; this tool does not invent a digest,
a chart, or a plugin ZIP for it.

Container images are probed the way an anonymous `docker pull` is: an anonymous GHCR pull
token, then the manifest bytes, whose sha256 is the recorded digest. A multi-arch index must
also resolve its linux/amd64 manifest. A pinned digest that the registry no longer serves, or an
image whose anonymous token or manifest is denied, fails the preflight.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import re
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RECEIPT_PATH = REPO_ROOT / "certification" / "first-publication" / "preflight-2026-10-02.json"
SCHEMA = "honua.first-publication-preflight/v1"
ISSUE = "honua-io/honua-release#57"

# Retained anonymous byte receipts. A live download that does not match fails
# the preflight; the hashes are not rewritten from the response.
GRPC_NUPKG_SHA256 = "69ab1ae0212a81bba6018bbd01789698f8e2f0c1d5134fec1eab3cefe841979f"
GRPC_NUPKG_URL = "https://api.nuget.org/v3-flatcontainer/geospatial.grpc/1.0.0/geospatial.grpc.1.0.0.nupkg"
BSR_ZIP_SHA256 = "7f68c40e1308dc47aff5cf87eb30ba220b513830e0c7677287687744adc970ef"
BSR_ZIP_URL = "https://buf.build/honua-io/geospatial-grpc/archive/f52df33b3b5d4723881ad0bacaf8a754.zip"
# Upstream GHSA-jrc7-96c5-q579 lists <=6.4.0 as affected and 6.4.1 as patched.
MAPLIBRE_PATCHED_VERSION = (6, 4, 1)


def _maplibre_pin_is_patched(pin: str) -> bool:
    """Require an exact stable version with a known patched lower bound."""
    match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", pin)
    return match is not None and tuple(map(int, match.groups())) >= MAPLIBRE_PATCHED_VERSION

SDK_PACKAGE_IDS = (
    "Honua.Sdk",
    "Honua.Sdk.Abstractions",
    "Honua.Sdk.Admin",
    "Honua.Sdk.Catalogs",
    "Honua.Sdk.Cli",
    "Honua.Sdk.ConsoleShare",
    "Honua.Sdk.Field",
    "Honua.Sdk.GeoServices",
    "Honua.Sdk.Geometry",
    "Honua.Sdk.Grpc",
    "Honua.Sdk.Offline",
    "Honua.Sdk.OgcFeatures",
    "Honua.Sdk.Processes",
    "Honua.Sdk.Scenes",
    "Honua.Sdk.Spec",
    "Honua.Sdk.Studio",
)
MOBILE_PACKAGE_IDS = ("Honua.Mobile.Maui", "Honua.Mobile.Offline", "Honua.Mobile.Sdk")
PINNED_NUPKG_IDS = ("Honua.Sdk", "Honua.Sdk.Studio")
MOBILE_EMBED_PACKAGE = "@honua-io/embed"
# Channel -> platform-manifest component that ships it. A channel whose component is listed under
# the manifest's top-level `experimental:` block (status experimental) is reported as
# `deferred-experimental`: visible in the receipt, never GA evidence, and never a release blocker.
# When the component leaves `experimental:` the channel is required again with no code change.
COMPONENT_CHANNELS = {
    **{f"nuget:{package_id}": "honua-mobile" for package_id in MOBILE_PACKAGE_IDS},
    f"npm:{MOBILE_EMBED_PACKAGE}": "honua-mobile",
}
DEFERRED_EXPERIMENTAL = "deferred-experimental"
# clientArtifacts rows published to npmjs. honua-sdk-js is required; a companion is probed when pinned.
NPM_CLIENT_ROWS = ("honua-sdk-js", "honua-mcp-server")
PYPI_MIGRATE = "honua-migrate"

ANONYMOUS_HEADERS = {"User-Agent": "honua-release-preflight", "Accept": "*/*"}
ALLOWED_HOSTS = {
    "api.github.com",
    "api.nuget.org",
    "buf.build",
    "codeload.github.com",
    "files.pythonhosted.org",
    "ghcr.io",
    "github.com",
    "globalcdn.nuget.org",
    "plugins.qgis.org",
    "pypi.org",
    "registry.npmjs.org",
    "registry.terraform.io",
}

MOBILE_NUGET_BOUNDARY = (
    "honua-mobile .github/workflows/publish-dotnet-mobile.yml publishes Honua.Mobile.Sdk, "
    "Honua.Mobile.Offline, and Honua.Mobile.Maui to nuget.org only from signed mobile-dotnet-v* tags, "
    "through nuget.org Trusted Publishing (NuGet/login, policy owner makanikai) in environment public-nuget. "
    "Publication needs that nuget.org Trusted Publishing policy for honua-io/honua-mobile "
    "publish-dotnet-mobile.yml and a public-nuget deployment rule that admits mobile-dotnet-v* tags. "
    "honua-release has no credential that can publish these packages."
)
MOBILE_NPM_BOUNDARY = (
    "honua-mobile .github/workflows/publish-npm-embed.yml publishes @honua-io/embed to npmjs.com only from "
    "signed mobile-embed-v* tags in environment public-npm, using npm Trusted Publishing with provenance. "
    "npm attaches a trusted publisher only to an existing package, so the first publish needs a short-lived "
    "NPM_BOOTSTRAP_TOKEN environment secret (deleted afterwards), owner control of the @honua-io npm scope, "
    "and a public-npm deployment rule that admits mobile-embed-v* tags. honua-release has no credential that "
    "can publish @honua-io/embed."
)
HELM_BOUNDARY = (
    "Anonymous GHCR token for repository:honua-io/charts/honua:pull was denied, so "
    "oci://ghcr.io/honua-io/charts/honua has no public tag list. honua-helm .github/workflows/release.yml "
    "refuses appVersion 0.0.0 and requires a published ghcr.io/honua-io/honua-server:v<semver>-aot image "
    "before it will push a chart. This repo does not invent that server SemVer or an appVersion."
)
# Anonymous container pulls. Every ghcr.io/honua-io/... string in platform-manifest.yaml is probed,
# plus the published channel tags below. A channel tag floats (each RC moves it), so its digest is
# recorded as observed_* and left out of drift; its anonymous pullability is still compared.
GHCR_IMAGE_PREFIX = "ghcr.io/honua-io/"
OCI_CHANNEL_TAGS = ("ghcr.io/honua-io/honua-server:2026.1-rc",)
OCI_EVIDENCE = "anonymous-oci-manifest"
OCI_INDEX_TYPES = frozenset({
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
})
OCI_IMAGE_TYPES = frozenset({
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
})
OCI_ACCEPT = ", ".join(sorted(OCI_INDEX_TYPES | OCI_IMAGE_TYPES))
# Sibling manifest fields that pin an image field: (index digest, linux/amd64 manifest digest).
# awsLambdaDigest pins the linux/amd64 child of the Lambda index, not the index itself.
OCI_PIN_FIELDS = {
    "image": ("digest", ("platformDigests", "amd64")),
    "awsLambdaImage": (None, ("awsLambdaDigest",)),
}
OCI_FLOATING_FIELDS = ("observed_digest", "observed_platform_digests", "observed_urls")
QGIS_REPO_URL = "https://api.github.com/repos/honua-io/honua-qgis-plugin"
QGIS_PUBLIC_REPO_URL = "https://github.com/honua-io/honua-qgis-plugin"
# plugins.qgis.org keys a plugin by its ZIP's top-level package folder, not its display name.
# honua-qgis-plugin scripts/package.py builds honua_gis_assistant-<version>.zip with the single
# top-level folder honua_gis_assistant/, so that is the listing slug.
QGIS_PLUGIN_URL = "https://plugins.qgis.org/plugins/honua_gis_assistant/"
# Anonymous api.github.com denials on shared CI addresses are rate-limit or abuse blocks.
# Retry them, then give up. A 403 is never itself evidence the repository is private.
QGIS_DENIAL_RETRY_DELAYS = (10, 30, 60, 120, 80)
QGIS_BOUNDARY = (
    "https://api.github.com/repos/honua-io/honua-qgis-plugin returned 404 to an unauthenticated request "
    "(a private repository is not distinguishable from a missing one) and "
    "https://plugins.qgis.org/plugins/honua_gis_assistant/ returned 404. honua-qgis-plugin CI builds a ZIP artifact only; "
    "it does not submit to the QGIS plugin repository. Publication still needs an operator visibility, "
    "history, and signing review, an exact release ZIP, and OSGeo submission (honua-qgis-plugin#29). "
    "No signing secret is recorded here."
)


class PreflightError(ValueError):
    """The preflight cannot honestly classify a coordinate."""


class Response:
    def __init__(self, status: int, body: bytes, url: str, headers: dict | None = None):
        self.status = status
        self.body = body
        self.url = url
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}


# A registry 5xx/429 or a dropped connection is an outage, not an answer about the package.
# Retry it with bounded backoff (npm answered 502 on pinned version documents for over an hour on
# 2026-10-22 while the tarballs served). A persistent outage still fails; a 4xx is never retried.
REQUEST_DELAYS = (0, 5, 15, 30)
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504, 522})
_sleep = time.sleep


class UrllibTransport:
    def get(self, url: str, headers: dict | None = None) -> Response:
        last: Response | None = None
        for delay in REQUEST_DELAYS:
            if delay:
                _sleep(delay)
            try:
                response = self._get_once(url, headers)
            except _TransientProbeFailure as exc:
                if delay == REQUEST_DELAYS[-1]:
                    raise PreflightError(f"registry probe failed for {url}: {exc.__cause__}") from exc.__cause__
                continue
            if response.status not in TRANSIENT_STATUSES:
                return response
            last = response
        assert last is not None
        return last

    def _get_once(self, url: str, headers: dict | None = None) -> Response:
        _require_host(url)
        bearer = _ghcr_bearer_headers(url, headers)
        request = urllib.request.Request(url, headers={**ANONYMOUS_HEADERS, **(headers or {})})
        if request.has_header("Authorization") and not bearer:
            raise PreflightError(f"refusing to send Authorization to {url}")
        # The anonymous GHCR pull token goes only to ghcr.io/v2/ and never follows a redirect.
        opener = urllib.request.build_opener(_NoRedirect).open if bearer else urllib.request.urlopen
        try:
            with opener(request, timeout=60) as response:
                final = response.geturl()
                _require_host(final)
                return Response(response.status, response.read(), final, _header_map(response))
        except urllib.error.HTTPError as exc:
            final = exc.geturl() or url
            _require_host(final)
            return Response(exc.code, exc.read(), final, _header_map(exc))
        except (urllib.error.URLError, TimeoutError) as exc:
            raise _TransientProbeFailure() from exc


class _TransientProbeFailure(Exception):
    """A connection-level failure; UrllibTransport.get retries it and then reports the cause."""


def _header_map(response) -> dict:
    headers = getattr(response, "headers", None)
    return dict(headers.items()) if headers is not None and hasattr(headers, "items") else {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ARG002
        return None


def _ghcr_bearer_headers(url: str, headers: dict | None) -> bool:
    """True only for an anonymous GHCR bearer sent to ghcr.io/v2/. Anything else is refused."""
    auth = [value for key, value in (headers or {}).items() if key.lower() == "authorization"]
    if not auth:
        return False
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "ghcr.io" or not parsed.path.startswith("/v2/"):
        raise PreflightError(f"refusing to send Authorization to {url}")
    if len(auth) != 1 or not str(auth[0]).startswith("Bearer "):
        raise PreflightError(f"refusing a non-bearer Authorization for {url}")
    return True


def _require_host(url: str) -> None:
    host = urllib.parse.urlparse(url).hostname
    if host not in ALLOWED_HOSTS:
        raise PreflightError(f"refusing registry URL outside the anonymous allowlist: {url}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_json(body: bytes, url: str) -> dict:
    raw = gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, gzip.BadGzipFile, OSError) as exc:
        raise PreflightError(f"registry returned invalid JSON for {url}") from exc
    if not isinstance(value, dict):
        raise PreflightError(f"registry returned a non-object for {url}")
    return value


def _sha512_sri(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _inconclusive_github_denial(response: Response) -> bool:
    """True when GitHub refused the request instead of answering whether the repo exists."""
    if response.status not in {403, 429}:
        return False
    text = response.body.decode("utf-8", errors="replace").lower()
    return any(
        marker in text
        for marker in (
            "rate limit",
            "secondary rate",
            "too many requests",
            "temporarily blocked",
            "automated requests",
            "abuse detection",
        )
    )


class QgisRepoProbe:
    """Canonical API coordinate plus the response that was actually observed.

    A public-page 404 during an API denial is not rewritten onto the API URL.
    `api` stays the GitHub API response. `observed` is that page, or None when
    the API itself answered.
    """

    def __init__(self, api: Response, observed: Response | None = None):
        self.api = api
        self.observed = observed

    @property
    def fallback(self) -> bool:
        return self.observed is not None


def _probe_qgis_repo(transport) -> QgisRepoProbe:
    """Classify the QGIS repo without treating an API denial as privacy evidence.

    A definitive API status is returned as-is. A rate-limit or abuse denial is
    retried within a 300 second budget. A public-page 404 during that denial is
    kept as its own observation (URL and status); it does not replace the API
    status. Any other denial is returned so the caller can refuse it.
    """
    response = transport.get(QGIS_REPO_URL)
    waited = 0
    delays = iter(QGIS_DENIAL_RETRY_DELAYS)
    while _inconclusive_github_denial(response):
        page = transport.get(QGIS_PUBLIC_REPO_URL)
        if page.status == 404:
            return QgisRepoProbe(response, page)
        try:
            delay = next(delays)
        except StopIteration:
            break
        if waited >= 300:
            break
        delay = min(delay, 300 - waited)
        _sleep(delay)
        waited += delay
        response = transport.get(QGIS_REPO_URL)
    return QgisRepoProbe(response)


def _status_only(status: int, url: str, *, allowed: set[int]) -> int:
    if status not in allowed:
        raise PreflightError(
            f"{url} returned HTTP {status}; not recording that as absent or published"
        )
    return status


def nuget_index_url(package_id: str) -> str:
    return f"https://api.nuget.org/v3-flatcontainer/{package_id.lower()}/index.json"


def nuget_nupkg_url(package_id: str, version: str) -> str:
    package = package_id.lower()
    return f"https://api.nuget.org/v3-flatcontainer/{package}/{version}/{package}.{version}.nupkg"


def nuget_registration_url(package_id: str, version: str) -> str:
    return f"https://api.nuget.org/v3/registration5-gz-semver2/{package_id.lower()}/{version.lower()}.json"


def nuget_flat_symbol_url(package_id: str, version: str) -> str:
    package = package_id.lower()
    return f"https://api.nuget.org/v3-flatcontainer/{package}/{version}/{package}.{version}.snupkg"


def nuget_cdn_symbol_url(package_id: str, version: str) -> str:
    return f"https://globalcdn.nuget.org/symbol-packages/{package_id.lower()}.{version}.snupkg"


def pypi_json_url(package: str) -> str:
    return f"https://pypi.org/pypi/{urllib.parse.quote(package, safe='')}/json"


def npm_package_url(package: str) -> str:
    return "https://registry.npmjs.org/" + urllib.parse.quote(package, safe="@")


def npm_version_url(package: str, version: str) -> str:
    return npm_package_url(package) + "/" + urllib.parse.quote(version, safe="")


def _version_key(version: str) -> tuple:
    main = version.split("-", 1)[0].split("+", 1)[0]
    try:
        return tuple(int(part) for part in main.split("."))
    except ValueError:
        return (0,)


def _newest(versions: list[str]) -> str:
    if not versions:
        raise PreflightError("cannot select a newest NuGet version from an empty index")
    return max(versions, key=_version_key)


def _file_record(filename: str, url: str, body: bytes, **extra: str) -> dict:
    record = {"filename": filename, "sha256": _sha256(body), "url": url}
    record.update(extra)
    return record


def _manifest_view(manifest: dict) -> dict:
    artifacts = manifest.get("clientArtifacts") or {}
    components = manifest.get("components") or {}
    try:
        dotnet = artifacts["honua-sdk-dotnet"]
        javascript = artifacts["honua-sdk-js"]
        python_sdk = artifacts["honua-sdk-python-wheel"]
        python_admin = artifacts["honua-admin-python-wheel"]
        iac = components["honua-iac"]
        dotnet_component = components["honua-sdk-dotnet"]
    except KeyError as exc:
        raise PreflightError(f"platform manifest is missing {exc}") from exc
    archive = str(iac.get("artifact", ""))
    if not archive.startswith("archive:https://"):
        raise PreflightError("honua-iac artifact is not an archive:https URL")
    digest = str(iac.get("artifactSha256", ""))
    if not digest.startswith("sha256:") or len(digest) != 7 + 64:
        raise PreflightError("honua-iac artifactSha256 is not sha256:<64 hex>")
    return {
        "dotnet_package": str(dotnet["package"]),
        "dotnet_version": str(dotnet["version"]),
        "dotnet_registry": str(dotnet["registry"]),
        "dotnet_sha256": str(dotnet.get("digest", "")).removeprefix("sha256:"),
        "dotnet_component_version": str(dotnet_component["version"]),
        "js_version": str(javascript["version"]),
        "npm": _npm_rows(artifacts),
        "python": (
            ("honua-sdk-python-wheel", python_sdk),
            ("honua-admin-python-wheel", python_admin),
        ),
        "iac_url": archive.removeprefix("archive:"),
        "iac_sha256": digest.removeprefix("sha256:"),
    }


def _npm_rows(artifacts: dict) -> tuple[tuple[str, dict], ...]:
    """The primary SDK and its pinned companion npm packages. Every one is downloaded."""
    return tuple(
        (row, artifacts[row])
        for row in NPM_CLIENT_ROWS
        if row == "honua-sdk-js" or isinstance(artifacts.get(row), dict)
    )


def experimental_components(manifest: dict) -> dict[str, str]:
    """Components the manifest lists under `experimental:` with status experimental -> reason."""
    block = manifest.get("experimental")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise PreflightError("platform manifest experimental: block is not a mapping")
    ga = manifest.get("components") or {}
    deferred = {}
    for name, entry in block.items():
        if not isinstance(entry, dict) or entry.get("status") != "experimental":
            continue
        if name in ga:
            raise PreflightError(f"{name} is listed under both components: and experimental:")
        reason = str(entry.get("reason") or "").strip()
        deferred[str(name)] = reason or (
            f"platform-manifest.yaml lists {name} under experimental: with status experimental "
            "(ADR-0059 experimental+disabled). It is excluded from the certified set, so its public "
            "publication is deferred and is not a first-publication blocker."
        )
    return deferred


def _deferred_channel(channel_id: str, publication: str, component: str, reason: str, *,
                      http_status: int, urls: list[str], versions: list[str] | None = None) -> dict:
    fields = {
        "component": component,
        "deferral_reason": reason,
        "http_status": http_status,
        "urls": urls,
    }
    if versions:
        # Recorded so a registry change is visible as drift. An index listing is not bytes, and an
        # experimental component's package is never GA evidence.
        fields["registry_versions"] = versions
    return _channel(channel_id, publication, DEFERRED_EXPERIMENTAL, **fields)


def _nuget_versions(transport, package_id: str) -> tuple[int, list[str]]:
    url = nuget_index_url(package_id)
    response = transport.get(url)
    status = _status_only(response.status, url, allowed={200, 404})
    if status == 404:
        return status, []
    payload = _parse_json(response.body, response.url)
    versions = payload.get("versions")
    if not isinstance(versions, list) or not versions or not all(isinstance(item, str) for item in versions):
        raise PreflightError(f"{url} did not list package versions")
    return status, list(versions)


def _download_matches(transport, url: str, expected_sha256: str | None, filename: str) -> dict:
    response = transport.get(url)
    if response.status != 200:
        raise PreflightError(f"required public bytes at {url} returned HTTP {response.status}")
    record = _file_record(filename, response.url, response.body)
    if expected_sha256 is not None and record["sha256"] != expected_sha256:
        raise PreflightError(
            f"downloaded {filename} sha256 {record['sha256']} does not match retained {expected_sha256}"
        )
    return record


def _nuget_published_file(transport, package_id: str, version: str, expected_sha256: str | None) -> dict:
    """Download a public nupkg bound to its NuGet catalog SHA-512 and its nuspec identity."""
    url = nuget_nupkg_url(package_id, version)
    registration_url = nuget_registration_url(package_id, version)
    response = transport.get(registration_url)
    _status_only(response.status, registration_url, allowed={200})
    metadata = _parse_json(response.body, response.url)
    catalog_url = str(metadata.get("catalogEntry", ""))
    if not catalog_url.startswith("https://api.nuget.org/v3/catalog0/"):
        raise PreflightError(f"{package_id} {version}: NuGet returned an untrusted catalog URL")
    if metadata.get("packageContent") != url:
        raise PreflightError(f"{package_id} {version}: NuGet packageContent is not {url}")
    response = transport.get(catalog_url)
    _status_only(response.status, catalog_url, allowed={200})
    catalog = _parse_json(response.body, response.url)
    if catalog.get("id") != package_id or catalog.get("version") != version:
        raise PreflightError(f"{package_id} {version}: NuGet catalog identity does not match")
    if metadata.get("listed") is not True or catalog.get("listed") is not True:
        raise PreflightError(f"{package_id} {version}: NuGet package is not listed")
    filename = f"{package_id.lower()}.{version}.nupkg"
    response = transport.get(url)
    if response.status != 200:
        raise PreflightError(f"required public bytes at {url} returned HTTP {response.status}")
    record = _file_record(filename, response.url, response.body)
    if expected_sha256 is not None and record["sha256"] != expected_sha256:
        raise PreflightError(
            f"downloaded {filename} sha256 {record['sha256']} does not match retained {expected_sha256}"
        )
    package_hash = base64.b64encode(hashlib.sha512(response.body).digest()).decode("ascii")
    if catalog.get("packageHashAlgorithm") != "SHA512" or catalog.get("packageHash") != package_hash:
        raise PreflightError(f"downloaded {filename} does not match the NuGet catalog SHA-512")
    try:
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            nuspecs = [name for name in archive.namelist() if name.lower().endswith(".nuspec")]
            if len(nuspecs) != 1:
                raise PreflightError(f"{filename} must contain exactly one .nuspec")
            root = ElementTree.fromstring(archive.read(nuspecs[0]))
    except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise PreflightError(f"{filename} is not a valid .nupkg") from exc
    fields = {element.tag.rsplit("}", 1)[-1]: (element.text or "") for element in root.iter()}
    if fields.get("id") != package_id or fields.get("version") != version:
        raise PreflightError(f"{filename} nuspec does not identify {package_id} {version}")
    return record


def _pypi_files(transport, package: str, version: str | None) -> tuple[str, list[dict]]:
    url = pypi_json_url(package)
    response = transport.get(url)
    status = _status_only(response.status, url, allowed={200, 404})
    if status == 404:
        return "", []
    payload = _parse_json(response.body, response.url)
    info = payload.get("info") or {}
    selected = version or str(info.get("version") or "")
    if not selected:
        raise PreflightError(f"{url} did not name a version")
    releases = payload.get("releases") or {}
    rows = releases.get(selected)
    if not isinstance(rows, list) or not rows:
        raise PreflightError(f"PyPI has no files for {package}=={selected}")
    files = []
    for row in rows:
        filename = str(row.get("filename") or "")
        digest = str((row.get("digests") or {}).get("sha256") or "")
        file_url = str(row.get("url") or "")
        if not filename or len(digest) != 64 or not file_url:
            raise PreflightError(f"PyPI file metadata for {package}=={selected} is incomplete")
        _require_host(file_url)
        downloaded = transport.get(file_url)
        if downloaded.status != 200:
            raise PreflightError(f"PyPI file {file_url} returned HTTP {downloaded.status}")
        record = _file_record(filename, downloaded.url, downloaded.body, registry_sha256=digest)
        if record["sha256"] != digest:
            raise PreflightError(f"downloaded {filename} does not match the PyPI sha256")
        files.append(record)
    files.sort(key=lambda item: item["filename"])
    return selected, files


def _npm_published(transport, row: str, artifact: dict) -> dict:
    """Download a pinned npm tarball and bind it to the clientArtifacts integrity and sourceSha."""
    package = str(artifact.get("package") or "")
    version = str(artifact.get("version") or "")
    integrity = str(artifact.get("integrity") or "")
    source_sha = str(artifact.get("sourceSha") or "")
    if not package or not version or not integrity.startswith("sha512-") or len(source_sha) != 40:
        raise PreflightError(f"clientArtifacts.{row} needs package, version, sha512 integrity and sourceSha")
    url = npm_version_url(package, version)
    response = transport.get(url)
    if response.status != 200:
        raise PreflightError(f"pinned {package}@{version} returned HTTP {response.status}")
    meta = _parse_json(response.body, response.url)
    dist = meta.get("dist") or {}
    if dist.get("integrity") != integrity:
        raise PreflightError(f"npm integrity for the pinned {package} version does not match clientArtifacts")
    # gitHead is the commit npm publish ran from; the published bytes are that source, not the tag.
    if meta.get("gitHead") != source_sha:
        raise PreflightError(
            f"npm gitHead for {package}@{version} is {meta.get('gitHead')}, not clientArtifacts.{row}.sourceSha"
        )
    tarball_url = str(dist.get("tarball") or "")
    _require_host(tarball_url)
    tarball = transport.get(tarball_url)
    if tarball.status != 200:
        raise PreflightError(f"{package}@{version} tarball returned HTTP {tarball.status}")
    if _sha512_sri(tarball.body) != integrity:
        raise PreflightError(f"{package}@{version} tarball does not match the npm integrity")
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball.body), mode="r:gz") as archive:
            member = archive.extractfile("package/package.json")
            document = json.load(member) if member is not None else {}
    except (tarfile.TarError, KeyError, json.JSONDecodeError) as exc:
        raise PreflightError(f"{package}@{version} tarball has no readable package/package.json") from exc
    if document.get("name") != package or str(document.get("version")) != version:
        raise PreflightError(f"{package}@{version} tarball package.json names another package or version")
    filename = str(artifact.get("filename") or tarball_url.rstrip("/").split("/")[-1])
    return _channel(
        f"npm:{package}",
        f"npm {package}@{version}",
        "published",
        evidence_class="downloaded-bytes",
        files=[_file_record(filename, tarball.url, tarball.body, registry_integrity=integrity)],
        http_status=200,
        source_sha=source_sha,
        urls=[url, tarball_url],
        version=version,
    )


def _template_pins(body: bytes) -> list[dict]:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
    except tarfile.TarError as exc:
        raise PreflightError("create-honua-app tarball is not a gzip tar") from exc
    pins = []
    with archive:
        names = sorted(
            name for name in archive.getnames()
            if name.startswith("package/templates/") and name.endswith("/package.json")
        )
        if not names:
            raise PreflightError("create-honua-app tarball has no template package.json")
        for name in names:
            member = archive.extractfile(name)
            if member is None:
                raise PreflightError(f"create-honua-app tarball missing {name}")
            try:
                document = json.load(member)
            except json.JSONDecodeError as exc:
                raise PreflightError(f"{name} is not JSON") from exc
            dependencies = document.get("dependencies") or {}
            if not isinstance(dependencies, dict):
                raise PreflightError(f"{name} dependencies are not an object")
            pins.append({
                "path": name,
                "dependencies": {
                    "@honua/sdk-js": dependencies.get("@honua/sdk-js"),
                    "maplibre-gl": dependencies.get("maplibre-gl"),
                },
            })
    return pins


def _parse_oci_ref(ref: str) -> tuple[str, str, str | None]:
    """ghcr.io/<repo>[:tag][@sha256:...] -> (repo, reference, pinned digest or None)."""
    if not ref.startswith(GHCR_IMAGE_PREFIX):
        raise PreflightError(f"{ref} is not a {GHCR_IMAGE_PREFIX} image reference")
    name, _, digest = ref.removeprefix("ghcr.io/").partition("@")
    repo, _, tag = name.rpartition(":") if ":" in name else (name, "", "")
    if digest and not _is_oci_digest(digest):
        raise PreflightError(f"{ref} has a digest that is not sha256:<64 hex>")
    reference = digest or tag
    if not repo or not reference or not re.fullmatch(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", repo):
        raise PreflightError(f"{ref} is not a tag- or digest-qualified GHCR reference")
    return repo, reference, digest or None


def _is_oci_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _dig(entry: dict, path: tuple[str, ...]):
    for key in path:
        if not isinstance(entry, dict):
            return None
        entry = entry.get(key)
    return entry


def oci_image_refs(manifest: dict) -> list[dict]:
    """Every ghcr.io/honua-io/... string in the manifest, with its sibling digest pins."""
    found: dict[str, dict] = {}

    def walk(value, path: tuple[str, ...], parent: dict | None) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, path + (str(key),), value)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, path + (str(index),), None)
        elif isinstance(value, str) and value.startswith(GHCR_IMAGE_PREFIX):
            index_field, platform_field = OCI_PIN_FIELDS.get(path[-1], (None, None))
            pins = {}
            if index_field and parent is not None:
                pins["index_digest"] = parent.get(index_field)
            if platform_field and parent is not None:
                pins["linux_amd64_digest"] = _dig(parent, platform_field)
            pins = {key: pin for key, pin in pins.items() if pin is not None}
            for key, pin in pins.items():
                if not _is_oci_digest(pin):
                    raise PreflightError(f"{'.'.join(path)} {key} pin is not sha256:<64 hex>")
            row = {"ref": value, "fields": [".".join(path)], "pins": pins, "floating": False}
            previous = found.get(value)
            if previous is not None:
                if previous["pins"] != pins:
                    raise PreflightError(f"{value} is pinned to different digests in the manifest")
                previous["fields"].append(row["fields"][0])
            else:
                found[value] = row

    walk(manifest, (), None)
    for ref in OCI_CHANNEL_TAGS:
        found.setdefault(ref, {"ref": ref, "fields": [], "pins": {}, "floating": True})
    return [found[ref] for ref in sorted(found)]


def ghcr_token_url(repo: str) -> str:
    return f"https://ghcr.io/token?service=ghcr.io&scope=repository:{repo}:pull"


def ghcr_manifest_url(repo: str, reference: str) -> str:
    return f"https://ghcr.io/v2/{repo}/manifests/{reference}"


def _oci_manifest(transport, repo: str, reference: str, token: str, ref: str) -> tuple[str, str, dict]:
    """Fetch one manifest and return (sha256 digest of its bytes, media type, document)."""
    url = ghcr_manifest_url(repo, reference)
    response = transport.get(url, {"Accept": OCI_ACCEPT, "Authorization": f"Bearer {token}"})
    if response.status != 200:
        raise PreflightError(
            f"anonymous pull of {ref} manifest {reference} returned HTTP {response.status}; "
            "customers cannot pull it"
        )
    document = _parse_json(response.body, url)
    digest = "sha256:" + _sha256(response.body)
    served = response.headers.get("docker-content-digest")
    if served and served != digest:
        raise PreflightError(f"{ref} manifest bytes hash to {digest}, but GHCR named {served}")
    if reference.startswith("sha256:") and digest != reference:
        raise PreflightError(f"{ref} manifest fetched by {reference} hashes to {digest}")
    media_type = str(response.headers.get("content-type") or document.get("mediaType") or "").split(";")[0].strip()
    if media_type not in OCI_INDEX_TYPES | OCI_IMAGE_TYPES:
        raise PreflightError(f"{ref} manifest {reference} has media type {media_type or '(none)'}")
    return digest, media_type, document


def _oci_published(transport, image: dict) -> dict:
    """Pull an image's manifest anonymously, bind it to its pins, and resolve linux/amd64."""
    ref = image["ref"]
    repo, reference, inline = _parse_oci_ref(ref)
    token_url = ghcr_token_url(repo)
    response = transport.get(token_url)
    if response.status != 200:
        raise PreflightError(
            f"GHCR denied an anonymous pull token for {repo} (HTTP {response.status}); {ref} is not public"
        )
    token = str(_parse_json(response.body, token_url).get("token") or "")
    if not token:
        raise PreflightError(f"GHCR returned no anonymous pull token for {repo}")
    digest, media_type, document = _oci_manifest(transport, repo, reference, token, ref)
    pins = dict(image["pins"])
    if inline:
        if pins.get("index_digest", inline) != inline:
            raise PreflightError(f"{ref} carries {inline} but its manifest digest field pins {pins['index_digest']}")
        pins["index_digest"] = inline
    urls = [token_url, ghcr_manifest_url(repo, reference)]
    child_urls = []
    platforms = {}
    if media_type in OCI_INDEX_TYPES:
        children = [
            item for item in document.get("manifests") or []
            if isinstance(item, dict)
            and (item.get("platform") or {}).get("os") == "linux"
            and (item.get("platform") or {}).get("architecture") == "amd64"
        ]
        if len(children) != 1 or not _is_oci_digest(children[0].get("digest")):
            raise PreflightError(f"{ref} index does not name exactly one linux/amd64 manifest")
        child = str(children[0]["digest"])
        child_digest, child_type, _ = _oci_manifest(transport, repo, child, token, ref)
        if child_type not in OCI_IMAGE_TYPES:
            raise PreflightError(f"{ref} linux/amd64 entry {child} is not an image manifest")
        platforms["linux/amd64"] = child_digest
        child_urls.append(ghcr_manifest_url(repo, child))
    elif "linux_amd64_digest" in pins and "index_digest" not in pins:
        # A single-platform manifest is its own linux/amd64 manifest.
        platforms["linux/amd64"] = digest
    for key, pin in sorted(pins.items()):
        observed = digest if key == "index_digest" else platforms.get("linux/amd64")
        if observed != pin:
            raise PreflightError(
                f"{ref} {key} is {observed}, not the manifest pin {pin}"
            )
    fields = {
        "evidence_class": OCI_EVIDENCE,
        "http_status": 200,
        "image": ref,
        "manifest_fields": image["fields"],
        "media_type": media_type,
        "pins": pins,
    }
    if image["floating"]:
        # Every digest-derived field of a floating tag is observed_*, so an RC push is not drift.
        fields.update(
            floating_channel_tag=True,
            observed_digest=digest,
            observed_platform_digests=platforms,
            observed_urls=child_urls,
            platforms=sorted(platforms),
            urls=urls,
        )
    else:
        fields.update(digest=digest, platform_digests=platforms, urls=urls + child_urls)
    return _channel(f"oci:{ref}", ref, "published", **fields)


def _channel(channel_id: str, publication: str, disposition: str, **fields) -> dict:
    channel = {"disposition": disposition, "id": channel_id, "publication": publication}
    channel.update(fields)
    return channel


def build_receipt(
    transport,
    manifest: dict,
    *,
    observed_at: str,
    retained: dict | None = None,
) -> dict:
    retained = retained or {}
    grpc_sha256 = str(retained.get("grpc_sha256") or GRPC_NUPKG_SHA256)
    bsr_sha256 = str(retained.get("bsr_sha256") or BSR_ZIP_SHA256)
    view = _manifest_view(manifest)
    deferred = experimental_components(manifest)
    channels: list[dict] = []

    sdk_versions: dict[str, list[str]] = {}
    for package_id in SDK_PACKAGE_IDS:
        status, versions = _nuget_versions(transport, package_id)
        sdk_versions[package_id] = versions
        if status == 404:
            channels.append(_channel(
                f"nuget:{package_id}",
                f"nuget.org {package_id}",
                "blocked-on-operator",
                http_status=404,
                urls=[nuget_index_url(package_id)],
            ))
            continue
        channels.append(_channel(
            f"nuget:{package_id}",
            f"nuget.org {package_id}",
            "listed",
            evidence_class="registry-index",
            http_status=200,
            urls=[nuget_index_url(package_id)],
            versions=versions,
        ))

    pin = view["dotnet_version"]
    pin_statuses = {}
    for package_id in PINNED_NUPKG_IDS:
        url = nuget_nupkg_url(package_id, pin)
        response = transport.get(url)
        pin_statuses[package_id] = _status_only(response.status, url, allowed={200, 404})
        if pin_statuses[package_id] == 200 and pin not in sdk_versions.get(package_id, []):
            raise PreflightError(f"{package_id} {pin} nupkg exists but the index omitted it")
        if pin_statuses[package_id] == 404 and pin in sdk_versions.get(package_id, []):
            raise PreflightError(f"{package_id} index lists {pin} but the nupkg URL returned 404")
    listed = [package_id for package_id, versions in sdk_versions.items() if versions]
    pin_listed = [package_id for package_id in listed if pin in sdk_versions[package_id]]
    version_sets = {tuple(sdk_versions[package_id]) for package_id in listed}
    representative = sdk_versions.get("Honua.Sdk", [])
    if pin_statuses["Honua.Sdk"] == 404 and not pin_listed:
        channels.append(_channel(
            "train:honua-sdk-dotnet",
            f"nuget.org {view['dotnet_package']} {pin}",
            "blocked-on-train-binding",
            client_artifact_registry=view["dotnet_registry"],
            client_artifact_version=pin,
            component_version=view["dotnet_component_version"],
            http_status=404,
            nuget_versions=representative,
            pin_package_statuses=pin_statuses,
            urls=[nuget_nupkg_url(package_id, pin) for package_id in PINNED_NUPKG_IDS],
            version_lists_agree=len(version_sets) <= 1,
        ))
    elif pin_listed:
        if view["dotnet_registry"] != "nuget.org" or len(view["dotnet_sha256"]) != 64:
            raise PreflightError(
                "nuget.org now serves the manifest Honua.Sdk pin; record downloaded bytes and rebind "
                "clientArtifacts before calling the train published"
            )
        if any(status != 200 for status in pin_statuses.values()):
            raise PreflightError("nuget.org does not serve every required package at the manifest pin")
        # Honua.Sdk pins exact same-version dependencies on the whole SDK set, so one package
        # missing the pin makes the train unrestorable even when Honua.Sdk itself is served.
        missing = [package_id for package_id in SDK_PACKAGE_IDS if pin not in sdk_versions[package_id]]
        if missing:
            raise PreflightError(
                f"nuget.org does not list {pin} for every SDK package: {', '.join(missing)}"
            )
        files = [
            _nuget_published_file(
                transport, package_id, pin,
                view["dotnet_sha256"] if package_id == view["dotnet_package"] else None,
            )
            for package_id in PINNED_NUPKG_IDS
        ]
        channels.append(_channel(
            "train:honua-sdk-dotnet",
            f"nuget.org {view['dotnet_package']} {pin}",
            "published",
            evidence_class="downloaded-bytes",
            client_artifact_registry=view["dotnet_registry"],
            client_artifact_version=pin,
            component_version=view["dotnet_component_version"],
            files=files,
            http_status=200,
            pin_package_statuses=pin_statuses,
            urls=[nuget_nupkg_url(package_id, pin) for package_id in PINNED_NUPKG_IDS],
        ))

    if representative:
        newest = _newest(representative)
        nupkg = _download_matches(
            transport,
            nuget_nupkg_url("Honua.Sdk", newest),
            None,
            f"honua.sdk.{newest}.nupkg",
        )
        flat_symbol = transport.get(nuget_flat_symbol_url("Honua.Sdk", newest))
        flat_status = _status_only(
            flat_symbol.status, nuget_flat_symbol_url("Honua.Sdk", newest), allowed={200, 404}
        )
        files = [nupkg]
        symbol_url = nuget_cdn_symbol_url("Honua.Sdk", newest)
        symbol = transport.get(symbol_url)
        symbol_status = _status_only(symbol.status, symbol_url, allowed={200, 404})
        if symbol_status == 200:
            files.append(_file_record(f"honua.sdk.{newest}.snupkg", symbol.url, symbol.body))
        files.sort(key=lambda item: item["filename"])
        channels.append(_channel(
            "nuget:Honua.Sdk-newest",
            f"nuget.org Honua.Sdk {newest}",
            "published",
            evidence_class="downloaded-bytes",
            files=files,
            flat_container_snupkg_status=flat_status,
            http_status=200,
            urls=[nuget_nupkg_url("Honua.Sdk", newest), symbol_url],
            version=newest,
        ))

    for package_id in MOBILE_PACKAGE_IDS:
        status, versions = _nuget_versions(transport, package_id)
        component = COMPONENT_CHANNELS[f"nuget:{package_id}"]
        if component in deferred:
            channels.append(_deferred_channel(
                f"nuget:{package_id}",
                f"nuget.org {package_id}",
                component,
                deferred[component],
                http_status=status,
                urls=[nuget_index_url(package_id)],
                versions=versions,
            ))
            continue
        if status == 200:
            raise PreflightError(
                f"nuget.org listed {package_id} {versions}; download the nupkg before recording it"
            )
        channels.append(_channel(
            f"nuget:{package_id}",
            f"nuget.org {package_id}",
            "blocked-on-operator",
            http_status=404,
            urls=[nuget_index_url(package_id)],
        ))

    grpc_index, grpc_versions = _nuget_versions(transport, "Geospatial.Grpc")
    if grpc_index != 200 or "1.0.0" not in grpc_versions:
        raise PreflightError("Geospatial.Grpc 1.0.0 is no longer listed on nuget.org")
    grpc_file = _download_matches(
        transport, GRPC_NUPKG_URL, grpc_sha256, "geospatial.grpc.1.0.0.nupkg"
    )
    channels.append(_channel(
        "nuget:Geospatial.Grpc",
        "nuget.org Geospatial.Grpc 1.0.0",
        "published",
        evidence_class="downloaded-bytes",
        files=[grpc_file],
        http_status=200,
        urls=[nuget_index_url("Geospatial.Grpc"), GRPC_NUPKG_URL],
        versions=grpc_versions,
    ))
    bsr_file = _download_matches(
        transport,
        BSR_ZIP_URL,
        bsr_sha256,
        "geospatial-grpc-bsr-f52df33b3b5d4723881ad0bacaf8a754.zip",
    )
    channels.append(_channel(
        "bsr:buf.build/honua-io/geospatial-grpc",
        "buf.build/honua-io/geospatial-grpc:f52df33b3b5d4723881ad0bacaf8a754",
        "published",
        evidence_class="downloaded-bytes",
        files=[bsr_file],
        http_status=200,
        urls=[BSR_ZIP_URL],
    ))

    for artifact_name, artifact in view["python"]:
        package = str(artifact["package"])
        version = str(artifact["version"])
        filename = str(artifact["filename"])
        digest = str(artifact["digest"]).removeprefix("sha256:")
        selected, files = _pypi_files(transport, package, version)
        match = [item for item in files if item["filename"] == filename]
        if selected != version or len(match) != 1 or match[0]["sha256"] != digest:
            raise PreflightError(f"{artifact_name} bytes are not the pinned PyPI file {filename}")
        channels.append(_channel(
            f"pypi:{package}",
            f"PyPI {package} {version}",
            "published",
            evidence_class="downloaded-bytes",
            files=files,
            http_status=200,
            urls=[pypi_json_url(package)],
            version=version,
        ))

    migrate_version, migrate_files = _pypi_files(transport, PYPI_MIGRATE, None)
    if not migrate_files:
        channels.append(_channel(
            "pypi:honua-migrate",
            "PyPI honua-migrate",
            "blocked-on-operator",
            http_status=404,
            urls=[pypi_json_url(PYPI_MIGRATE)],
        ))
    else:
        channels.append(_channel(
            "pypi:honua-migrate",
            f"PyPI honua-migrate {migrate_version}",
            "published",
            evidence_class="downloaded-bytes",
            files=migrate_files,
            http_status=200,
            urls=[pypi_json_url(PYPI_MIGRATE)],
            version=migrate_version,
        ))


    for row, artifact in view["npm"]:
        channels.append(_npm_published(transport, row, artifact))

    embed_url = npm_package_url(MOBILE_EMBED_PACKAGE)
    embed = transport.get(embed_url)
    embed_status = _status_only(embed.status, embed_url, allowed={200, 404})
    embed_component = COMPONENT_CHANNELS[f"npm:{MOBILE_EMBED_PACKAGE}"]
    if embed_component in deferred:
        embed_versions = []
        if embed_status == 200:
            embed_versions = sorted((_parse_json(embed.body, embed.url).get("versions") or {}).keys())
        channels.append(_deferred_channel(
            f"npm:{MOBILE_EMBED_PACKAGE}",
            f"npmjs {MOBILE_EMBED_PACKAGE}",
            embed_component,
            deferred[embed_component],
            http_status=embed_status,
            urls=[embed_url],
            versions=embed_versions,
        ))
    elif embed_status == 200:
        raise PreflightError("npmjs listed @honua-io/embed; download the tarball before recording it")
    else:
        channels.append(_channel(
            "npm:@honua-io/embed",
            "npmjs @honua-io/embed",
            "blocked-on-operator",
            http_status=404,
            urls=[embed_url],
        ))

    create_url = npm_package_url("create-honua-app")
    create = transport.get(create_url)
    if create.status != 200:
        raise PreflightError(f"create-honua-app returned HTTP {create.status}")
    create_meta = _parse_json(create.body, create.url)
    latest = str((create_meta.get("dist-tags") or {}).get("latest") or "")
    version_meta = (create_meta.get("versions") or {}).get(latest) or {}
    dist = version_meta.get("dist") or {}
    tarball_url = str(dist.get("tarball") or "")
    shasum = str(dist.get("shasum") or "")
    if not latest or not tarball_url or len(shasum) != 40:
        raise PreflightError("create-honua-app metadata has no latest tarball")
    _require_host(tarball_url)
    tarball = transport.get(tarball_url)
    if tarball.status != 200:
        raise PreflightError(f"create-honua-app tarball returned HTTP {tarball.status}")
    if hashlib.sha1(tarball.body).hexdigest() != shasum:
        raise PreflightError("create-honua-app tarball does not match the npm shasum")
    templates = _template_pins(tarball.body)
    sdk_pins = sorted({
        item["dependencies"].get("@honua/sdk-js")
        for item in templates
        if item["dependencies"].get("@honua/sdk-js")
    })
    sdk_pin_statuses = {}
    for sdk_pin in sdk_pins:
        sdk_pin_url = npm_version_url("@honua/sdk-js", sdk_pin)
        sdk_pin_response = transport.get(sdk_pin_url)
        sdk_pin_statuses[sdk_pin] = _status_only(sdk_pin_response.status, sdk_pin_url, allowed={200, 404})
    maplibre_pins = sorted({
        str(item["dependencies"].get("maplibre-gl"))
        for item in templates
        if item["dependencies"].get("maplibre-gl")
    })
    # A template SDK pin that resolves on npm is still off-train unless it is the
    # manifest's @honua/sdk-js version; HTTP 200 alone does not clear the blocker.
    template_blocked = (
        any(not _maplibre_pin_is_patched(pin) for pin in maplibre_pins)
        or any(status != 200 for status in sdk_pin_statuses.values())
        or any(sdk_pin != view["js_version"] for sdk_pin in sdk_pins)
        or not sdk_pins
    )
    create_channel = _channel(
        "npm:create-honua-app",
        f"npm create-honua-app@{latest}",
        "blocked-on-republish" if template_blocked else "published",
        evidence_class="downloaded-bytes",
        files=[_file_record(f"create-honua-app-{latest}.tgz", tarball.url, tarball.body, registry_shasum=shasum)],
        http_status=200,
        manifest_sdk_pin=view["js_version"],
        maplibre_pins=maplibre_pins,
        sdk_pin_statuses=sdk_pin_statuses,
        templates=templates,
        urls=[create_url, tarball_url],
        version=latest,
    )
    channels.append(create_channel)

    helm_url = "https://ghcr.io/token?service=ghcr.io&scope=repository:honua-io/charts/honua:pull"
    helm = transport.get(helm_url)
    if helm.status == 200:
        raise PreflightError("GHCR issued a pull token for honua-io/charts/honua; record tags before calling it absent")
    if helm.status not in {401, 403}:
        raise PreflightError(f"GHCR token probe returned HTTP {helm.status}")
    channels.append(_channel(
        "helm:oci://ghcr.io/honua-io/charts/honua",
        "oci://ghcr.io/honua-io/charts/honua",
        "blocked-on-candidate",
        http_status=helm.status,
        urls=[helm_url],
    ))

    for image in oci_image_refs(manifest):
        channels.append(_oci_published(transport, image))

    qgis_repo_url = QGIS_REPO_URL
    qgis_plugin_url = QGIS_PLUGIN_URL
    qgis_repo = _probe_qgis_repo(transport)
    qgis_plugin = transport.get(qgis_plugin_url)
    if not qgis_repo.fallback and qgis_repo.api.status in {403, 429}:
        raise PreflightError("GitHub API denied the QGIS repo probe; that is not evidence the repo is private")
    # Visibility uses the public page only when the API denial fell back to it.
    # The channel still stores the API status beside that page, not in its place.
    repo_visibility = qgis_repo.observed.status if qgis_repo.fallback else qgis_repo.api.status
    if repo_visibility not in {200, 404} or qgis_plugin.status not in {200, 404}:
        raise PreflightError(
            f"QGIS probe returned repo HTTP {qgis_repo.api.status} and plugin HTTP {qgis_plugin.status}"
        )
    if qgis_plugin.status == 200 or (repo_visibility == 200 and qgis_plugin.status != 404):
        raise PreflightError("QGIS visibility changed; inspect the listing before recording publication")
    qgis_fields = {
        "http_status": qgis_repo.api.status,
        "plugin_http_status": qgis_plugin.status,
        "urls": [qgis_repo_url, qgis_plugin_url],
    }
    if qgis_repo.fallback:
        qgis_fields.update(
            canonical_url=qgis_repo_url,
            api_http_status=qgis_repo.api.status,
            observed_url=qgis_repo.observed.url,
            observed_http_status=qgis_repo.observed.status,
        )
    channels.append(_channel(
        "qgis:honua-qgis-plugin",
        "QGIS plugin honua_gis_assistant",
        "blocked-on-operator",
        **qgis_fields,
    ))

    terraform_url = "https://registry.terraform.io/v1/modules/honua-io"
    terraform = transport.get(terraform_url)
    terraform_status = _status_only(terraform.status, terraform_url, allowed={200, 404})
    if terraform_status == 200:
        raise PreflightError("registry.terraform.io listed honua-io modules; the git-archive disposition no longer holds")
    archive_name = urllib.parse.urlparse(view["iac_url"]).path.rstrip("/").split("/")[-1]
    archive = _download_matches(transport, view["iac_url"], view["iac_sha256"], archive_name)
    channels.append(_channel(
        "iac:git-archive",
        f"honua-iac Git archive {archive_name.removesuffix('.tar.gz')}",
        "supported-by-git-url",
        evidence_class="downloaded-bytes",
        files=[archive],
        registry_http_status=404,
        urls=[terraform_url, view["iac_url"]],
    ))

    channels.sort(key=lambda item: item["id"])
    receipt = {
        "blockers": _blockers(channels),
        "channels": channels,
        "deferred_experimental_components": dict(sorted(deferred.items())),
        "issue": ISSUE,
        "method": (
            "Anonymous HTTPS with no registry credential; the only Authorization header sent is a GHCR "
            "pull token issued to an unauthenticated request. published rows are sha256 of bytes this "
            "probe downloaded. listed rows are registry indexes only. Blocked rows name the publication "
            "and the operator boundary and do not carry package bytes. deferred-experimental rows are "
            "channels of a component the platform manifest lists under experimental:; they are not "
            "GA evidence and not release blockers. anonymous-oci-manifest rows are container images "
            "pulled with an anonymous GHCR token: the digest is the sha256 of the manifest bytes, a "
            "multi-arch index also resolves its linux/amd64 manifest, and manifest pins must match. "
            "A floating channel tag records observed_* digests that are not compared for drift."
        ),
        "observed_at": observed_at,
        "schema": SCHEMA,
    }
    audit(receipt)
    return receipt


def _blockers(channels: list[dict]) -> list[dict]:
    by_id = {channel["id"]: channel for channel in channels}
    blockers = []

    def add(kind: str, publication: str, boundary: str, channel_ids: list[str]) -> None:
        missing = [channel_id for channel_id in channel_ids if channel_id not in by_id]
        if missing:
            raise PreflightError(f"blocker references unknown channels {missing}")
        blockers.append({
            "boundary": boundary,
            "channels": channel_ids,
            "kind": kind,
            "publication": publication,
        })

    train = by_id.get("train:honua-sdk-dotnet")
    if train and train["disposition"] == "blocked-on-train-binding":
        versions = ", ".join(train.get("nuget_versions") or []) or "(none)"
        component = train["component_version"]
        pin = train["client_artifact_version"]
        registry = train["client_artifact_registry"]
        add(
            "blocked-on-train-binding",
            f"nuget.org Honua.Sdk {pin}",
            (
                f"clientArtifacts.honua-sdk-dotnet pins Honua.Sdk {pin} on {registry}. "
                f"The nuget.org flat-container nupkg for that version returned "
                f"{train['pin_package_statuses']}. Public Honua.Sdk index versions are {versions}. "
                f"components.honua-sdk-dotnet.version is {component}, which is not that public set. "
                "Those later versions are not recorded as the train's bytes. honua-console#356 cannot "
                "anonymously restore this pin; a public Honua.Sdk.Studio at another version does not "
                "satisfy it. Rebinding the manifest is an operator decision. No nuget.org credential "
                "is required to see this gap."
            ),
            ["train:honua-sdk-dotnet"],
        )
    mobile_ids = [f"nuget:{package_id}" for package_id in MOBILE_PACKAGE_IDS]
    if all(by_id[channel_id]["disposition"] == "blocked-on-operator" for channel_id in mobile_ids):
        add(
            "blocked-on-operator",
            "nuget.org Honua.Mobile.Sdk, Honua.Mobile.Offline, Honua.Mobile.Maui",
            MOBILE_NUGET_BOUNDARY,
            mobile_ids,
        )
    if by_id["npm:@honua-io/embed"]["disposition"] == "blocked-on-operator":
        add(
            "blocked-on-operator",
            "npmjs @honua-io/embed",
            MOBILE_NPM_BOUNDARY,
            ["npm:@honua-io/embed"],
        )
    if by_id["helm:oci://ghcr.io/honua-io/charts/honua"]["disposition"] == "blocked-on-candidate":
        add(
            "blocked-on-candidate",
            "oci://ghcr.io/honua-io/charts/honua",
            HELM_BOUNDARY,
            ["helm:oci://ghcr.io/honua-io/charts/honua"],
        )
    qgis = by_id["qgis:honua-qgis-plugin"]
    if qgis["disposition"] == "blocked-on-operator":
        if qgis.get("observed_url"):
            qgis_boundary = (
                f"{qgis['canonical_url']} returned {qgis['api_http_status']} to an unauthenticated request "
                f"(not evidence the repository is private). "
                f"{qgis['observed_url']} returned {qgis['observed_http_status']}. "
                f"{QGIS_PLUGIN_URL} returned {qgis['plugin_http_status']}. "
                "honua-qgis-plugin CI builds a ZIP artifact only; "
                "it does not submit to the QGIS plugin repository. Publication still needs an operator visibility, "
                "history, and signing review, an exact release ZIP, and OSGeo submission (honua-qgis-plugin#29). "
                "No signing secret is recorded here."
            )
        else:
            qgis_boundary = QGIS_BOUNDARY
        add("blocked-on-operator", "QGIS plugin honua_gis_assistant", qgis_boundary, ["qgis:honua-qgis-plugin"])
    missing_sdk = [
        f"nuget:{package_id}"
        for package_id in SDK_PACKAGE_IDS
        if by_id[f"nuget:{package_id}"]["disposition"] != "listed"
    ]
    if missing_sdk:
        add(
            "blocked-on-operator",
            "nuget.org Honua.Sdk package set",
            "One or more Honua.Sdk package IDs returned 404 from the nuget.org flat-container index. "
            "The public set is incomplete; this probe does not fill the gap with a local pack.",
            missing_sdk,
        )
    migrate = by_id.get("pypi:honua-migrate")
    if migrate and migrate["disposition"] == "blocked-on-operator":
        add(
            "blocked-on-operator",
            "PyPI honua-migrate",
            "PyPI returned 404 for honua-migrate. Publication is the honua-migrate Trusted Publisher "
            "workflow. This repo does not hold that publisher credential.",
            ["pypi:honua-migrate"],
        )
    create = by_id["npm:create-honua-app"]
    if create["disposition"] == "blocked-on-republish":
        templates = ", ".join(
            f"{item['path']} @honua/sdk-js@{item['dependencies'].get('@honua/sdk-js')} "
            f"maplibre-gl@{item['dependencies'].get('maplibre-gl')}"
            for item in create["templates"]
        )
        add(
            "blocked-on-republish",
            f"npm create-honua-app@{create['version']} template pins",
            (
                f"The published tarball was downloaded, but its templates pin {templates}. "
                f"npm version status for those @honua/sdk-js pins is {create['sdk_pin_statuses']}; "
                f"the platform manifest pins @honua/sdk-js {create['manifest_sdk_pin']}. "
                "MapLibre pins must be exact stable versions at least 6.4.1, the patched version "
                "for GHSA-jrc7-96c5-q579; unresolved ranges and prereleases need review. "
                "A replacement create-honua-app release has to "
                "come from honua-sdk-js. This repo cannot publish that tarball."
            ),
            ["npm:create-honua-app"],
        )
    blockers.sort(key=lambda item: item["publication"])
    return blockers


def audit(receipt: dict, manifest: dict | None = None) -> None:
    if receipt.get("schema") != SCHEMA or receipt.get("issue") != ISSUE:
        raise PreflightError("preflight receipt schema or issue is wrong")
    if not str(receipt.get("observed_at") or ""):
        raise PreflightError("preflight receipt has no observed_at")
    channels = receipt.get("channels")
    blockers = receipt.get("blockers")
    if not isinstance(channels, list) or not channels:
        raise PreflightError("preflight receipt has no channels")
    if not isinstance(blockers, list):
        raise PreflightError("preflight receipt blockers must be a list")
    ids = [channel.get("id") for channel in channels]
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise PreflightError("preflight channels must be uniquely sorted by id")
    deferred = receipt.get("deferred_experimental_components")
    if not isinstance(deferred, dict):
        raise PreflightError("preflight receipt has no deferred_experimental_components mapping")
    if manifest is not None and deferred != experimental_components(manifest):
        raise PreflightError(
            "preflight receipt deferred_experimental_components does not match the manifest experimental: block"
        )
    required = {
        "bsr:buf.build/honua-io/geospatial-grpc",
        "helm:oci://ghcr.io/honua-io/charts/honua",
        "iac:git-archive",
        "npm:@honua/sdk-js",
        "npm:create-honua-app",
        "nuget:Geospatial.Grpc",
        "nuget:Honua.Sdk",
        "pypi:honua-admin",
        "pypi:honua-migrate",
        "pypi:honua-sdk",
        "qgis:honua-qgis-plugin",
        "train:honua-sdk-dotnet",
    }
    by_id = {channel.get("id"): channel for channel in channels}
    for channel_id, component in COMPONENT_CHANNELS.items():
        channel = by_id.get(channel_id)
        if component in deferred:
            # Deferred channels stay visible; dropping them would hide the deferral.
            if channel is None:
                raise PreflightError(f"preflight receipt omits deferred-experimental channel {channel_id}")
            if channel.get("disposition") != DEFERRED_EXPERIMENTAL:
                raise PreflightError(
                    f"{channel_id} belongs to experimental component {component} and must be "
                    f"{DEFERRED_EXPERIMENTAL}, not {channel.get('disposition')}"
                )
        else:
            required.add(channel_id)
    if manifest is not None:
        for row, artifact in _npm_rows(manifest.get("clientArtifacts") or {}):
            channel_id = f"npm:{artifact.get('package')}"
            required.add(channel_id)
            channel = by_id.get(channel_id) or {}
            files = channel.get("files") or [{}]
            if (
                channel.get("version") != str(artifact.get("version"))
                or channel.get("source_sha") != artifact.get("sourceSha")
                or files[0].get("registry_integrity") != artifact.get("integrity")
            ):
                raise PreflightError(
                    f"{channel_id} receipt does not record the clientArtifacts.{row} version, sourceSha and integrity"
                )
    for ref in OCI_CHANNEL_TAGS:
        required.add(f"oci:{ref}")
    if manifest is not None:
        for image in oci_image_refs(manifest):
            channel_id = f"oci:{image['ref']}"
            required.add(channel_id)
            channel = by_id.get(channel_id) or {}
            expected = dict(image["pins"])
            _, _, inline = _parse_oci_ref(image["ref"])
            if inline:
                expected["index_digest"] = inline
            if channel.get("pins") != expected or channel.get("manifest_fields") != image["fields"]:
                raise PreflightError(f"{channel_id} receipt does not record the manifest digest pins")
    missing = required.difference(ids)
    if missing:
        raise PreflightError(f"preflight receipt omits {sorted(missing)}")
    if any(channel["id"] == "nuget:Honua.Sdk" and channel["disposition"] == "listed" for channel in channels):
        if "nuget:Honua.Sdk-newest" not in ids:
            raise PreflightError("Honua.Sdk is listed but the newest nupkg was not downloaded")
    blocked_ids = set()
    for channel in channels:
        disposition = channel.get("disposition")
        files = channel.get("files") or []
        if disposition == "published" and channel.get("evidence_class") == OCI_EVIDENCE:
            _audit_oci(channel)
        elif disposition == "published":
            if channel.get("evidence_class") != "downloaded-bytes" or not files:
                raise PreflightError(f"{channel['id']} is published without downloaded bytes")
        elif disposition == "listed":
            if files:
                raise PreflightError(f"{channel['id']} listing must not carry package bytes")
            if not channel.get("versions") and not channel.get("version"):
                raise PreflightError(f"{channel['id']} listing has no version")
        elif disposition == "supported-by-git-url":
            if channel.get("registry_http_status") != 404 or not files:
                raise PreflightError(f"{channel['id']} git-url support needs a 404 registry and archive bytes")
        elif disposition == "blocked-on-republish":
            # The tarball itself is public. The blocker is the template inside those bytes.
            if channel.get("evidence_class") != "downloaded-bytes" or not files:
                raise PreflightError(f"{channel['id']} republish blocker must carry the downloaded tarball")
        elif disposition == DEFERRED_EXPERIMENTAL:
            component = channel.get("component")
            if COMPONENT_CHANNELS.get(channel["id"]) != component or component not in deferred:
                raise PreflightError(
                    f"{channel['id']} is {DEFERRED_EXPERIMENTAL} but its component is not an experimental "
                    "component of the manifest"
                )
            if not str(channel.get("deferral_reason") or ""):
                raise PreflightError(f"{channel['id']} is {DEFERRED_EXPERIMENTAL} without a reason")
            if files or channel.get("evidence_class"):
                raise PreflightError(f"{channel['id']} is {DEFERRED_EXPERIMENTAL} and is not GA evidence")
        elif str(disposition).startswith("blocked-") or disposition == "absent-retired":
            if files:
                raise PreflightError(f"{channel['id']} must not carry package bytes")
        else:
            raise PreflightError(f"{channel['id']} has unknown disposition {disposition}")
        for record in files:
            digest = str(record.get("sha256") or "")
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise PreflightError(f"{channel['id']} has a digest that is not 64 hex")
            if not str(record.get("url") or "").startswith("https://"):
                raise PreflightError(f"{channel['id']} file has no https URL")
        if str(disposition).startswith("blocked-"):
            blocked_ids.add(channel["id"])
    covered = set()
    for blocker in blockers:
        if blocker.get("kind") not in {
            "blocked-on-train-binding",
            "blocked-on-operator",
            "blocked-on-candidate",
            "blocked-on-republish",
        }:
            raise PreflightError(f"unknown blocker kind {blocker.get('kind')}")
        if not str(blocker.get("publication") or "") or not str(blocker.get("boundary") or ""):
            raise PreflightError("a blocker is missing its publication or boundary")
        channel_ids = blocker.get("channels")
        if not isinstance(channel_ids, list) or not channel_ids:
            raise PreflightError(f"blocker {blocker.get('publication')} names no channels")
        covered.update(channel_ids)
    if covered != blocked_ids:
        raise PreflightError(
            f"blocker channels {sorted(covered)} do not match blocked rows {sorted(blocked_ids)}"
        )


def _audit_oci(channel: dict) -> None:
    channel_id = channel["id"]
    if not channel_id.startswith(f"oci:{GHCR_IMAGE_PREFIX}") or channel.get("image") != channel_id.removeprefix("oci:"):
        raise PreflightError(f"{channel_id} is an OCI row without its GHCR image reference")
    if channel.get("files") or channel.get("http_status") != 200:
        raise PreflightError(f"{channel_id} OCI row must be an HTTP 200 manifest without package files")
    floating = channel.get("floating_channel_tag") is True
    digest = channel.get("observed_digest" if floating else "digest")
    platforms = channel.get("observed_platform_digests" if floating else "platform_digests")
    if not _is_oci_digest(digest) or not isinstance(platforms, dict):
        raise PreflightError(f"{channel_id} OCI row has no sha256 manifest digest")
    if not all(_is_oci_digest(value) for value in platforms.values()):
        raise PreflightError(f"{channel_id} OCI row has a platform digest that is not sha256:<64 hex>")
    if channel.get("media_type") in OCI_INDEX_TYPES and "linux/amd64" not in platforms:
        raise PreflightError(f"{channel_id} is a multi-arch index without a resolved linux/amd64 manifest")
    if floating and (channel.get("pins") or channel.get("platforms") != sorted(platforms)):
        raise PreflightError(f"{channel_id} floating channel tag must not carry pins")
    for key, pin in (channel.get("pins") or {}).items():
        observed = digest if key == "index_digest" else platforms.get("linux/amd64")
        if observed != pin:
            raise PreflightError(f"{channel_id} {key} {observed} does not match its pin {pin}")


def project(receipt: dict) -> dict:
    """The drift-compared view: no observed_at, and no observed digests of floating channel tags."""
    projected = {key: value for key, value in receipt.items() if key != "observed_at"}
    projected["channels"] = [
        {key: value for key, value in channel.items() if key not in OCI_FLOATING_FIELDS}
        if channel.get("floating_channel_tag") is True else channel
        for channel in receipt.get("channels") or []
    ]
    return projected


def compare(live: dict, committed: dict) -> None:
    audit(live)
    audit(committed)
    if project(live) != project(committed):
        live_ids = {channel["id"]: channel for channel in project(live)["channels"]}
        committed_ids = {channel["id"]: channel for channel in project(committed)["channels"]}
        for channel_id in sorted(set(live_ids) | set(committed_ids)):
            if live_ids.get(channel_id) != committed_ids.get(channel_id):
                raise PreflightError(f"first-publication receipt drifted at {channel_id}")
        if live["blockers"] != committed["blockers"]:
            raise PreflightError("first-publication blocker text drifted")
        raise PreflightError("first-publication receipt drifted")


def dumps(receipt: dict) -> str:
    return json.dumps(receipt, indent=2, sort_keys=True) + "\n"


def load_receipt(path: Path) -> dict:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"cannot read preflight receipt {path}: {exc}") from exc
    if not isinstance(receipt, dict):
        raise PreflightError(f"{path} is not a receipt object")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=REPO_ROOT / "platform-manifest.yaml")
    parser.add_argument("--receipt", type=Path, default=RECEIPT_PATH)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--observed-at")
    args = parser.parse_args(argv)
    if args.check == args.write:
        parser.error("choose exactly one of --check or --write")
    manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8")) or {}
    observed_at = args.observed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        live = build_receipt(UrllibTransport(), manifest, observed_at=observed_at)
        audit(live, manifest)
        if args.write:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(dumps(live), encoding="utf-8")
            print(f"wrote {args.receipt}")
        else:
            committed = load_receipt(args.receipt)
            audit(committed, manifest)
            compare(live, committed)
            print(f"OK    first-publication preflight matches {args.receipt}")
        for blocker in live["blockers"]:
            print(f"BLOCKED {blocker['kind']}: {blocker['publication']}")
    except PreflightError as exc:
        print(f"ERROR {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

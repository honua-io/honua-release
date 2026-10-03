"""A local package-registry front that serves ONLY the manifest-pinned Honua packages.

Doc containers point npm, pip and NuGet at this front. Third-party packages pass through to the
public registries unchanged. A Honua package (npm `@honua/*`, `@honua-io/*`, `create-honua*`,
`honua*`; PyPI `honua*`; NuGet `Honua.*`) is visible only at the version platform-manifest.yaml
pins in clientArtifacts, so a doc that installs an unpinned, renamed or newer package fails the
install the way it would be wrong for the release, and the guard records the refusal. A pin's
declared hash (npm `integrity`, PyPI/NuGet `digest`) binds the bytes too: the packument integrity and
the simple-index `#sha256=` fragment must equal it (npm and pip verify the download against those),
and the guard hashes the pinned npm tarball and NuGet package it serves.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

NPM_UPSTREAM = "https://registry.npmjs.org"
PYPI_UPSTREAM = "https://pypi.org/simple"
NUGET_FLAT_UPSTREAM = "https://api.nuget.org/v3-flatcontainer"
NUGET_REG_UPSTREAM = "https://api.nuget.org/v3/registration5-semver1"

NPM_HONUA = re.compile(r"^(@honua(-io)?/|create-honua|honua)", re.I)
PYPI_HONUA = re.compile(r"^honua", re.I)
NUGET_HONUA = re.compile(r"^honua\.", re.I)


def pypi_normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pins_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """{"npm": {name: version}, "pypi": {name: filename}, "nuget": {id-lower: version},
    "digests": the same keys mapped to the declared hash, for the pins that declare one}"""
    pins: dict[str, Any] = {"npm": {}, "pypi": {}, "nuget": {}}
    digests: dict[str, dict[str, str]] = {"npm": {}, "pypi": {}, "nuget": {}}
    for pin in (manifest.get("clientArtifacts") or {}).values():
        package, version = str(pin.get("package", "")), str(pin.get("version", ""))
        if not package or not version:
            continue
        registry = str(pin.get("registry") or "").lower()
        if package.startswith("@") or registry == "npm" or pin.get("integrity", "").startswith("sha512-"):
            ecosystem, key, declared = "npm", package, pin.get("integrity")
            pins["npm"][package] = version
        elif registry == "nuget.org" or package.startswith("Honua."):
            ecosystem, key, declared = "nuget", package.lower(), pin.get("digest")
            pins["nuget"][key] = version
        elif pin.get("filename"):
            ecosystem, key, declared = "pypi", pypi_normalize(package), pin.get("digest")
            pins["pypi"][key] = str(pin["filename"])
        else:
            continue
        if declared:
            digests[ecosystem][key] = str(declared)
    pins["digests"] = digests
    return pins


def digest_matches(data: bytes, declared: str) -> bool:
    """A manifest hash over the bytes: npm SRI (`sha512-<base64>`) or `sha256:<hex>`."""
    if declared.startswith("sha256:"):
        return hashlib.sha256(data).hexdigest() == declared[len("sha256:"):].lower()
    algorithm, _, value = declared.partition("-")
    if algorithm not in {"sha256", "sha384", "sha512"}:
        return False
    return base64.b64encode(hashlib.new(algorithm, data).digest()).decode() == value


def nuget_family_pins(pins: dict[str, dict[str, str]]) -> dict[str, str]:
    """Honua.Sdk ships every Honua.Sdk.* package at one version (single release-please version)."""
    root = pins["nuget"].get("honua.sdk")
    return {"__family__": root} if root else {}


EXACT = re.compile(r"^=?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def honua_dependencies(manifest_entry: dict[str, Any]) -> dict[str, str]:
    """Honua packages a version depends on: the exact version, or "*" when it names a range.

    A pinned package's dependency closure is part of the pinned artifact, so the guard admits it
    (and reports it); a range admits every version and npm picks the one that satisfies it.
    """
    out: dict[str, str] = {}
    for section in ("dependencies", "optionalDependencies"):
        for name, spec in (manifest_entry.get(section) or {}).items():
            if NPM_HONUA.match(name):
                out[name] = str(spec).lstrip("=") if EXACT.match(str(spec)) else "*"
    return out


def filter_npm_packument(name: str, body: dict[str, Any], pins: dict[str, str],
                         integrity: str | None = None) -> dict[str, Any] | None:
    if not NPM_HONUA.match(name):
        return body
    version = pins.get(name)
    if version == "*":
        return body
    if not version or version not in (body.get("versions") or {}):
        return None
    if integrity and ((body["versions"][version] or {}).get("dist") or {}).get("integrity") != integrity:
        return None
    body = dict(body)
    body["versions"] = {version: body["versions"][version]}
    body["dist-tags"] = {"latest": version}
    if isinstance(body.get("time"), dict):
        body["time"] = {k: v for k, v in body["time"].items() if k in {"created", "modified", version}}
    return body


ANCHOR = re.compile(r"<a\b[^>]*>([^<]+)</a>\s*(?:<br\s*/?>)?", re.I)
SHA256_FRAGMENT = re.compile(r'href="[^"#]*#sha256=([0-9a-fA-F]{64})"')


def filter_pypi_simple(name: str, html: str, pins: dict[str, str], digest: str | None = None) -> str | None:
    if not PYPI_HONUA.match(name):
        return html
    filename = pins.get(pypi_normalize(name))
    if not filename:
        return None
    kept = [m.group(0) for m in ANCHOR.finditer(html) if m.group(1).strip() == filename]
    if digest:   # pip verifies the download against this fragment, so the fragment must be the pin
        kept = [a for a in kept if (f := SHA256_FRAGMENT.search(a))
                and "sha256:" + f.group(1).lower() == digest.lower()]
    if not kept:
        return None
    return "<!DOCTYPE html><html><body>\n" + "\n".join(kept) + "\n</body></html>\n"


def filter_nuget_versions(package_id: str, body: dict[str, Any], pins: dict[str, str]) -> dict[str, Any] | None:
    lower = package_id.lower()
    if not NUGET_HONUA.match(lower):
        return body
    version = pins.get(lower) or (pins.get("__family__") if lower.startswith("honua.sdk") else None)
    if not version:
        return None
    versions = [v for v in body.get("versions", []) if v.lower() == version.lower()]
    return {"versions": versions} if versions else None


def filter_nuget_registration(index: dict[str, Any], version: str) -> dict[str, Any] | None:
    """Keep only the pinned version's leaf in an (inlined) NuGet registration index."""
    pages = []
    for page in index.get("items") or []:
        leaves = [leaf for leaf in page.get("items") or []
                  if str(leaf.get("catalogEntry", {}).get("version", "")).lower() == version.lower()]
        if leaves:
            pages.append({**page, "items": leaves, "count": len(leaves), "lower": version, "upper": version})
    if not pages:
        return None
    return {**index, "items": pages, "count": len(pages)}


class Guard:
    def __init__(self, pins: dict[str, dict[str, str]], host: str = "127.0.0.1", port: int = 0,
                 refusals_path: str | None = None):
        self.pins = pins
        self.digests: dict[str, dict[str, str]] = pins.get("digests") or {"npm": {}, "pypi": {}, "nuget": {}}
        self.npm_closure: dict[str, str] = {}     # exact Honua dependencies of pinned npm packages
        self.nuget_pins = dict(self.pins["nuget"], **nuget_family_pins(self.pins))
        self.refusals: list[dict[str, str]] = []
        self.refusals_path = refusals_path
        self.lock = threading.Lock()
        guard = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def do_GET(self) -> None:  # noqa: N802
                guard.handle(self)

            def do_HEAD(self) -> None:  # noqa: N802
                guard.handle(self, head=True)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "Guard":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def refuse(self, handler: BaseHTTPRequestHandler, ecosystem: str, name: str) -> None:
        with self.lock:
            entry = {"ecosystem": ecosystem, "package": name}
            if entry not in self.refusals:
                self.refusals.append(entry)
                if self.refusals_path:
                    with open(self.refusals_path, "w", encoding="utf-8") as handle:
                        json.dump(self.refusals, handle)
        self._send(handler, 404, json.dumps({"error": f"{name} is not a manifest-pinned Honua package"}).encode(),
                   "application/json")

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, code: int, body: bytes, ctype: str, head: bool = False) -> None:
        handler.send_response(code)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if not head:
            handler.wfile.write(body)

    @staticmethod
    def _fetch(url: str, accept: str | None = None) -> tuple[int, bytes, str]:
        request = urllib.request.Request(url, headers={"User-Agent": "honua-executable-docs-guard",
                                                       "Accept-Encoding": "gzip"})
        if accept:
            request.add_header("Accept", accept)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return response.status, data, response.headers.get("Content-Type", "application/octet-stream")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type", "text/plain")

    def handle(self, handler: BaseHTTPRequestHandler, head: bool = False) -> None:
        path = urllib.parse.urlsplit(handler.path).path
        try:
            if path.startswith("/npm/"):
                self._npm(handler, path[len("/npm/"):], head)
            elif path.startswith("/pypi/simple/"):
                self._pypi(handler, path[len("/pypi/simple/"):], head)
            elif path == "/nuget/v3/index.json":
                body = json.dumps({"version": "3.0.0", "resources": [
                    {"@id": f"{self.base}/nuget/flat/", "@type": "PackageBaseAddress/3.0.0"},
                    {"@id": f"{self.base}/nuget/reg/", "@type": "RegistrationsBaseUrl"},
                    {"@id": f"{self.base}/nuget/reg/", "@type": "RegistrationsBaseUrl/3.0.0-rc"},
                ]}).encode()
                self._send(handler, 200, body, "application/json", head)
            elif path.startswith("/nuget/flat/"):
                self._nuget_flat(handler, path[len("/nuget/flat/"):], head)
            elif path.startswith("/nuget/reg/"):
                self._nuget_registration(handler, path[len("/nuget/reg/"):], head)
            else:
                self._send(handler, 404, b"unknown registry path", "text/plain", head)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _npm(self, handler: BaseHTTPRequestHandler, rest: str, head: bool) -> None:
        rest = urllib.parse.unquote(rest)
        if "/-/" in rest:   # tarball
            name = rest.split("/-/", 1)[0]
            filename = rest.split("/-/", 1)[1]
            if NPM_HONUA.match(name):
                version = self.pins["npm"].get(name) or self.npm_closure.get(name)
                if not version or (version != "*" and not filename.endswith(f"-{version}.tgz")):
                    self.refuse(handler, "npm", f"{name} ({filename})")
                    return
            code, data, ctype = self._fetch(f"{NPM_UPSTREAM}/{name}/-/{filename}")
            declared = self.digests["npm"].get(name)
            if code == 200 and declared and not digest_matches(data, declared):
                self.refuse(handler, "npm", f"{name} ({filename}) differs from the manifest integrity")
                return
            self._send(handler, code, data, ctype, head)
            return
        name = rest.rstrip("/")
        accept = handler.headers.get("Accept") or "application/json"
        code, data, ctype = self._fetch(f"{NPM_UPSTREAM}/{urllib.parse.quote(name, safe='@')}", accept)
        if code != 200:
            if NPM_HONUA.match(name):
                self.refuse(handler, "npm", name)
                return
            self._send(handler, code, data, ctype, head)
            return
        body = filter_npm_packument(name, json.loads(data), {**self.npm_closure, **self.pins["npm"]},
                                    self.digests["npm"].get(name))
        if body is None:
            self.refuse(handler, "npm", name)
            return
        with self.lock:
            for version_entry in (body.get("versions") or {}).values():
                for dep, version in honua_dependencies(version_entry).items():
                    if dep not in self.pins["npm"] and self.npm_closure.get(dep) != "*":
                        self.npm_closure[dep] = version
            if self.refusals_path:
                with open(self.refusals_path.replace("refusals", "closure"), "w", encoding="utf-8") as handle:
                    json.dump(self.npm_closure, handle)
        self._send(handler, 200, json.dumps(body).encode(), "application/json", head)

    def _pypi(self, handler: BaseHTTPRequestHandler, rest: str, head: bool) -> None:
        name = rest.strip("/")
        code, data, ctype = self._fetch(f"{PYPI_UPSTREAM}/{name}/", "text/html")
        if code != 200:
            if PYPI_HONUA.match(name):
                self.refuse(handler, "pypi", name)
                return
            self._send(handler, code, data, ctype, head)
            return
        text = filter_pypi_simple(name, data.decode("utf-8"), self.pins["pypi"],
                                  self.digests["pypi"].get(pypi_normalize(name)))
        if text is None:
            self.refuse(handler, "pypi", name)
            return
        self._send(handler, 200, text.encode(), "text/html", head)

    def _rewrite_nuget(self, data: bytes) -> bytes:
        return (data.replace(NUGET_REG_UPSTREAM.encode() + b"/", f"{self.base}/nuget/reg/".encode())
                    .replace(NUGET_FLAT_UPSTREAM.encode() + b"/", f"{self.base}/nuget/flat/".encode()))

    def _nuget_registration(self, handler: BaseHTTPRequestHandler, rest: str, head: bool) -> None:
        package_id = rest.split("/", 1)[0]
        if not NUGET_HONUA.match(package_id):
            code, data, ctype = self._fetch(f"{NUGET_REG_UPSTREAM}/{rest.lower()}")
            self._send(handler, code, self._rewrite_nuget(data), ctype, head)
            return
        lower = package_id.lower()
        version = self.nuget_pins.get(lower) or (self.nuget_pins.get("__family__") if lower.startswith("honua.sdk") else None)
        if not version:
            self.refuse(handler, "nuget", package_id)
            return
        leaf = rest.split("/", 1)[1] if "/" in rest else "index.json"
        if leaf != "index.json":
            if leaf.lower() != f"{version.lower()}.json":
                self.refuse(handler, "nuget", f"{package_id} {leaf[:-5]}")
                return
            code, data, ctype = self._fetch(f"{NUGET_REG_UPSTREAM}/{lower}/{leaf.lower()}")
            self._send(handler, code, self._rewrite_nuget(data), ctype, head)
            return
        code, data, _ = self._fetch(f"{NUGET_REG_UPSTREAM}/{lower}/index.json")
        if code != 200:
            self.refuse(handler, "nuget", package_id)
            return
        index = json.loads(data)
        for page in index.get("items") or []:   # inline any page the hub left external
            if "items" not in page and page.get("@id"):
                page_code, page_data, _ = self._fetch(page["@id"])
                if page_code == 200:
                    page["items"] = json.loads(page_data).get("items", [])
        body = filter_nuget_registration(index, version)
        if body is None:
            self.refuse(handler, "nuget", package_id)
            return
        self._send(handler, 200, self._rewrite_nuget(json.dumps(body).encode()), "application/json", head)

    def _nuget_flat(self, handler: BaseHTTPRequestHandler, rest: str, head: bool) -> None:
        parts = rest.split("/")
        package_id = parts[0]
        if len(parts) >= 2 and parts[1] == "index.json":
            code, data, ctype = self._fetch(f"{NUGET_FLAT_UPSTREAM}/{package_id.lower()}/index.json")
            if code != 200:
                if NUGET_HONUA.match(package_id):
                    self.refuse(handler, "nuget", package_id)
                    return
                self._send(handler, code, data, ctype, head)
                return
            body = filter_nuget_versions(package_id, json.loads(data), self.nuget_pins)
            if body is None:
                self.refuse(handler, "nuget", package_id)
                return
            self._send(handler, 200, json.dumps(body).encode(), "application/json", head)
            return
        if NUGET_HONUA.match(package_id) and len(parts) >= 2:
            allowed = filter_nuget_versions(package_id, {"versions": [parts[1]]}, self.nuget_pins)
            if not allowed or not allowed["versions"]:
                self.refuse(handler, "nuget", f"{package_id} {parts[1]}")
                return
        code, data, ctype = self._fetch(f"{NUGET_FLAT_UPSTREAM}/{rest.lower()}")
        declared = self.digests["nuget"].get(package_id.lower())
        if code == 200 and declared and parts[-1].lower().endswith(".nupkg") and not digest_matches(data, declared):
            self.refuse(handler, "nuget", f"{package_id} {parts[1]} differs from the manifest digest")
            return
        self._send(handler, code, data, ctype, head)


def main() -> int:
    """Serve inside a sidecar container that shares the doc containers' network."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--pins", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--refusals", required=True)
    args = parser.parse_args()
    with open(args.pins, encoding="utf-8") as handle:
        pins = json.load(handle)
    guard = Guard(pins, port=args.port, refusals_path=args.refusals)
    print(f"registry guard on {guard.base}", flush=True)
    guard.server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

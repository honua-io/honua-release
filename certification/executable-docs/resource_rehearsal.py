"""Owned-resource controls against native I/O and an integrity-bound public SDK.

These local fixtures test the interpreter, never candidate/server qualification.
The published SDK path is used by live Docker regressions, not offline self-tests.
"""
import base64
import hashlib
import http.server
import io
import json
import socketserver
import tarfile
import threading
import urllib.request
from pathlib import Path, PurePosixPath

import yaml


def install_published_sdk(root, destination):
    pin = yaml.safe_load((root / "platform-manifest.yaml").read_text())["clientArtifacts"]["honua-sdk-js"]
    metadata_url = f"https://registry.npmjs.org/@honua%2fsdk-js/{pin['version']}"
    with urllib.request.urlopen(metadata_url, timeout=30) as response:
        metadata = json.loads(response.read(1024 * 1024))
    assert metadata["gitHead"] == pin["sourceSha"]
    assert metadata["dist"]["integrity"] == pin["integrity"]
    url = f"https://registry.npmjs.org/@honua/sdk-js/-/{pin['filename']}"
    assert metadata["dist"]["tarball"] == url
    with urllib.request.urlopen(url, timeout=30) as response:
        body = response.read(20 * 1024 * 1024)
        assert not response.read(1), "SDK archive exceeded control limit"
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(body).digest()).decode()
    assert integrity == pin["integrity"]
    package = destination / "node_modules/@honua/sdk-js"
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            assert not path.is_absolute() and path.parts[0] == "package" and ".." not in path.parts
            assert member.isfile() or member.isdir(), "SDK control archive contains links/special files"
            if member.isfile():
                target = package.joinpath(*path.parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
    manifest = json.loads((package / "package.json").read_text())
    assert manifest["name"] == pin["package"] and manifest["version"] == pin["version"]
    return {"package": pin["package"], "version": pin["version"], "sourceSha": pin["sourceSha"],
            "tarball": url, "integrity": integrity, "sha256": hashlib.sha256(body).hexdigest()}


def socket_after_pass_control(session):
    release = threading.Event()
    connected = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            connected.set()
            if release.wait(5):
                try:
                    self.request.sendall(b"late data")
                except OSError:
                    pass  # cleanup may have already closed the public socket

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            first = session.run_js("const ownedSocket = require('node:net').connect(" + str(port) +
                ", '127.0.0.1'); await new Promise(resolve => ownedSocket.once('connect', resolve)); "
                "ownedSocket.on('data', () => require('node:fs').writeFileSync('socket-stale', 'bad')); "
                "ownedSocket.unref();", "javascript", 10)
            assert first.status == "pass", first
            assert connected.wait(1)
            release.set()  # the native I/O event is deliberately AFTER completion
            later = session.run_js("await new Promise(resolve => setTimeout(resolve, 60)); "
                "console.log('healthy');", "javascript", 10)
            assert later.status == "pass" and later.stdout == "healthy\n", later
            assert not Path(session.cwd, "socket-stale").exists()
        finally:
            release.set()
            server.shutdown()
            thread.join(2)


def published_sdk_pool_control(session, root):
    artifact = install_published_sdk(root, Path(session.cwd))
    counts = {"requests": 0, "connections": 0}

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def get_request(self):
            connection = super().get_request()
            counts["connections"] += 1
            return connection

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            assert self.path.startswith("/rest/services/pool/FeatureServer/0/query?")
            counts["requests"] += 1
            body = b'{"features":[{"attributes":{"OBJECTID":42}}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with Server(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            setup = "import {HonuaClient} from '@honua/sdk-js';\nconst pooledClient = new HonuaClient({baseUrl:" + \
                json.dumps(f"http://127.0.0.1:{server.server_port}") + \
                ", timeoutMs:1000, retry:{maxRetries:0}});\n"
            query = "(await pooledClient.queryFeatures({serviceId:'pool',layerId:0,where:'1=1'})).features[0].attributes.OBJECTID"
            native = Path(session.cwd, "native-pool.mjs")
            session.put("node", native, setup + f"console.log(JSON.stringify([{query}, {query}]));")
            oracle = session.run_shell("node native-pool.mjs", "node", 20, False)
            assert oracle.status == "pass" and oracle.stdout == "[42,42]\n", oracle
            before = dict(counts)
            first = session.run_js(setup + f"console.log({query});", "javascript", 20)
            assert first.status == "pass" and first.stdout == "42\n", first
            second = session.run_js(f"console.log({query});", "javascript", 20)
            assert second.status == "pass" and second.stdout == "42\n", second
            assert counts["requests"] - before["requests"] == 2
            assert counts["connections"] - before["connections"] == 2, "idle owned pool must reconnect after cleanup"
            artifact["workerRequests"] = 2
            artifact["workerConnections"] = 2
        finally:
            server.shutdown()
            thread.join(2)
    return artifact

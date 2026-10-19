"""Reproduce the fixed-client manifest from authenticated npm archives, offline."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import PurePosixPath
import tarfile

CLIENTS = {
    "sdk": {"canonicalClient": "Official MCP TypeScript SDK", "name": "@modelcontextprotocol/sdk", "version": "1.32.1",
            "integrity": "sha512-2DdE+SJDtzLEEWzY1ZjY7Q+VcPhcV1KisD3zI4u0XZyktsjHum1mwbMI+JaulUBi2OZk+KJAi2uPXzxichPkdw==",
            "entry": "dist/esm/client/index.js", "clientInfo": {"name": "geospatial-mcp-map-package-certification", "version": "1.0.0"}},
    "inspector": {"canonicalClient": "MCP Inspector", "name": "@modelcontextprotocol/inspector", "version": "2.10.1",
                  "integrity": "sha512-5lU14Qms3MNQQ+3OUKKYPWewWh9cUsPCmJrZ3vsQTSR4O9UAXfmIcZiSx5gDgvJtrXRXGn7E1gIDC5TY7cI1pQ==",
                  "entry": "clients/launcher/build/index.js", "clientInfo": {"name": "inspector-cli", "version": "2.10.1"}},
}


def package(selector, data):
    selected = CLIENTS[selector]
    if "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode() != selected["integrity"]:
        raise ValueError("npm archive integrity differs")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            if member.isdir():
                continue
            path = PurePosixPath(member.name)
            if (not member.isfile() or not member.name.startswith("package/") or ".." in path.parts
                    or path.is_absolute() or str(path) != member.name):
                raise ValueError("unexpected npm archive entry")
            name = member.name.removeprefix("package/")
            if name in files:
                raise ValueError("duplicate npm archive entry")
            files[name] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
        metadata = json.loads(archive.extractfile("package/package.json").read())
    if (metadata["name"], metadata["version"]) != (selected["name"], selected["version"]) or selected["entry"] not in files:
        raise ValueError("npm package identity differs")
    return {key: selected[key] for key in ("canonicalClient", "name", "version", "integrity")} | {
        "tarball": f"https://registry.npmjs.org/{selected['name']}/-/{selector}-{selected['version']}.tgz",
        "entry": selected["entry"], "clientInfo": selected["clientInfo"], "files": dict(sorted(files.items()))}


def main():
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    for name in CLIENTS:
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        clients = {name: package(name, getattr(args, name).read_bytes()) for name in CLIENTS}
        print(json.dumps({"schema": "honua.mcp-fixed-client-manifest/v1", "clients": clients}, indent=2))
    except (ValueError, KeyError, OSError, tarfile.TarError) as exc:
        parser.exit(1, f"fixed client manifest refused: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()

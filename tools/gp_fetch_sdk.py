#!/usr/bin/env python3
"""Acquire an exact complete Linux x64 SDK from Microsoft's checked release metadata."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

METADATA = "https://builds.dotnet.microsoft.com/dotnet/release-metadata/10.0/releases.json"


class InputError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        raise InputError("Official SDK acquisition must not redirect")


def select(metadata, version):
    if not re.fullmatch(r"10\.0\.[0-9]+", version):
        raise InputError("Select an exact stable .NET 10 SDK version")
    matches = []
    for release in metadata.get("releases", []):
        for sdk in [release.get("sdk", {}), *release.get("sdks", [])]:
            if sdk.get("version") != version:
                continue
            matches.extend(entry for entry in sdk.get("files", []) if
                           entry.get("rid") == "linux-x64" and entry.get("name") == "dotnet-sdk-linux-x64.tar.gz")
    identities = {(entry.get("url"), entry.get("hash", "").lower()) for entry in matches}
    if len(identities) != 1:
        raise InputError("Official metadata must identify one exact Linux x64 SDK archive")
    url, digest = identities.pop()
    parsed = urlsplit(url)
    expected_path = f"/dotnet/Sdk/{version}/dotnet-sdk-{version}-linux-x64.tar.gz"
    if (parsed.scheme != "https" or parsed.netloc != "builds.dotnet.microsoft.com" or
            parsed.path != expected_path or parsed.query or parsed.fragment or
            not re.fullmatch(r"[0-9a-f]{128}", digest)):
        raise InputError("SDK archive identity is outside the exact official release path")
    return url, digest


def acquire(version, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(METADATA, timeout=90) as response:
        metadata = response.read(16 * 1024 * 1024 + 1)
    if len(metadata) > 16 * 1024 * 1024:
        raise InputError("SDK metadata exceeds its size budget")
    url, expected = select(json.loads(metadata), version)
    target = destination / f"dotnet-sdk-{version}-linux-x64.tar.gz"
    partial = target.with_suffix(".partial")
    sha512, sha256, count = hashlib.sha512(), hashlib.sha256(), 0
    try:
        with opener.open(url, timeout=90) as response, partial.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > 512 * 1024 * 1024:
                    raise InputError("SDK archive exceeds its size budget")
                sha512.update(chunk)
                sha256.update(chunk)
                output.write(chunk)
        if sha512.hexdigest() != expected:
            raise InputError("Complete SDK archive differs from Microsoft's SHA-512")
        partial.rename(target)
    finally:
        partial.unlink(missing_ok=True)
    receipt = {"version": version, "rid": "linux-x64", "url": url, "bytes": count,
               "sha512": expected, "sha256": sha256.hexdigest(), "metadataUrl": METADATA,
               "metadataSha256": hashlib.sha256(metadata).hexdigest(), "file": target.name}
    (destination / "official-metadata.json").write_bytes(metadata)
    (destination / "official-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    print(json.dumps(acquire(options.version, options.output)))

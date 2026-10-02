"""Install and invoke the manifest's published .NET SDK, without source fallback."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pins
from transport import ExecutionError

HERE = Path(__file__).resolve().parent


def prepare(manifest, workdir):
    pin = manifest.get("clientArtifacts", {}).get("honua-sdk-dotnet")
    if not pin or pin.get("ecosystem") != "nuget" or pin.get("registry") != "github-packages":
        raise ExecutionError("install published .NET SDK", "manifest lacks a supported published .NET SDK pin", blocked=True)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise ExecutionError("install published .NET SDK", "GitHub Packages read credential is unavailable", blocked=True)
    verifier = pins._load_verifier()
    owner = pin["repository"].split("/")[0]
    package, version = pin["package"], pin["version"]
    url = (f"https://nuget.pkg.github.com/{urllib.parse.quote(owner)}/download/"
           f"{urllib.parse.quote(package)}/{urllib.parse.quote(version)}/{urllib.parse.quote(package + '.' + version + '.nupkg')}")
    try:
        data = verifier._request(url, token=token)
        if "sha256:" + hashlib.sha256(data).hexdigest() != pin["digest"]:
            raise ExecutionError("install published .NET SDK", "downloaded package does not match manifest digest")
        verifier._verify_nuget(data, package, version)
    except verifier.VerificationError as exc:
        raise ExecutionError("install published .NET SDK", "published package verification failed", blocked=True) from exc
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [n for n in archive.namelist() if n.endswith(".nuspec")]
        if len(names) != 1:
            raise ExecutionError("install published .NET SDK", "package has no unique nuspec")
        root = ET.fromstring(archive.read(names[0]))
        dependencies = [n for n in root.iter() if n.tag.rsplit("}", 1)[-1] == "dependency"
                        and n.attrib.get("id") == "Honua.Sdk.Admin"]
        if not dependencies or any(n.attrib.get("version", "").strip("[]") != version for n in dependencies):
            raise ExecutionError("install published .NET SDK", "meta-package does not bind Admin to its exact version")
    destination = Path(workdir) / "sdk"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "sdk-import" / "JourneyImport.csproj", destination / "JourneyImport.csproj")
    shutil.copyfile(HERE / "sdk-import" / "Program.cs", destination / "Program.cs")
    # Source configuration contains URLs only. The read credential is an ephemeral
    # NuGet environment override, not XML, argv, logs or receipt data.
    (destination / "NuGet.Config").write_text(
        '<configuration><packageSources><clear />'
        '<add key="nuget" value="https://api.nuget.org/v3/index.json" />'
        f'<add key="journey" value="https://nuget.pkg.github.com/{owner}/index.json" />'
        '</packageSources><packageSourceMapping>'
        '<packageSource key="journey"><package pattern="Honua.Sdk.*" /></packageSource>'
        '<packageSource key="nuget"><package pattern="*" /></packageSource>'
        '</packageSourceMapping></configuration>')
    env = {**os.environ, "NuGetPackageSourceCredentials_journey":
           f"Username=journey-reader;Password={token};ValidAuthenticationTypes=Basic",
           "NUGET_PACKAGES": str(destination / "packages")}
    result = subprocess.run(["dotnet", "build", str(destination / "JourneyImport.csproj"),
                             "-c", "Release", f"-p:JourneySdkVersion={version}",
                             "--verbosity", "quiet"], env=env, capture_output=True, timeout=1200, check=False)
    if result.returncode:
        raise ExecutionError("dotnet build JourneyImport.csproj", "published SDK restore/build failed", blocked=True)
    return {"dll": str(destination / "bin" / "Release" / "net10.0" / "JourneyImport.dll"),
            "package": package, "version": version, "sha256": hashlib.sha256(data).hexdigest()}


def invoke(binding, method, arguments, *, base_url, credential):
    if not binding or not Path(binding["dll"]).is_file():
        raise ExecutionError(method, "verified published SDK bridge is unavailable", blocked=True)
    env = {**os.environ, "HONUA_JOURNEY_BASE_URL": base_url, "HONUA_JOURNEY_SDK_KEY": credential}
    try:
        result = subprocess.run(["dotnet", binding["dll"]],
                                input=json.dumps({"method": method, "arguments": arguments}),
                                env=env, capture_output=True, text=True, timeout=120, check=False)
        response = json.loads(result.stdout)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise ExecutionError(method, "published SDK invocation failed") from exc
    if response.get("status") != "pass":
        raise ExecutionError(method, "published SDK refused the operation",
                             blocked=response.get("status") == "blocked")
    return response["result"]

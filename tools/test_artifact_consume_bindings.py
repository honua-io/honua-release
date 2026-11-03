"""gate-artifact-consume's resolver: a declared terraform/gRPC artifact binds the manifest pin or resolution fails.

Runs the workflow's own resolver program (no copy) against the committed manifest and mutated copies."""
import copy
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gate-artifact-consume.yml"


def _resolver():
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["resolve_pins"]["steps"]
    run = next(s["run"] for s in steps if s.get("id") == "pins")
    return textwrap.dedent(run.split("<<'PY'\n", 1)[1].rsplit("PY", 1)[0])


def _resolve(tmp_path, manifest):
    (tmp_path / "platform-manifest.yaml").write_text(yaml.safe_dump(manifest))
    out = tmp_path / "out.txt"
    out.write_text("")
    result = subprocess.run([sys.executable, "-c", _resolver()], cwd=tmp_path, capture_output=True, text=True,
                            env={**os.environ, "GITHUB_OUTPUT": str(out)})
    values = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return result, values


def _manifest():
    return yaml.safe_load((ROOT / "platform-manifest.yaml").read_text())


def test_the_committed_pins_resolve_to_the_published_bindings(tmp_path):
    result, values = _resolve(tmp_path, _manifest())
    assert result.returncode == 0, result.stderr
    iac = _manifest()["components"]["honua-iac"]
    assert values["iac_archive_url"] == f"https://github.com/honua-io/honua-iac/archive/{iac['sha']}.tar.gz"
    assert values["iac_archive_sha256"] == iac["artifactSha256"]
    grpc = _manifest()["components"]["geospatial-grpc"]
    assert values["grpc_nupkg_id"] == "Geospatial.Grpc" and values["grpc_nupkg_sha256"] == grpc["artifactSha256"]


@pytest.mark.parametrize("component, key, value", [
    ("honua-iac", "artifact", "archive:https://github.com/honua-io/honua-iac/archive/" + "0" * 40 + ".tar.gz"),
    ("honua-iac", "artifactSourceRevision", "0" * 40),
    ("honua-iac", "artifactSha256", "sha256:short"),
    ("honua-iac", "artifactSha256", None),
    ("geospatial-grpc", "artifactSourceRevision", "0" * 40),
    ("geospatial-grpc", "artifactSha256", "md5:abc"),
    ("geospatial-grpc", "artifact", "nuget:bad id"),
])
def test_a_declared_artifact_with_a_wrong_binding_fails_resolution(tmp_path, component, key, value):
    manifest = copy.deepcopy(_manifest())
    if value is None:
        manifest["components"][component].pop(key)
    else:
        manifest["components"][component][key] = value
    result, values = _resolve(tmp_path, manifest)
    assert result.returncode != 0 and "not bound to its pinned sha" in result.stderr
    assert "iac_archive_url" not in values and "grpc_nupkg_id" not in values


def test_an_undeclared_artifact_leaves_the_published_attempt_empty(tmp_path):
    manifest = copy.deepcopy(_manifest())
    manifest["components"]["honua-iac"]["artifact"] = "terraform-module:honua"
    manifest["components"]["geospatial-grpc"]["artifact"] = "buf:geospatial"
    result, values = _resolve(tmp_path, manifest)
    assert result.returncode == 0, result.stderr
    assert values["iac_archive_url"] == "" and values["grpc_nupkg_id"] == ""


def test_terraform_never_extracts_a_partial_or_unverified_pinned_download():
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["consume-terraform"]["steps"]
    run = next(s["run"] for s in steps if s.get("id") == "consume")
    published = run.split("Try PUBLISHED", 1)[1].split("::endgroup::", 1)[0]
    assert published.count('rm -f "$WORK/pinned-archive.tgz"') == 2      # digest mismatch and failed download
    assert 'TARBALL="$WORK/pinned-archive.tgz"; STRIP=1' in published
    assert '[ -n "$TARBALL" ] || TARBALL=' in run and "honua-iac-*.tar.gz" in run
    assert "could not be extracted" in run

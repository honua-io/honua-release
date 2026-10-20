"""Owned native API control only. Never parses the large proposal or installs packages."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

parser = argparse.ArgumentParser(description=__doc__)
for name in ("implementation", "subject", "output"):
    parser.add_argument("--" + name, required=True, type=Path)
parser.add_argument("--tool-wheel", type=Path)
args = parser.parse_args()
code = Path(__file__).parent.resolve()
implementation, subject = args.implementation.resolve(), args.subject.resolve()
output = args.output.resolve() / uuid.uuid4().hex
output.mkdir(parents=True, mode=0o700)
sys.dont_write_bytecode = True
sys.path.insert(0, str(code))
import python_source_inputs as inputs
from python_source_layout import canonical_layout
from python_source_session import _create
from verify_python_source_witness import selected_image_chain, save

selection, _ = inputs.selected_inputs()


def git(*arguments):
    return subprocess.check_output(["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(implementation), *arguments])


assert git("rev-parse", "HEAD").decode().strip() == selection["producer"]["sourceSha"]
assert not git("status", "--porcelain", "--untracked-files=all").strip()
for raw in git("ls-tree", "-rz", "--full-tree", selection["producer"]["sourceSha"]).split(b"\0"):
    if not raw:
        continue
    meta, name = raw.split(b"\t", 1)
    mode, kind, blob = meta.decode().split()
    path = implementation / os.fsdecode(name)
    assert kind == "blob" and mode in ("100644", "100755", "120000")
    data = os.fsencode(os.readlink(path)) if mode == "120000" else path.read_bytes()
    assert hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == blob
policy = inputs.read_json((implementation / "scripts/development-inputs/governed-runtime.json").read_bytes())
layout = canonical_layout(policy)
sys.path.insert(0, str(implementation / "scripts"))
import run_governed_source_inputs as launcher
if args.tool_wheel is None:
    wheel, tool_receipt = launcher.download_implementation_tool(policy, output)
    save(output, "implementation-tool.json", tool_receipt)
else:
    wheel = args.tool_wheel.resolve()
assert inputs.sha_file(wheel) == policy["executionBinding"]["implementationPackagingSha256"]

chain = selected_image_chain(selection, inputs.SELECTION.parent)
image = "python:3.12-bookworm@" + selection["image"]["indexDigest"]
observed_image = inputs.read_json(subprocess.check_output(["docker", "image", "inspect", "--format",
    '{"Id":{{json .Id}},"RepoDigests":{{json .RepoDigests}},"Os":{{json .Os}},"Architecture":{{json .Architecture}}}', image]))
assert observed_image["Id"] in (selection["image"]["indexDigest"], selection["image"]["configDigest"])
assert "python@" + selection["image"]["indexDigest"] in observed_image["RepoDigests"]
assert observed_image["Os"] == "linux" and observed_image["Architecture"] == "amd64"
home, request = output / "home", output / "probe-request"
home.mkdir(mode=0o700)
request.mkdir(mode=0o700)
uid, gid = os.getuid(), os.getgid()
user = f"{uid}:{gid}"
tmpfs = f"/tmp:rw,exec,mode=0700,uid={uid},gid={gid}"
save(request, "context.json", {"mountSources": {"implementation": str(implementation), "subject": str(subject),
    "tool": str(wheel), "output": str(output)}, "user": user, "tmpfs": tmpfs,
    "image": {"id": observed_image["Id"], "repoDigests": observed_image["RepoDigests"],
              "declaredImage": image, "os": "linux", "architecture": "amd64"}})
mounts = [(code, "/controller", True), (implementation, "/implementation", True),
          (wheel, str(layout["toolWheel"]), True), (wheel, "/legacy-tool.whl", True),
          (home, "/runtime-home", True), (request, "/probe-request", True)]
arguments = ["/usr/local/bin/python3.12", "-I", "/controller/python_source_runtime_probe.py"]
container, owner = _create(directory=output, image=image, arguments=arguments, cwd="/controller",
    home="/runtime-home", mounts=mounts, user=user, tmpfs=tmpfs)


def observe(name):
    actual = launcher.inspect_container(container)
    launcher.validate_container(actual, container, owner=owner, image_id=observed_image["Id"],
        arguments=arguments, mounts=mounts, user=user, tmpfs=tmpfs)
    network = inputs.read_json(subprocess.check_output(["docker", "inspect", "--format", "{{json .HostConfig.NetworkMode}}", container]))
    assert network == "none"
    save(output, name, actual)
    return actual


try:
    assert observe("before.json")["status"] == "created"
    with (output / "stdout").open("xb") as stdout, (output / "stderr").open("xb") as stderr:
        actual = subprocess.run(["docker", "start", "--attach", container], stdout=stdout, stderr=stderr, check=False)
    after = observe("after.json")
    save(output, "receipt.json", {"containerId": container, "exitCode": actual.returncode,
        "observedExitCode": after["exitCode"], "imageChain": chain,
        "stdoutSha256": inputs.sha_file(output / "stdout"), "stderrSha256": inputs.sha_file(output / "stderr"),
        "fullProposalVerified": False, "originalInstallCommandsExecuted": False,
        "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False})
    assert not after["running"] and after["status"] == "exited" and actual.returncode == after["exitCode"] == 0
    result = inputs.read_json((output / "stdout").read_bytes())
    assert result["contractContextOnly"] is True and result["fullProposalVerified"] is False
    assert all(result[key] is False for key in ("sourceAdmitted", "publishedWheelIdentity", "candidateQualified"))
    print(json.dumps({"output": str(output), "containerId": container, "runtimePolicyAndActualActionBuildApiJoin": "PASS",
        "sourceAdmitted": False, "candidateQualified": False}))
finally:
    current = launcher.inspect_container(container)
    save(output, "cleanup-observed.json", current)
    if current["ownerNonce"] == owner and not current["running"] and current["status"] in ("created", "exited"):
        removed = subprocess.check_output(["docker", "rm", container], text=True).strip()
        save(output, "cleanup.json", {"containerId": container, "removed": removed == container})
    else:
        save(output, "cleanup-pending.json", current)

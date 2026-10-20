"""Credential-free canonical verification of the selected source-input witness.

This performs verification only, never producer generation or source installation.
Even a successful result is a prerequisite, not source or document admission.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

import python_source_inputs as inputs
from python_image_binding import verify_image_chain


def git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *arguments], text=True).strip()


def require_checkout(root: Path, revision: str) -> None:
    if (not (root / ".git").is_dir() or git(root, "rev-parse", "HEAD") != revision
            or git(root, "status", "--porcelain", "--untracked-files=all")):
        raise inputs.InputRefusal("canonical verifier requires a clean self-contained exact checkout")


def authenticated_context(context: dict, selection: dict, chain: dict) -> None:
    """Check the byte-pinned pre-action request against the protected launch recipe.

    Its bytes are authenticated by the selected artifact, never reconstructed from
    repeated daemon observations in execution.json.
    """
    index = selection["image"]["indexDigest"]
    image = {"id": chain["configDigest"], "repoDigests": ["python@" + index],
             "declaredImage": "python:3.12-bookworm@" + index, "os": "linux", "architecture": "amd64"}
    sources = context.get("mountSources", {})
    implementation = sources.get("implementation", "")
    if (set(context) != {"mountSources", "user", "tmpfs", "image"}
            or context["image"] != image or set(sources) != {"implementation", "subject", "tool", "output"}
            or re.fullmatch(r"/tmp/honua-governed-inputs-[a-z0-9_]+/implementation", implementation) is None
            or sources["subject"] != str(Path(implementation).parent / "subject")
            or re.fullmatch(r"/tmp/honua-implementation-tool-[a-z0-9_]+/packaging-26\.3-py3-none-any\.whl", sources["tool"]) is None
            or sources["output"] != "/home/runner/work/_temp/governed-development-inputs"
            or context["user"] != "1001:1001"
            or context["tmpfs"] != "/tmp:rw,exec,mode=0700,uid=1001,gid=1001"):
        raise inputs.InputRefusal("original pre-action execution request differs from protected launch context")


def selected_image_chain(selection: dict, directory: Path) -> dict:
    chain = verify_image_chain((directory / "image-index.json").read_bytes(),
                               (directory / "image-manifest.json").read_bytes(),
                               (directory / "image-config.json").read_bytes(),
                               admitted_index=selection["image"]["indexDigest"])
    if (chain["platformManifestDigest"] != selection["image"]["manifestDigest"]
            or chain["configDigest"] != selection["image"]["configDigest"]):
        raise inputs.InputRefusal("source-input OCI identity differs")
    return chain


def save(root: Path, name: str, value: dict) -> None:
    (root / name).write_text(json.dumps(value, sort_keys=True) + "\n")


def verify(fetch: Path, implementation: Path, subject: Path, destination: Path) -> dict:
    selection, files = inputs.selected_inputs()
    producer = selection["producer"]
    # Reauthenticate metadata and bytes after the credentialed step has ended.
    inputs.verify_producer(selection, inputs.read_json((fetch / "run.json").read_bytes()),
                           inputs.read_json((fetch / "artifact.json").read_bytes()),
                           inputs.read_json((fetch / "jobs.json").read_bytes()))
    require_checkout(implementation, producer["sourceSha"])
    require_checkout(subject, selection["subject"]["sourceSha"])
    destination.mkdir(mode=0o700)
    originals = destination / "originals"
    inputs.unpack_originals(fetch / "original.zip", originals, selection, files)
    data = inputs.SELECTION.parent
    chain = selected_image_chain(selection, data)
    context = inputs.read_json((originals / "execution-context.json").read_bytes())
    authenticated_context(context, selection, chain)
    policy = inputs.read_json((implementation / "scripts/development-inputs/governed-runtime.json").read_bytes())
    # Canonical source planning imports packaging. Use the producer's already
    # hash-bound read-only tool ZIP; do not depend on or install a host package.
    tool = originals / "proposal/build" / policy["executionBinding"]["implementationPackagingFilename"]
    if inputs.sha_file(tool) != policy["executionBinding"]["implementationPackagingSha256"]:
        raise inputs.InputRefusal("canonical source-planning tool bytes differ")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(tool))
    sys.path.insert(0, str(implementation / "scripts"))
    launcher = importlib.import_module("run_governed_source_inputs")
    engine = importlib.import_module("development_source_inputs")
    documents = importlib.import_module("python_document_inputs")
    initial = engine.source(subject, selection["subject"]["sourceSha"])
    declarations = documents.plan(engine, subject, initial)
    actual_docs = [{"document": row["document"], "documentGitBlob": row["documentGitBlob"],
                    "documentSha256": row["documentSha256"], "index": row["blockIndex"], "sha256": row["blockSha256"]}
                   for row in declarations]
    if actual_docs != selection["documentation"]:
        raise inputs.InputRefusal("source-input documentation requests differ")
    suffixes = ("stdout", "stderr", "attach.json", "container-before.json", "container-after.json", "cleanup-observed.json", "cleanup.json")
    raw = {"execution-context.json": (originals / "execution-context.json").read_bytes()}
    raw.update({action + "." + suffix: (originals / (action + "." + suffix)).read_bytes()
                for action in launcher.ACTIONS for suffix in suffixes})
    outer = launcher.verify_execution_receipt(inputs.read_json((originals / "execution.json").read_bytes()),
        implementation_sha=producer["sourceSha"], subject_sha=selection["subject"]["sourceSha"], policy=policy,
        proposal_bytes=(originals / "proposal/proposal.json").read_bytes(), expected_documentation=declarations,
        execution_context=context, original_files=raw)
    if outer.get("innerArtifactVerificationRequired") is not True:
        raise inputs.InputRefusal("canonical outer did not require full inner verification")
    save(destination, "outer-verification.json", {"result": outer, "imageChain": chain,
         "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False})
    del raw
    run_inner(destination, originals, implementation, subject, selection, policy, launcher)
    inputs.retain_originals(selection, originals, files)
    engine.source(implementation, producer["sourceSha"])
    engine.source(subject, selection["subject"]["sourceSha"])
    result = {"schema": "honua.python-source-witness-verification/v1", "implementationSha": producer["sourceSha"],
              "subjectSha": selection["subject"]["sourceSha"], "artifactSha256": selection["artifact"]["sha256"],
              "innerProposalSha256": selection["innerProposalSha256"], "executionContextSha256": selection["executionContextSha256"],
              "documentationContexts": len(declarations), "orderedPipSteps": sum(len(row["invocations"]) for row in declarations),
              "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False}
    save(destination, "verified-inputs.json", result)
    return result


def run_inner(destination, originals, implementation, subject, selection, policy, launcher):
    image = "python:3.12-bookworm@" + selection["image"]["indexDigest"]
    wheel = originals / "proposal/build" / policy["executionBinding"]["implementationPackagingFilename"]
    if inputs.sha_file(wheel) != policy["executionBinding"]["implementationPackagingSha256"]:
        raise inputs.InputRefusal("canonical verifier tool bytes differ")
    observed = inputs.read_json(subprocess.check_output(["docker", "image", "inspect", "--format",
        '{"Id":{{json .Id}},"RepoDigests":{{json .RepoDigests}},"Os":{{json .Os}},"Architecture":{{json .Architecture}}}', image]))
    if (observed["Id"] not in (selection["image"]["indexDigest"], selection["image"]["configDigest"])
            or observed["Os"] != "linux" or observed["Architecture"] != "amd64"
            or "python@" + selection["image"]["indexDigest"] not in observed["RepoDigests"]):
        raise inputs.InputRefusal("actual verifier image is outside the independently verified OCI chain")
    uid, gid, owner = os.getuid(), os.getgid(), uuid.uuid4().hex
    user = f"{uid}:{gid}"
    tmpfs = f"/tmp:rw,exec,mode=0700,uid={uid},gid={gid}"
    mounts = [(implementation, "/implementation"), (subject, "/subject"), (wheel, "/implementation-tools/" + wheel.name), (originals, "/output")]
    arguments = ["/usr/local/bin/python3.12", "-I", "/implementation/scripts/governed_source_inputs_bootstrap.py",
                 "--implementation-packaging-wheel", "/implementation-tools/" + wheel.name, "--action", "verify",
                 "--source", "/subject", "--revision", selection["subject"]["sourceSha"], "--destination", "/output/proposal",
                 "--runtime-policy", "/implementation/scripts/development-inputs/governed-runtime.json"]
    command = ["docker", "create", "--network", "none", "--read-only", "--label", "io.honua.development-input-owner=" + owner,
               "--user", user, "--workdir", "/implementation", "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
               "--tmpfs", tmpfs]
    for source, target in mounts:
        command += ["--mount", f"type=bind,src={source},dst={target},readonly"]
    command += [image, *arguments]
    container = subprocess.check_output(command, text=True).strip()
    if re.fullmatch(r"[a-f0-9]{64}", container) is None:
        raise inputs.InputRefusal("owned verifier container identity is malformed")
    save(destination, "inner-request.json", {"command": command, "containerId": container, "sourceAdmitted": False})
    observed_image_id = observed["Id"]
    def validate(observed):
        launcher.validate_container(observed, container, owner=owner, image_id=observed_image_id,
            arguments=arguments, mounts=[(source, target, True) for source, target in mounts], user=user, tmpfs=tmpfs)
        network = subprocess.check_output(["docker", "inspect", "--format", "{{json .HostConfig.NetworkMode}}", container])
        observed["networkMode"] = inputs.read_json(network)
        if observed["networkMode"] != "none":
            raise inputs.InputRefusal("actual verifier network is not isolated")
    try:
        before = launcher.inspect_container(container)
        try:
            validate(before)
        finally:
            save(destination, "inner-before.json", before)
        if before["status"] != "created" or before["running"] is not False:
            raise inputs.InputRefusal("actual canonical verifier container differs from owned request")
        with (destination / "inner.stdout").open("xb") as stdout, (destination / "inner.stderr").open("xb") as stderr:
            terminal = subprocess.run(["docker", "start", "--attach", container], stdout=stdout, stderr=stderr, check=False)
        after = launcher.inspect_container(container)
        try:
            validate(after)
        finally:
            save(destination, "inner-verification.json", {"before": before, "after": after, "exitCode": terminal.returncode,
                 "stdoutSha256": inputs.sha_file(destination / "inner.stdout"), "stderrSha256": inputs.sha_file(destination / "inner.stderr"),
                 "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False})
        if (after["ownerNonce"] != owner or after["running"] or after["status"] != "exited"
                or terminal.returncode != 0 or after["exitCode"] != 0):
            raise inputs.InputRefusal("canonical full inner verification refused; original streams retained")
    finally:
        current = launcher.inspect_container(container)
        if current["ownerNonce"] == owner and not current["running"] and current["status"] in ("created", "exited"):
            removed = subprocess.check_output(["docker", "rm", container], text=True).strip()
            save(destination, "owned-cleanup.json", {"containerId": container, "removed": removed == container})
        else:
            save(destination, "owned-cleanup-pending.json", current)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("fetch", "implementation", "subject", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.fetch.resolve(), args.implementation.resolve(), args.subject.resolve(), args.output.resolve()), sort_keys=True))

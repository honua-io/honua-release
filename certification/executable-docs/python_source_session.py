"""Execute the three original source blocks under an explicit immutable recipe.

This is source-block execution, not complete README/INSTALL replay or source
admission. The owning run.py document-continuity join remains separately required.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import threading
import uuid

import python_source_inputs as inputs
from python_source_broker import BlockBroker, validate_original_phases
from python_source_channel import ReaderSocket, WorkerChannel
from python_source_layout import canonical_layout
from verify_python_source_witness import prepare_outer, save


def _docker(*arguments):
    return subprocess.check_output(["docker", *arguments])


def _terminal(container, owner, launcher, output):
    observed = launcher.inspect_container(container)
    save(output, "cleanup-observed.json", observed)
    if observed["ownerNonce"] != owner:
        raise inputs.InputRefusal("owned source container nonce changed")
    if observed["running"] or observed["status"] not in ("created", "exited"):
        save(output, "cleanup-pending.json", {"containerId": container, "observed": observed})
        return False
    removed = _docker("rm", container).decode().strip()
    save(output, "cleanup.json", {"containerId": container, "removed": removed == container})
    if removed != container:
        raise inputs.InputRefusal("owned source container removal was not observed")
    return True


def _create(*, directory, image, arguments, cwd, home, mounts, user, tmpfs, interactive=False, path=None):
    owner = uuid.uuid4().hex
    command = ["docker", "create", "--network", "none", "--read-only", "--user", user,
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
               "--label", "io.honua.development-input-owner=" + owner,
               "--workdir", cwd, "--env", "HOME=" + home, "--env", "PYTHONDONTWRITEBYTECODE=1",
               "--env", "PATH=" + (path or "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
               "--tmpfs", tmpfs]
    if interactive:
        command.append("--interactive")
    for source, target, read_only in mounts:
        command += ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if read_only else "")]
    command += [image, *arguments]
    container = subprocess.check_output(command, text=True).strip()
    if len(container) != 64 or any(letter not in "0123456789abcdef" for letter in container):
        raise inputs.InputRefusal("owned source container identity is malformed")
    save(directory, "request.json", {"command": command, "containerId": container, "ownerNonce": owner,
         "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False})
    return container, owner


def run(fetch, implementation, subject, output):
    from python_source_node import prepare as prepare_node, require_node, verify as verify_node
    prepared = prepare_outer(fetch, implementation, subject, output)
    selection, originals = prepared["selection"], prepared["originals"]
    launcher, policy = prepared["launcher"], prepared["policy"]
    layout = canonical_layout(policy)
    image = "python:3.12-bookworm@" + selection["image"]["indexDigest"]
    observed_image = inputs.read_json(_docker("image", "inspect", "--format",
        '{"Id":{{json .Id}},"RepoDigests":{{json .RepoDigests}},"Os":{{json .Os}},"Architecture":{{json .Architecture}}}', image))
    if (observed_image["Id"] not in (selection["image"]["indexDigest"], selection["image"]["configDigest"])
            or observed_image["Os"] != "linux" or observed_image["Architecture"] != "amd64"
            or "python@" + selection["image"]["indexDigest"] not in observed_image["RepoDigests"]):
        raise inputs.InputRefusal("source execution image differs from the authenticated OCI chain")
    code = Path(__file__).parent.resolve()
    node_prefix, node_receipt = prepare_node(output / "node-prerequisite", code / "sources.json")
    request_directory, evidence, wire, staging = [output / name for name in ("request", "evidence", "wire", "clone-staging")]
    for path in (request_directory, evidence, wire, staging):
        path.mkdir(mode=0o700)
    contexts, scratch_roots = [], []
    for index, declaration in enumerate(prepared["declarations"]):
        scratch = Path(tempfile.mkdtemp(prefix="honua-document-context-"))
        scratch_roots.append(scratch)
        # Private trusted setup is recorded separately from the actual clone at
        # the original shell line. It is never credited as that clone's execution.
        setup = [["git", "clone", "--no-local", "--no-hardlinks", "--no-checkout", "--", str(subject), str(scratch / "honua-sdk-python")],
                 ["git", "-C", str(scratch / "honua-sdk-python"), "checkout", "--detach", selection["subject"]["sourceSha"]],
                 ["git", "-C", str(scratch / "honua-sdk-python"), "remote", "set-url", "origin", "https://github.com/honua-io/honua-sdk-python.git"]]
        for ordinal, command in enumerate(setup):
            command = [command[0], "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *command[1:]]
            safe_environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(output / "setup-home"),
                                "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
            Path(safe_environment["HOME"]).mkdir(mode=0o700, exist_ok=True)
            actual = subprocess.run(command, env=safe_environment, capture_output=True, check=False)
            for stream in ("stdout", "stderr"):
                with (request_directory / f"setup-{index}-{ordinal}.{stream}").open("xb") as target:
                    target.write(getattr(actual, stream))
            save(request_directory, f"setup-{index}-{ordinal}.json", {"argv": command,
                "environment": safe_environment, "exitCode": actual.returncode, "sourceAdmitted": False})
            if actual.returncode:
                raise inputs.InputRefusal("trusted source authority setup failed")
        (scratch / "controller-home").mkdir(mode=0o700)
        (scratch / "environment").mkdir(mode=0o700)
        request = {"schema": "honua.python-document-source-request/v1",
                   "repository": selection["subject"]["repository"], "sourceSha": selection["subject"]["sourceSha"],
                   "document": declaration["document"], "documentSha256": declaration["documentSha256"],
                   "blocks": [{"index": row["blockIndex"], "sha256": row["blockSha256"], "invocations": row["invocations"]}
                              for row in prepared["declarations"] if row["document"] == declaration["document"]],
                   "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False}
        contexts.append({"contextIndex": index, "scratch": str(scratch), "request": request, "blockIndex": declaration["blockIndex"]})
    save(request_directory, "session.json", {"schema": "honua.python-controlled-session/v1", "contexts": contexts})
    uid, gid = os.getuid(), os.getgid()
    user = f"{uid}:{gid}"
    tmpfs = f"/tmp:rw,exec,mode=0700,uid={uid},gid={gid}"
    tool = originals / "proposal/build" / policy["executionBinding"]["implementationPackagingFilename"]
    mounts = [(code, "/controller", True), (implementation, "/implementation", True), (subject, "/subject", True),
              (originals, str(layout["originalsDirectory"]), True), (tool, str(layout["toolWheel"]), True), (request_directory, "/request", True),
              (evidence, "/evidence", False), (staging, "/clone-staging", False)]
    for scratch in scratch_roots:
        mounts += [(scratch, str(scratch), False), (scratch / "controller-home", str(scratch / "controller-home"), True),
                   (scratch / "honua-sdk-python/.git", str(scratch / "honua-sdk-python/.git"), True)]
    arguments = ["/usr/local/bin/python3.12", "-I", "/controller/python_source_worker.py"]
    controller_output = output / "controller"
    controller_output.mkdir()
    container, owner = _create(directory=controller_output, image=image, arguments=arguments, cwd="/controller",
        home=str(scratch_roots[0] / "controller-home"), mounts=mounts, user=user, tmpfs=tmpfs, interactive=True)

    def validate(identity, nonce, argv, bindings, directory, expected_status=None, *, expected_home=None, expected_path=None, expected_cwd=None):
        actual = launcher.inspect_container(identity)
        save(directory, secrets.token_hex(8) + "-observed.json", actual)
        launcher.validate_container(actual, identity, owner=nonce, image_id=observed_image["Id"],
            arguments=argv, mounts=bindings, user=user, tmpfs=tmpfs)
        network = inputs.read_json(_docker("inspect", "--format", "{{json .HostConfig.NetworkMode}}", identity))
        if network != "none" or expected_status is not None and actual["status"] != expected_status:
            raise inputs.InputRefusal("actual source container differs from its declared lifecycle or network")
        projection = inputs.read_json(_docker("inspect", "--format",
            '{"env":{{json .Config.Env}},"cwd":{{json .Config.WorkingDir}},"pidMode":{{json .HostConfig.PidMode}},'
            '"privileged":{{json .HostConfig.Privileged}},"capAdd":{{json .HostConfig.CapAdd}},'
            '"capDrop":{{json .HostConfig.CapDrop}},"securityOpt":{{json .HostConfig.SecurityOpt}},'
            '"tty":{{json .Config.Tty}}}', identity))
        save(directory, secrets.token_hex(8) + "-process-boundary.json", projection)
        environment = {}
        for row in projection["env"]:
            key, value = row.split("=", 1)
            if key in environment:
                raise inputs.InputRefusal("actual source container repeats an environment key")
            environment[key] = value
        if (projection["pidMode"] != "" or projection["privileged"] is not False or projection["capAdd"]
                or projection["capDrop"] != ["ALL"] or projection["securityOpt"] != ["no-new-privileges=true"]
                or projection["tty"] is not False or environment.get("PYTHONDONTWRITEBYTECODE") != "1"
                or expected_home is not None and environment.get("HOME") != expected_home
                or expected_path is not None and environment.get("PATH") != expected_path
                or expected_cwd is not None and projection["cwd"] != expected_cwd):
            raise inputs.InputRefusal("actual source process namespace, privilege, cwd or environment differs")
        return actual

    attached = None
    active_reader = None
    try:
        controller_contract = {"expected_home": str(scratch_roots[0] / "controller-home"), "expected_cwd": "/controller",
            "expected_path": "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
        validate(container, owner, arguments, mounts, controller_output, "created", **controller_contract)
        with (controller_output / "stderr").open("xb") as stderr:
            attached = subprocess.Popen(["docker", "start", "--attach", "--interactive", container],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)
            channel = WorkerChannel(attached, inner_sha256=selection["innerProposalSha256"], receipt_directory=evidence, wire_directory=wire)
            for index, (declaration, scratch) in enumerate(zip(prepared["declarations"], scratch_roots, strict=True)):
                phase = validate_original_phases(declaration["blockBytes"], declaration["invocations"])
                if not phase["cloneRequired"]:
                    channel.request({"contextIndex": index, "operation": "initialize"})
                bridge, state, home = [output / f"reader-{index}" / name for name in ("bridge", "state", "home")]
                for path in (bridge, state, home):
                    path.mkdir(parents=True, mode=0o700)
                shutil.copyfile(code / "python_source_bridge.py", bridge / "bridge")
                (bridge / "bridge").chmod(0o755)
                for name in ("pip", "git"):
                    (bridge / name).symlink_to("bridge")
                uses_node = require_node(declaration, selection)
                if uses_node:
                    save(bridge, "node-prerequisite.json", verify_node(node_prefix, node_receipt))
                save(bridge, "context.json", {"schema": "honua.python-controlled-block/v1", "contextIndex": index,
                    "blockSha256": declaration["blockSha256"]})
                with (bridge / "original-block.sh").open("xb") as original:
                    original.write(declaration["blockBytes"].encode())
                broker = BlockBroker(context_index=index, block_sha256=declaration["blockSha256"],
                    checkout=scratch / "honua-sdk-python", invocations=declaration["invocations"], dispatch=channel.request,
                    clone_required=phase["cloneRequired"])
                server = ReaderSocket(bridge / "request.sock", broker)
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                reader_output = output / f"reader-{index}"
                bindings = [(scratch / "honua-sdk-python", str(scratch / "honua-sdk-python"), False),
                    (scratch / "honua-sdk-python/.git", str(scratch / "honua-sdk-python/.git"), True),
                    (scratch / "environment", str(scratch / "environment"), False), (bridge, "/python-source-bridge", True),
                    (state, "/reader-state", False), (home, "/reader-home", False), (code / "python_source_reader.py", "/reader.py", True)]
                if uses_node:
                    bindings.append((node_prefix, "/opt/node", True))
                argv = ["/usr/local/bin/python3.12", "-I", "/reader.py"]
                reader_path = "/python-source-bridge:" + ("/opt/node/bin:" if uses_node else "") + str(scratch / "environment/bin") + ":/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                reader_cwd = str(scratch if phase["cloneRequired"] else scratch / "honua-sdk-python")
                reader_contract = {"expected_home": "/reader-home", "expected_path": reader_path, "expected_cwd": reader_cwd}
                reader = None
                try:
                    reader, nonce = _create(directory=reader_output, image=image, arguments=argv,
                        cwd=reader_cwd, home="/reader-home", mounts=bindings, user=user, tmpfs=tmpfs, path=reader_path)
                    active_reader = (reader, nonce, reader_output)
                    validate(reader, nonce, argv, bindings, reader_output, "created", **reader_contract)
                    with (reader_output / "stdout").open("xb") as stdout, (reader_output / "stderr").open("xb") as stderr:
                        actual = subprocess.Popen(["docker", "start", "--attach", reader], stdout=stdout, stderr=stderr)
                        reader_code = actual.wait()
                    observed = validate(reader, nonce, argv, bindings, reader_output, "exited", **reader_contract)
                    save(reader_output, "terminal.json", {"containerId": reader, "exitCode": reader_code,
                        "observedExitCode": observed["exitCode"], "stdoutSha256": inputs.sha_file(reader_output / "stdout"),
                        "stderrSha256": inputs.sha_file(reader_output / "stderr"), "sourceAdmitted": False,
                        "publishedWheelIdentity": False, "candidateQualified": False})
                    if reader_code or observed["exitCode"]:
                        raise inputs.InputRefusal("an original controlled source block failed; raw streams retained")
                    lifetime = inputs.read_json((state / "lifetime.json").read_bytes())
                    if (lifetime.get("pid") != 1 or lifetime.get("shellExitCode") != 0
                            or lifetime.get("originalBlockSha256") != declaration["blockSha256"]
                            or any(os.waitstatus_to_exitcode(row["waitStatus"]) != 0 for row in lifetime["drainedChildren"])):
                        raise inputs.InputRefusal("reader lifetime did not prove normal original-child completion")
                    if uses_node:
                        save(reader_output, "node-prerequisite-after.json", verify_node(node_prefix, node_receipt))
                    # Only now, with the reader and every namespace child actually
                    # terminal, may the trusted controller execute the shared venv.
                    broker.finish()
                finally:
                    removed = reader is None or _terminal(reader, nonce, launcher, reader_output)
                    if removed:
                        active_reader = None
                        server.shutdown()
                        thread.join()
                        server.server_close()
            attached.stdin.close()
            terminal = attached.wait()
            actual = validate(container, owner, arguments, mounts, controller_output, "exited", **controller_contract)
            if terminal or actual["exitCode"]:
                raise inputs.InputRefusal("private controller source execution refused")
            inputs.retain_originals(selection, originals, prepared["files"])
            save(output, "source-block-execution.json", {"schema": "honua.python-controlled-source-blocks/v1",
                "documentationContexts": len(prepared["declarations"]),
                "orderedPipSteps": sum(len(row["invocations"]) for row in prepared["declarations"]),
                "allThreeSourceBlockCommandsExecuted": True, "fullDocumentNonpipCommandsExecuted": False,
                "fullDocumentContinuityVerified": False, "sourceAdmitted": False,
                "publishedWheelIdentity": False, "candidateQualified": False})
    finally:
        if attached is not None and attached.poll() is None and active_reader is None:
            # A real protocol failure ends the input normally. This is not an
            # observer timeout, stop, kill, or termination of a live reader.
            if not attached.stdin.closed:
                attached.stdin.close()
            attached.wait()
        _terminal(container, owner, launcher, controller_output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("fetch", "implementation", "subject", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    run(args.fetch.resolve(), args.implementation.resolve(), args.subject.resolve(), args.output.resolve())

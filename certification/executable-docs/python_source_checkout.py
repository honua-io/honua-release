"""Explicit immutable-source clone/transposition, never a mutable-network clone claim."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import uuid


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _files(root, expected):
    """A complete finite working-tree census, including ignored stale files."""
    found, actual_directories = {}, set()
    expected_directories = {parent.as_posix() for name in expected
        for parent in PurePosixPath(name).parents if parent.as_posix() != "."}
    for directory, directories, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        if parent == root:
            directories[:] = [name for name in directories if name != ".git"]
        for name in list(directories):
            path = parent / name
            if path.is_symlink():
                directories.remove(name)
                names.append(name)
            else:
                actual_directories.add(path.relative_to(root).as_posix())
        for name in names:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                data, mode = os.fsencode(os.readlink(path)), "120000"
            elif stat.S_ISREG(info.st_mode):
                data = path.read_bytes()
                mode = "100755" if info.st_mode & 0o111 else "100644"
            else:
                raise ValueError("clone working tree contains a special entry")
            found[relative] = {"mode": mode, "sha256": _digest(data),
                "gitBlob": hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()}
    if set(found) != set(expected) or actual_directories != expected_directories:
        raise ValueError("clone working tree has missing, extra or stale inputs")
    for name, observed in found.items():
        if any(observed[key] != expected[name][key] for key in observed):
            raise ValueError("clone working-tree input differs from selected blob")
    return found


def _copy(stage, target, expected):
    """Use directory descriptors and never follow a document-writable ancestor."""
    root_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name, binding in expected.items():
            parts = PurePosixPath(name).parts
            if not parts or name.startswith("/") or any(part in ("", ".", "..", ".git") for part in parts):
                raise ValueError("selected source path escapes its finite working tree")
            parent = os.dup(root_fd)
            temporary = ".controlled-clone-" + uuid.uuid4().hex
            try:
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    os.close(parent)
                    parent = child
                if binding["mode"] == "120000":
                    link = os.readlink(stage / name)
                    if name != "CLAUDE.md" or link != "AGENTS.md":
                        raise ValueError("clone symlink differs from canonical source exception")
                    os.symlink(link, temporary, dir_fd=parent)
                else:
                    data = (stage / name).read_bytes()
                    if _digest(data) != binding["sha256"]:
                        raise ValueError("actual clone bytes changed before transposition")
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o755 if binding["mode"] == "100755" else 0o644, dir_fd=parent)
                    with os.fdopen(fd, "wb") as output:
                        output.write(data)
                        os.fchmod(output.fileno(), 0o755 if binding["mode"] == "100755" else 0o644)
                os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
                os.close(parent)
    finally:
        os.close(root_fd)


def clone_and_transpose(*, engine, subject, target, staging_parent, source,
                        original_argv, original_cwd, reader_destination, authority_check, metadata_snapshot):
    """The trusted controller owns staging; readers can access only target.

    authority_check must establish the original controller HOME/cwd and subject
    Git authority before EVERY Git invocation. The full original source verifier
    is retained, including its repository, commit, blob and package checks.
    """
    if original_argv != ["clone", "https://github.com/honua-io/honua-sdk-python.git"]:
        raise ValueError("clone request differs from the original finite command")
    if (not Path(original_cwd).is_absolute()
            or Path(original_cwd) / "honua-sdk-python" != Path(reader_destination)):
        raise ValueError("clone request changed its original destination")
    if not staging_parent.is_absolute() or staging_parent.resolve() != staging_parent:
        raise ValueError("clone staging path is not a private literal directory")
    authority_check()
    before = metadata_snapshot(target / ".git")
    _files(target, source["files"])
    if engine.source(subject, source["sourceSha"]) != source:
        raise ValueError("clone input differs from admitted immutable source")
    staging_parent.mkdir(mode=0o700, exist_ok=False)
    stage = staging_parent / "honua-sdk-python"
    commands = []

    def run(argv, cwd):
        authority_check()
        environment = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "SYSTEMROOT") if key in os.environ}
        environment.update(PIP_CONFIG_FILE=os.devnull, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(argv, cwd=cwd, env=environment, capture_output=True, check=False)
        record = {"argv": argv, "cwd": str(cwd), "exitCode": result.returncode,
            "stdoutBase64": base64.b64encode(result.stdout).decode(), "stdoutSha256": _digest(result.stdout),
            "stderrBase64": base64.b64encode(result.stderr).decode(), "stderrSha256": _digest(result.stderr)}
        prefix = f"command-{len(commands):03d}"
        # Persist originals before any later tree/census/copy validation can
        # fail. This directory is a controller-only retained host bind.
        for stream in ("stdout", "stderr"):
            with (staging_parent / (prefix + "." + stream)).open("xb") as output:
                output.write(getattr(result, stream))
        with (staging_parent / (prefix + ".json")).open("x") as output:
            json.dump(record, output, sort_keys=True)
        commands.append(record)
        if result.returncode:
            error = subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
            error.clone_receipt = {"commands": commands, "sourceAdmitted": False,
                "publishedWheelIdentity": False, "candidateQualified": False}
            raise error
        return result.stdout

    run(["git", "clone", "--no-local", "--no-hardlinks", "--no-checkout", "--", str(subject), str(stage)], staging_parent)
    run(["git", "-C", str(stage), "checkout", "--detach", source["sourceSha"]], staging_parent)
    # The original verifier requires the canonical repository origin. This is a
    # recorded metadata assignment, not a network request or an origin inference.
    run(["git", "-C", str(stage), "remote", "set-url", "origin",
         "https://github.com/honua-io/honua-sdk-python.git"], staging_parent)
    authority_check()
    actual = engine.source(stage, source["sourceSha"])
    if actual != source:
        raise ValueError("actual clone differs from the complete selected source")
    tree = run(["git", "-C", str(stage), "rev-parse", "HEAD^{tree}"], staging_parent).decode().strip()
    subject_tree = run(["git", "-C", str(subject), "rev-parse", "HEAD^{tree}"], staging_parent).decode().strip()
    target_tree = run(["git", "-C", str(target), "rev-parse", "HEAD^{tree}"], staging_parent).decode().strip()
    if tree != subject_tree or tree != target_tree:
        raise ValueError("clone, immutable subject and target metadata trees differ")
    _files(stage, source["files"])
    # Repeat the destination census immediately before writing, then replace
    # tracked paths only. Never copy or overwrite its separately RO Git metadata.
    _files(target, source["files"])
    _copy(stage, target, source["files"])
    observed = _files(target, source["files"])
    authority_check()
    if metadata_snapshot(target / ".git") != before or engine.source(target, source["sourceSha"]) != source:
        raise ValueError("transposition changed Git authority or selected source")
    return {"schema": "honua.python-immutable-clone-transposition/v1",
        "originalArgv": original_argv, "originalCwd": str(original_cwd), "originalDestination": str(reader_destination),
        "actualControllerDestination": str(target),
        "commands": commands, "sourceSha": source["sourceSha"], "selectedTree": tree,
        "subjectTree": subject_tree, "targetMetadataTree": target_tree,
        "workingTree": observed, "gitAuthorityBefore": before, "gitAuthorityAfter": metadata_snapshot(target / ".git"),
        "mutableNetworkCloneEquivalent": False, "sourceAdmitted": False,
        "publishedWheelIdentity": False, "candidateQualified": False}

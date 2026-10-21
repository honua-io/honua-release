"""Execute a documented source sequence using an explicitly controlled recipe.

The caller must first authenticate the protected producer witness and run its
full canonical inner verifier in this runtime. This controller does not turn a
producer proposal into admission: each actual environment is inventoried and
checked against source-built and registry wheel bytes before it can be used.

This is separate from default plain-pip execution and the default PEP610 refusal.
It never asserts independently published-wheel identity or candidate qualification.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid


class ExecutionRefusal(ValueError):
    pass


IMAGE_PATH = "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CONTROLLER_ROOT = Path("/controller")


def require_read_only_path(path: Path) -> None:
    """Require an actual read-only mount, including every nested mount."""
    if path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise ExecutionRefusal("trusted input is not its literal mounted directory")
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) < 7 or "-" not in fields:
            raise ExecutionRefusal("trusted mount authority is unreadable")
        mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
        mounts.append((mount, set(fields[5].split(","))))
    covering = [(mount, options) for mount, options in mounts
                if path == mount or mount in path.parents]
    if not covering:
        raise ExecutionRefusal("trusted input lacks read-only mount authority")
    deepest = max(len(mount.parts) for mount, _ in covering)
    if any("ro" not in options for mount, options in covering if len(mount.parts) == deepest):
        raise ExecutionRefusal("trusted input lacks read-only mount authority")
    if any("ro" not in options for mount, options in mounts if path in mount.parents):
        raise ExecutionRefusal("trusted input contains a writable nested mount")


def require_git_authority(root: Path) -> None:
    """Check before unmodified producer Git calls can read config or hooks."""
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise ExecutionRefusal("source checkout is not its literal owned directory")
    require_read_only_path(root / ".git")


def require_controller_process(scratch: Path) -> None:
    """Keep normal user-site installs and document shims out of the verifier."""
    home = scratch / "controller-home"
    if (sys.flags.isolated != 1 or os.environ.get("PATH") != IMAGE_PATH
            or os.environ.get("HOME") != str(home) or home.is_symlink() or not home.is_dir()
            or Path.cwd() != CONTROLLER_ROOT):
        raise ExecutionRefusal("controlled verifier lacks its isolated original-image path and private home")
    require_read_only_path(CONTROLLER_ROOT)
    require_read_only_path(home)
    if any(home.iterdir()):
        raise ExecutionRefusal("controlled verifier home contains undeclared configuration")


def venv_snapshot(root: Path) -> dict:
    """Inspect bytes with the trusted controller; never execute the target venv."""
    if root.is_symlink() or not root.is_dir():
        raise ExecutionRefusal("controlled environment root is not an owned directory")
    rows = {}
    for directory, names, files in os.walk(root, followlinks=False):
        for name in sorted(names + files):
            path = Path(directory) / name
            before = path.lstat()
            relative = path.relative_to(root).as_posix()
            row = {"mode": stat.S_IMODE(before.st_mode)}
            if stat.S_ISLNK(before.st_mode):
                row.update(kind="symlink", target=os.readlink(path))
            elif stat.S_ISDIR(before.st_mode):
                row.update(kind="directory")
            elif stat.S_ISREG(before.st_mode):
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                row.update(kind="file", bytes=before.st_size, sha256=digest.hexdigest())
            else:
                raise ExecutionRefusal("controlled environment contains a special entry")
            after = path.lstat()
            if (before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns):
                raise ExecutionRefusal("controlled environment changed while inspecting its bytes")
            rows[relative] = row
    return rows


def require_new_environment(path: Path) -> None:
    # Docker bind sources must exist before launch. A literal empty directory
    # is still new; a populated, linked or non-directory target is never reused.
    if path.is_symlink() or path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ExecutionRefusal("controlled source environment was not newly prepared and empty")


class ControlledSequence:
    def __init__(self, *, engine, documents, proposal: dict, policy: dict, subject: Path,
                 original_directory: Path, scratch: Path, context_index: int,
                 admitted_revision: str, request: dict, block_index: int):
        self.engine, self.documents, self.proposal = engine, documents, proposal
        self.policy = policy
        if (not isinstance(admitted_revision, str) or re.fullmatch(r"[a-f0-9]{40}", admitted_revision) is None
                or proposal["source"]["sourceSha"] != admitted_revision
                or request.get("sourceSha") != admitted_revision
                or request.get("repository") != "honua-io/honua-sdk-python"
                or request.get("schema") != "honua.python-document-source-request/v1"
                or any(request.get(key) is not False for key in ("sourceAdmitted", "publishedWheelIdentity", "candidateQualified"))):
            raise ExecutionRefusal("controlled source input is not the actual admitted document revision")
        self.subject, self.original, self.scratch = subject, original_directory, scratch
        engine.scratch_identity(str(scratch), "honua-document-context-")
        require_controller_process(scratch)
        contexts = proposal["documentationSequences"]
        if type(context_index) is not int or context_index < 0 or context_index >= len(contexts):
            raise ExecutionRefusal("controlled source context is outside the original finite declaration")
        self.recorded = contexts[context_index]
        requested = [block for block in request["blocks"] if block["index"] == block_index]
        if (len(requested) != 1 or self.recorded["document"] != request["document"]
                or self.recorded["documentSha256"] != request["documentSha256"]
                or self.recorded["blockIndex"] != block_index
                or self.recorded["blockSha256"] != requested[0]["sha256"]
                or self.recorded["invocations"] != requested[0]["invocations"]):
            raise ExecutionRefusal("controlled source proof does not cover the actual original block and invocation order")
        self.runtime_unchanged()
        self.public = original_directory / f"document-{context_index}"
        self.checkout, self.environment = scratch / "honua-sdk-python", scratch / "environment"
        self.python = self.environment / "bin/python"
        self.active_warehouse = scratch / "active-wheels"
        self.source = proposal["source"]
        require_git_authority(subject)
        declarations = documents.plan(engine, subject, self.source)
        if any(self.recorded.get(key) != value for key, value in declarations[context_index].items()):
            raise ExecutionRefusal("controlled source declaration differs from exact document command bytes")
        if engine.source(subject, self.source["sourceSha"]) != self.source:
            raise ExecutionRefusal("actual subject checkout differs from the admitted immutable source")
        self.backend = (self.recorded["dynamicBackend"]["artifacts"] if self.recorded["dynamicBackend"]
                        else proposal["build"]["artifacts"])
        self.expected_context = {**self.recorded, "scratchRoot": str(scratch), "editableWheels": []}
        # This stays in the trusted controller/runner, never in a document-writable
        # state file. A later process must receive it from that trusted caller.
        self._trusted_venv = None
        self._next_ordinal = 0

    def runtime_unchanged(self):
        require_controller_process(self.scratch)
        if (self.engine.runtime(self.policy) != self.proposal["runtime"]
                or self.engine.default_environment() != self.proposal["build"]["environment"]):
            raise ExecutionRefusal("controlled source runtime, tools or full marker environment differs")

    def source_unchanged(self):
        self.runtime_unchanged()
        require_git_authority(self.subject)
        require_git_authority(self.checkout)
        if self.engine.source(self.subject, self.source["sourceSha"]) != self.source:
            raise ExecutionRefusal("immutable subject changed during controlled source execution")
        if self.engine.source(self.checkout, self.source["sourceSha"]) != self.source:
            raise ExecutionRefusal("actual documented checkout changed during controlled source execution")
        self.documents.source_hashes(self.engine, self.source, self.checkout)

    def clone(self, argv, cwd):
        from python_source_checkout import clone_and_transpose

        if cwd != str(self.scratch):
            raise ExecutionRefusal("actual clone request changed its original working directory")

        def authority():
            self.runtime_unchanged()
            require_git_authority(self.subject)
            require_git_authority(self.checkout)

        # This mount is controller-only. It is never exposed through the
        # reader's source/environment mounts or its bridge socket directory.
        staging = Path("/clone-staging") / uuid.uuid4().hex
        return clone_and_transpose(engine=self.engine, subject=self.subject, target=self.checkout,
            staging_parent=staging, source=self.source, original_argv=argv, original_cwd=cwd,
            reader_destination=self.checkout, authority_check=authority, metadata_snapshot=venv_snapshot)

    def warehouse(self, artifacts: list[dict], target: Path) -> None:
        """Expose only this step's closed inputs, never later sequence versions."""
        target.mkdir()
        if len({item["filename"] for item in artifacts}) != len(artifacts):
            raise ExecutionRefusal("controlled source wheel names collide")
        for item in artifacts:
            name = item["filename"]
            if Path(name).name != name or not name.endswith(".whl"):
                raise ExecutionRefusal("controlled source wheel escapes the finite input directory")
            candidates = [self.public / "wheels" / name, self.original / "build" / name,
                          self.public / "backend-dynamic" / name]
            matches = [path for path in candidates if path.is_file() and not path.is_symlink()
                       and hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]]
            if not matches:
                raise ExecutionRefusal("controlled source wheel input bytes are absent or changed")
            shutil.copyfile(matches[0], target / name)
        self.verify_warehouse(artifacts, target)

    @staticmethod
    def verify_warehouse(artifacts: list[dict], target: Path) -> None:
        if (target.is_symlink() or not target.is_dir()
                or set(path.name for path in target.iterdir()) != {item["filename"] for item in artifacts}):
            raise ExecutionRefusal("controlled source wheel census differs")
        for item in artifacts:
            path = target / item["filename"]
            if (path.is_symlink() or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]):
                raise ExecutionRefusal("controlled source wheel bytes changed")

    def initialize(self):
        # The trusted caller creates the checkout before container launch and
        # mounts its .git read-only. The working tree remains writable for the
        # original developer commands; the producer still checks source bytes.
        if not self.checkout.is_dir():
            raise ExecutionRefusal("controlled source context lacks its prepared checkout or new environment")
        require_new_environment(self.environment)
        self.source_unchanged()
        self.engine.run([sys.executable, "-m", "venv", "--without-pip", str(self.environment)])
        warehouse = self.scratch / "bootstrap-wheels"
        self.warehouse(self.backend, warehouse)
        lock = self.scratch / "bootstrap.lock"
        lock.write_text("\n".join(f'{item["name"]}=={item["version"]} --hash=sha256:{item["sha256"]}' for item in self.backend) + "\n")
        command = [sys.executable, "-m", "pip", "--isolated", "--python", str(self.python), "install", "--no-index",
                   "--find-links", str(warehouse), "--require-hashes", "-r", str(lock)]
        bootstrap_command = command
        self.engine.run(command)
        self.source_unchanged()
        observed = self.documents.inventory(self.engine, self.python)
        if {name: row["version"] for name, row in observed.items()} != {item["name"]: item["version"] for item in self.backend}:
            raise ExecutionRefusal("actual controlled backend closure differs")
        # Editable wheels must be built for this actual checkout; an original
        # wheel's .pth path cannot be rewritten and relabelled as the same bytes.
        editable = self.scratch / "editable-wheels"
        editable.mkdir()
        for name in self.source["packages"]:
            command = [str(self.python), "-c", "import hatchling.build; print(hatchling.build.build_editable(" + repr(str(editable)) + "))"]
            self.engine.run(command, cwd=self.checkout / "packages" / name)
            candidates = list(editable.glob(name.replace("-", "_") + "-*.whl"))
            if len(candidates) != 1:
                raise ExecutionRefusal("actual controlled editable build did not produce one wheel")
            wheel = candidates[0]
            data = wheel.read_bytes()
            actual_name, version, dependencies = self.engine.wheel_metadata(data, wheel.name)
            if actual_name != name or version != self.source["packages"][name]["metadata"]["version"]:
                raise ExecutionRefusal("actual controlled editable source identity differs")
            self.expected_context["editableWheels"].append({"name": name, "version": version, "filename": wheel.name,
                "sha256": self.engine.digest(data), "requiresDist": dependencies, "sourceSha": self.source["sourceSha"],
                "publishedIdentity": False})
        self.source_unchanged()
        self.audit(-1, _trusted_install_transition=True)
        return {"bootstrapCommand": bootstrap_command, "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False}

    def expected(self, ordinal: int):
        if type(ordinal) is not int or ordinal < -1 or ordinal >= len(self.recorded["steps"]):
            raise ExecutionRefusal("controlled source step is outside the original sequence")
        active = self.backend if ordinal == -1 else self.recorded["steps"][ordinal]["activeArtifacts"]
        origins = {}
        for step in self.recorded["steps"][:ordinal + 1]:
            origins.update(self.documents.expected_sources(self.source, step, self.checkout))
        return active, origins

    def audit(self, ordinal: int, *, _trusted_install_transition: bool = False):
        expected_ordinal = self._next_ordinal if _trusted_install_transition and ordinal >= 0 else self._next_ordinal - 1
        if ordinal != expected_ordinal:
            raise ExecutionRefusal("controlled source audit is not the actual completed sequence position")
        if not _trusted_install_transition:
            if self._trusted_venv is None or venv_snapshot(self.environment) != self._trusted_venv:
                raise ExecutionRefusal("controlled environment changed before any interpreter execution")
        self.source_unchanged()
        active, origins = self.expected(ordinal)
        observed = self.documents.inventory(self.engine, self.python)
        expected_versions = {item["name"]: item["version"] for item in active}
        expected_versions.update({name: value["version"] for name, value in origins.items()})
        if {name: item["version"] for name, item in observed.items()} != expected_versions:
            raise ExecutionRefusal("actual controlled installed closure differs")
        for name, origin in origins.items():
            if not self.documents.direct_matches(observed[name]["directUrl"], origin):
                raise ExecutionRefusal("actual controlled source origin differs")
        if any(observed[item["name"]]["directUrl"] is not None for item in active):
            raise ExecutionRefusal("controlled registry dependency has an unadmitted direct origin")
        warehouse = self.active_warehouse
        if not warehouse.exists():
            self.warehouse(active, warehouse)
        self.verify_warehouse(active, warehouse)
        for item in self.expected_context["editableWheels"]:
            path = self.scratch / "editable-wheels" / item["filename"]
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise ExecutionRefusal("actual controlled editable build bytes changed")
        self.documents.installed_source_and_artifact_bytes(self.engine, after=observed, active=active, warehouse=warehouse,
            origins=origins, source_build=self.proposal["sourceBuild"], destination=self.original,
            context=self.expected_context, public=self.scratch)
        self.source_unchanged()
        self._trusted_venv = venv_snapshot(self.environment)
        return observed

    def install(self, ordinal: int):
        if type(ordinal) is not int or ordinal < 0 or ordinal >= len(self.recorded["steps"]):
            raise ExecutionRefusal("controlled install step is outside the original sequence")
        if ordinal != self._next_ordinal:
            raise ExecutionRefusal("controlled source invocation is missing, repeated or reordered")
        before = self.audit(ordinal - 1)
        invocation = self.recorded["steps"][ordinal]
        active, _ = self.expected(ordinal)
        warehouse = self.active_warehouse
        previous_active, _ = self.expected(ordinal - 1)
        self.verify_warehouse(previous_active, warehouse)
        # Keep only one exact active closure per context. This does not expose a
        # final-version superset to earlier steps or replicate large wheels for
        # each audit; originals remain separately retained and read-only.
        for path in warehouse.iterdir():
            path.unlink()
        warehouse.rmdir()
        self.warehouse(active, warehouse)
        required = self.documents.roots(self.source, self.recorded["invocations"][:ordinal + 1],
                                        [f'{item["name"]}=={item["version"]}' for item in self.backend])
        if required != invocation["requiredClosure"]:
            raise ExecutionRefusal("controlled cumulative source requirements differ")
        self.engine.validate_closure(active, required, warehouse)
        report = self.scratch / f"actual-install-{ordinal}.json"
        command = self.documents.step_command(self.python, self.checkout, invocation, report, offline=warehouse)
        stdout = self.engine.run(command, cwd=self.checkout)
        after = self.audit(ordinal, _trusted_install_transition=True)
        self._next_ordinal += 1
        return {"originalCommand": invocation["command"], "originalCommandSha256": invocation["commandSha256"],
                "derivedInstallCommand": command, "sourceSha": self.source["sourceSha"],
                "blockIndex": self.recorded["blockIndex"], "blockSha256": self.recorded["blockSha256"],
                "ordinal": ordinal, "before": before, "after": after,
                "stdoutBase64": base64.b64encode(stdout).decode("ascii"),
                "stdoutSha256": hashlib.sha256(stdout).hexdigest(),
                # The unchanged canonical run() retains stderr only on error.
                # Do not fabricate a successful empty-stderr observation.
                "successfulStderrCaptured": False,
                "actualReportSha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False}

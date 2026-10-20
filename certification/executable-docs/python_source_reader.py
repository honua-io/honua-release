"""One-shot reader init: execute the intact original block and drain its real children.

This process owns no controller state. A private PID namespace separates it from
the trusted source verifier. It never stops a background child to obtain a clean
postcheck; the container remains live until its original children are terminal.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


def main():
    if os.getpid() != 1:
        raise ValueError("controlled reader must be the actual private namespace init")
    declaration = json.loads(Path("/python-source-bridge/context.json").read_bytes())
    block = Path("/python-source-bridge/original-block.sh")
    if hashlib.sha256(block.read_bytes()).hexdigest() != declaration["blockSha256"]:
        raise ValueError("reader original block bytes changed")
    # These files are untrusted session observations. They never become source
    # admission receipts or private controller state.
    state = Path("/reader-state")
    cwd_file, environment_file = state / "cwd", state / "environment"
    wrapper = ("trap 'rc=$?; pwd > " + shlex.quote(str(cwd_file))
        + "; env -0 > " + shlex.quote(str(environment_file)) + "; exit $rc' EXIT\n"
        + "set -a -e\n. " + shlex.quote(str(block)) + "\n")
    shell = subprocess.Popen(["bash", "-c", wrapper, "docrun"], stdin=subprocess.DEVNULL)
    shell_code = shell.wait()
    drained = []
    # As the actual namespace init, this process receives orphaned descendants.
    # No observer deadline, stop, kill, or fabricated child-completion claim.
    while True:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            break
        drained.append({"pid": pid, "waitStatus": status})
    result = {"schema": "honua.python-reader-lifetime/v1", "pid": os.getpid(),
        "shellExitCode": shell_code, "drainedChildren": drained,
        "originalBlockSha256": declaration["blockSha256"],
        "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False}
    with (state / "lifetime.json").open("x") as output:
        json.dump(result, output, sort_keys=True)
    if shell_code:
        return shell_code if 0 < shell_code < 256 else 1
    return 1 if any(os.waitstatus_to_exitcode(row["waitStatus"]) != 0 for row in drained) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("controlled reader refused; retain original streams and container state", file=sys.stderr)
        raise SystemExit(1) from None

"""Small REAL producer API join; no proposal parse, install, or source admission."""
from __future__ import annotations

import json
import os
from pathlib import Path
import base64
import hashlib
import subprocess
import sys


def main():
    sys.dont_write_bytecode = True
    sys.path.insert(0, "/controller")
    import python_source_inputs as inputs
    from python_source_execution import require_git_authority, require_read_only_path
    from python_source_layout import canonical_layout

    require_read_only_path(Path("/controller"))
    require_git_authority(Path("/implementation"))
    require_read_only_path(Path("/runtime-home"))
    if os.environ.get("HOME") != "/runtime-home" or any(Path("/runtime-home").iterdir()):
        raise ValueError("runtime probe home is not the original empty read-only boundary")
    policy = inputs.read_json(Path("/implementation/scripts/development-inputs/governed-runtime.json").read_bytes())
    layout = canonical_layout(policy)
    negative_alias = sys.argv[1:] == ["--negative-wheel-alias"]
    if sys.argv[1:] and not negative_alias:
        raise ValueError("runtime probe received an undeclared control")
    tool = Path("/legacy-tool.whl") if negative_alias else layout["toolWheel"]
    if inputs.sha_file(tool) != policy["executionBinding"]["implementationPackagingSha256"]:
        raise ValueError("runtime probe implementation tool bytes differ")
    sys.path[:0] = [str(tool), "/implementation/scripts"]
    import development_source_inputs as engine
    import run_governed_source_inputs as launcher

    # This invokes the unchanged REAL implementation, including executable/pip
    # bytes, the tool-wheel basename, and actual original child-tool separation.
    try:
        runtime = engine.runtime(policy)
    except ValueError as error:
        if negative_alias and str(error) == "implementation packaging is not the reviewed standalone wheel":
            return {"negativeControl": "wrong-tool-wheel-basename", "refusedByActualRuntime": True,
                "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False}
        raise
    if negative_alias:
        raise ValueError("actual canonical runtime accepted the wrong tool-wheel basename")
    request = inputs.read_json(Path("/probe-request/context.json").read_bytes())
    selection, _ = inputs.selected_inputs()
    arguments, _, _, _ = launcher.action_inputs("verify", selection["subject"]["sourceSha"], request, policy)
    actual_destination = Path(arguments[arguments.index("--destination") + 1])
    actual_tool = Path(arguments[arguments.index("--implementation-packaging-wheel") + 1])
    if layout["proposalDirectory"] != actual_destination or layout["toolWheel"] != actual_tool:
        raise ValueError("consumer paths differ from the actual protected action contract")
    scratch = Path("/tmp/honua-source-build-abcdefgh")
    install, build = engine.build_commands(scratch, layout["proposalDirectory"])
    expected_install, expected_build = engine.build_commands(scratch, actual_destination)
    if install != expected_install or build != expected_build:
        raise ValueError("consumer build inputs differ from the actual protected producer commands")
    legacy_install, legacy_build = engine.build_commands(scratch, Path("/original/proposal"))
    if legacy_install == expected_install or legacy_build == expected_build:
        raise ValueError("legacy proposal alias unexpectedly reproduced the original bound commands")
    negative = subprocess.run([sys.executable, "-I", __file__, "--negative-wheel-alias"], capture_output=True, check=False)
    if negative.returncode:
        sys.stderr.buffer.write(negative.stdout + negative.stderr)
        raise ValueError("real runtime basename negative did not reach the expected refusal")
    negative_result = json.loads(negative.stdout)
    if negative_result.get("refusedByActualRuntime") is not True:
        raise ValueError("real runtime basename negative omitted the actual refusal")
    return {"schema": "honua.python-source-runtime-layout-control/v1", "runtime": runtime,
        "canonicalActionArguments": arguments, "canonicalBuildCommands": [install, build],
        "wrongProposalAliasCommands": [legacy_install, legacy_build], "wrongProposalAliasExactJoinRefused": True,
        "wrongWheelBasenameControl": {"exitCode": negative.returncode,
            "stdoutBase64": base64.b64encode(negative.stdout).decode(), "stdoutSha256": hashlib.sha256(negative.stdout).hexdigest(),
            "stderrBase64": base64.b64encode(negative.stderr).decode(), "stderrSha256": hashlib.sha256(negative.stderr).hexdigest()},
        "contractContextOnly": True, "fullProposalVerified": False,
        "fullMarkerEnvironmentComparedToProposal": False, "originalInstallCommandsExecuted": False,
        "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False}


if __name__ == "__main__":
    print(json.dumps(main(), sort_keys=True))

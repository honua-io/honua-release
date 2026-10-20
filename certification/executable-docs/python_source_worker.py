"""Private original-runtime controller. No document code or arbitrary commands enter this protocol.

The trusted caller authenticates the protected implementation and original input
archive before mounting them read-only. Document containers share only the
required source and environment subdirectories, with their Git authority also
read-only. This worker never grants a default PEP610 exception.
"""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import sys


def canonical_worker():
    sys.dont_write_bytecode = True
    sys.path.insert(0, "/controller")
    import python_source_build as planning
    import python_source_inputs as inputs
    from python_source_execution import ControlledSequence, ExecutionRefusal, require_controller_process, require_git_authority, require_read_only_path
    from python_source_protocol import SequenceProtocol
    from python_source_broker import validate_original_phases
    from python_source_layout import canonical_layout

    require_read_only_path(Path("/request"))
    request_file = Path("/request/session.json")
    if request_file.is_symlink() or request_file.stat().st_size > 1024 * 1024:
        raise ExecutionRefusal("controlled session request escapes its finite input")
    session = inputs.read_json(request_file.read_bytes())
    if (set(session) != {"schema", "contexts"} or session["schema"] != "honua.python-controlled-session/v1"
            or not isinstance(session["contexts"], list) or len(session["contexts"]) != 3):
        raise ExecutionRefusal("controlled session does not cover the three original contexts")
    contexts = {}
    for context in session["contexts"]:
        if (not isinstance(context, dict) or set(context) != {"contextIndex", "scratch", "request", "blockIndex"}
                or type(context["contextIndex"]) is not int or context["contextIndex"] in contexts):
            raise ExecutionRefusal("controlled session context is not finite and unique")
        contexts[context["contextIndex"]] = context
    if set(contexts) != {0, 1, 2}:
        raise ExecutionRefusal("controlled session omitted an original context")
    os.environ["HOME"] = str(Path(contexts[0]["scratch"]) / "controller-home")
    require_controller_process(Path(contexts[0]["scratch"]))
    implementation, subject = Path("/implementation"), Path("/subject")
    require_git_authority(implementation)
    require_git_authority(subject)
    policy = inputs.read_json((implementation / "scripts/development-inputs/governed-runtime.json").read_bytes())
    layout = canonical_layout(policy)
    require_read_only_path(layout["originalsDirectory"])
    original, tool = layout["proposalDirectory"], layout["toolWheel"]
    if tool.is_symlink() or inputs.sha_file(tool) != policy["executionBinding"]["implementationPackagingSha256"]:
        raise ExecutionRefusal("controlled implementation tool bytes differ")
    sys.path[:0] = [str(tool), str(implementation / "scripts")]
    import development_source_inputs as engine
    import python_document_inputs as documents

    selection, _ = inputs.selected_inputs()
    proposal_file = original / "proposal.json"
    if proposal_file.is_symlink() or inputs.sha_file(proposal_file) != selection["innerProposalSha256"]:
        raise ExecutionRefusal("controlled original proposal bytes differ")
    proposal = inputs.read_json(proposal_file.read_bytes())
    # This is the actual original verifier and full marker/runtime equality.
    # A local venue mismatch remains a refusal; no normalization is performed.
    engine.verify_proposal(proposal, subject, selection["subject"]["sourceSha"], original, policy)
    phases = [validate_original_phases(row["blockBytes"], row["invocations"])
              for row in proposal["documentationSequences"]]
    for index, context in contexts.items():
        scratch = engine.scratch_identity(context["scratch"], "honua-document-context-")
        require_git_authority(scratch / "honua-sdk-python")
        require_read_only_path(scratch / "controller-home")
        selected = selection["documentation"][index]
        request = context["request"]
        document = {"repo": selection["subject"]["repository"], "path": selected["document"],
                    "pythonSourceBuild": {"blocks": [{"index": row["index"], "sha256": row["sha256"]}
                        for row in selection["documentation"] if row["document"] == selected["document"]]}}
        actual = planning.derive(document, selection["subject"]["sourceSha"],
                                 (subject / selected["document"]).read_text())
        if request != actual or context["blockIndex"] != selected["index"]:
            raise ExecutionRefusal("controlled session differs from actual original documentation")

    protocols = {}
    serial = 0
    print(json.dumps({"ready": True, "canonicalInnerVerified": True,
                      "controlledSourcePhases": phases,
                      "innerProposalSha256": selection["innerProposalSha256"],
                      "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False}), flush=True)
    while line := sys.stdin.buffer.readline(64 * 1024 + 1):
        if len(line) > 64 * 1024:
            raise ExecutionRefusal("controlled operation exceeds its finite request bound")
        request = inputs.read_json(line)
        if not isinstance(request, dict) or type(request.get("contextIndex")) is not int:
            raise ExecutionRefusal("controlled operation lacks its original context")
        index = request.pop("contextIndex")
        if index not in contexts:
            raise ExecutionRefusal("controlled operation targets a foreign context")
        context = contexts[index]
        scratch = Path(context["scratch"])
        os.environ["HOME"] = str(scratch / "controller-home")
        if index not in protocols:
            if request.get("operation") not in {"initialize", "clone"}:
                raise ExecutionRefusal("controlled context has not initialized")
            sequence = ControlledSequence(engine=engine, documents=documents, proposal=proposal, policy=policy,
                subject=subject, original_directory=original, scratch=scratch, context_index=index,
                admitted_revision=selection["subject"]["sourceSha"], request=context["request"],
                block_index=context["blockIndex"])
            protocols[index] = SequenceProtocol(sequence)
        result = protocols[index].request(request)
        # Full receipts stay in controller-only evidence, not in document-writable state.
        # Compression avoids duplicating the large finite installed-file inventories on the wire.
        evidence = Path("/evidence") / f"{serial:04d}-context-{index}.json.gz"
        with evidence.open("xb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as zipped:
                for chunk in json.JSONEncoder(sort_keys=True).iterencode({"request": request, "result": result}):
                    zipped.write(chunk.encode())
        response = {"contextIndex": index, "operation": request["operation"], "serial": serial,
                          "receipt": evidence.name, "receiptSha256": inputs.sha_file(evidence),
                          "completedInvocations": len(protocols[index].receipts),
                          "sourceAdmitted": False, "publishedWheelIdentity": False,
                          "candidateQualified": False}
        if request["operation"] == "install":
            response.update({key: result[key] for key in
                ("stdoutBase64", "stdoutSha256", "successfulStderrCaptured")})
        if request["operation"] == "clone":
            import base64
            for stream in ("stdout", "stderr"):
                data = b"".join(base64.b64decode(row[stream + "Base64"], validate=True) for row in result["commands"])
                response[stream + "Base64"] = base64.b64encode(data).decode()
                response[stream + "Sha256"] = engine.digest(data)
            response["mutableNetworkCloneEquivalent"] = False
        print(json.dumps(response), flush=True)
        serial += 1
    if set(protocols) != {0, 1, 2} or any(not protocol.finished for protocol in protocols.values()):
        raise ExecutionRefusal("controlled session ended before every original context finished")


if __name__ == "__main__":
    try:
        canonical_worker()
    except Exception as error:
        if isinstance(getattr(error, "clone_receipt", None), dict):
            with (Path("/evidence") / "failure-clone.json.gz").open("xb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as zipped:
                    zipped.write(json.dumps(error.clone_receipt, sort_keys=True).encode())
        for name, value in [("failure.stdout", getattr(error, "output", None)),
                            ("failure.stderr", getattr(error, "stderr", None))]:
            if isinstance(value, bytes):
                with (Path("/evidence") / name).open("xb") as output:
                    output.write(value)
        # Input URLs and package contents are not copied into diagnostics.
        print(json.dumps({"refused": True, "errorType": type(error).__name__,
                          "sourceAdmitted": False, "publishedWheelIdentity": False,
                          "candidateQualified": False}), flush=True)
        raise SystemExit(1) from None

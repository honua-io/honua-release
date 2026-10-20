#!/usr/local/bin/python3.12 -I
"""Reader-side bridge for the explicitly governed recipe, not a general installer.

The trusted caller mounts this file, its block declaration, and the owned Unix
socket read-only. The controller independently validates the original context,
block and next argv; this process cannot supply arbitrary code or proof state.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sys


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("controlled bridge received duplicate fields")
        result[key] = value
    return result


def main() -> int:
    declaration = Path("/python-source-bridge/context.json")
    if declaration.stat().st_size > 64 * 1024:
        raise ValueError("controlled bridge declaration exceeds its bound")
    context = json.loads(declaration.read_bytes(), object_pairs_hook=unique_object)
    if (set(context) != {"schema", "contextIndex", "blockSha256"}
            or context["schema"] != "honua.python-controlled-block/v1"
            or type(context["contextIndex"]) is not int or context["contextIndex"] not in (0, 1, 2)
            or not isinstance(context["blockSha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", context["blockSha256"]) is None):
        raise ValueError("controlled bridge declaration is not finite")
    # The git entry is a declared actual clone/transposition recipe. It never
    # treats merely exposing a prepared checkout as executing a clone line.
    operation = {"pip": "install", "git": "clone"}.get(Path(sys.argv[0]).name)
    if operation is None:
        raise ValueError("controlled bridge operation is not declared")
    if operation == "clone" and sys.argv[1:2] != ["clone"]:
        # Ordinary document Git commands execute in the isolated reader, never
        # in the trusted controller. Its Git metadata is still mounted RO.
        os.execv("/usr/bin/git", ["git", *sys.argv[1:]])
    request = {"operation": operation, "contextIndex": context["contextIndex"],
               "blockSha256": context["blockSha256"], "argv": sys.argv[1:], "cwd": os.getcwd()}
    raw = json.dumps(request, sort_keys=True).encode() + b"\n"
    if len(raw) > 64 * 1024:
        raise ValueError("controlled bridge request exceeds its bound")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect("/python-source-bridge/request.sock")
        connection.sendall(raw)
        response = bytearray()
        while not response.endswith(b"\n"):
            chunk = connection.recv(min(64 * 1024, 4 * 1024 * 1024 + 1 - len(response)))
            if not chunk:
                raise ValueError("controlled bridge response ended early")
            response.extend(chunk)
            if len(response) > 4 * 1024 * 1024:
                raise ValueError("controlled bridge response exceeds its bound")
    result = json.loads(response, object_pairs_hook=unique_object)
    if (set(result) != {"exitCode", "stdoutBase64", "stdoutSha256", "stderrBase64", "stderrSha256",
                       "successfulStderrCaptured"}
            or type(result["exitCode"]) is not int or not 0 <= result["exitCode"] <= 255):
        raise ValueError("controlled bridge response differs from its finite contract")
    for stream, field in ((sys.stdout.buffer, "stdout"), (sys.stderr.buffer, "stderr")):
        data = base64.b64decode(result[field + "Base64"], validate=True)
        if hashlib.sha256(data).hexdigest() != result[field + "Sha256"]:
            raise ValueError("controlled bridge response bytes changed")
        stream.write(data)
        stream.flush()
    return result["exitCode"]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("controlled source request refused; inspect the retained controller evidence", file=sys.stderr)
        raise SystemExit(1) from None

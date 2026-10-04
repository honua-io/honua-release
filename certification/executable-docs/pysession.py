"""In-container Python session: one interpreter per document, like a reader's REPL or notebook.

Reads one JSON request per line on stdin ({"file", "cwd", "env"}), executes that file's code in a
namespace shared with the document's earlier Python blocks, writes the block's stdout/stderr to
<file>.out / <file>.err and answers one JSON line {"ok": bool, "error": str}. Top-level await is
allowed, as in IPython. Runs inside the doc container with the container's own python3.
"""
import ast
import asyncio
import contextlib
import inspect
import json
import os
import sys
import traceback
import types

namespace = {"__name__": "__main__", "__builtins__": __builtins__}
FLAGS = getattr(ast, "PyCF_ALLOW_TOP_LEVEL_AWAIT", 0)

for raw in sys.stdin:
    request = json.loads(raw)
    path = request["file"]
    os.environ.update(request.get("env") or {})
    with contextlib.suppress(OSError):
        os.chdir(request.get("cwd") or ".")
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    ok, error = True, ""
    with open(path + ".out", "w") as out, open(path + ".err", "w") as err:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                source = open(path).read()
                code = compile(source, path, "exec", flags=FLAGS)
                if code.co_flags & inspect.CO_COROUTINE:
                    result = types.FunctionType(code, namespace)()
                    asyncio.run(result)
                else:
                    exec(code, namespace)
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    ok, error = False, f"SystemExit: {exc.code}"
            except BaseException:
                ok = False
                error = traceback.format_exc()
                print(error, file=sys.stderr)
    sys.__stdout__.write(json.dumps({"ok": ok, "error": error[-4000:]}) + "\n")
    sys.__stdout__.flush()

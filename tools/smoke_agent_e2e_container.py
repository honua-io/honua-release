"""Fixed local container smoke checks, not model execution or qualification."""
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, "/app")
from agent_e2e_container import terminal


assert os.geteuid() == 10001
assert not Path("/var/run/docker.sock").exists()
assert not Path("/app/agent_e2e_pack.py").exists()
assert not Path("/app/corpus").exists()
assert os.access("/workspace", os.W_OK) and os.access("/journal", os.W_OK)
workspace = Path("/workspace")
normal = terminal("id -u; printf '%s' \"$HONUA_AGENT_TRANSPORT$AWS_SECRET_ACCESS_KEY\"", 3, workspace)
assert normal["status"] == "completed" and normal["exitCode"] == 0
assert normal["stdout"] == "10001\n", normal
root_write = terminal("touch /app/should-not-exist", 3, workspace)
assert root_write["exitCode"] != 0 and not Path("/app/should-not-exist").exists()
started = time.monotonic()
timeout = terminal("sleep 10", 0.2, workspace)
assert timeout["status"] == "timed-out" and time.monotonic() - started < 3
oversized = terminal("python -c 'print(\"x\" * 2000000)'", 3, workspace)
assert len(oversized["stdout"].encode()) <= 16384 and len(oversized["stderr"].encode()) <= 4096
background = terminal("(sleep 1; touch survivor) &", 3, workspace)
time.sleep(1.2)
assert background["exitCode"] == 0 and not (workspace/"survivor").exists()
print("Container smoke passed: non-root, immutable app, isolated environment, bounded output, deadline, descendant cleanup.")

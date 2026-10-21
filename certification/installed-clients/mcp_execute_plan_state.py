"""Independent bounded atomic Redis job/result census for an owned candidate."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_execute_plan_validation import scenario, snapshot_output, PREFIX
from mcp_map_package_validation import strict_json
from mcp_render_validation import inspect_candidate
from mcp_style_state import restart

# One read-only Lua execution is atomic: records and results cannot come from
# different Redis instants. Do not include expiring coordination leases/indexes.
# The owned certification Redis is isolated, bounded and not a production target.
SCRIPT = r'''
local entries = {}
local size = 0
for _, key in ipairs(redis.call('KEYS', 'controlplane:job:*')) do
  local kind = redis.call('TYPE', key).ok
  -- Explicit store-owned coordination namespaces, with their actual types.
  local coordination = false
  if key == 'controlplane:job:active' or string.sub(key, 1, string.len('controlplane:job:active:')) == 'controlplane:job:active:' then
    if kind ~= 'set' then return redis.error_reply('malformed active index') end
    coordination = true
  elseif string.sub(key, 1, string.len('controlplane:job:index:created:')) == 'controlplane:job:index:created:' then
    if kind ~= 'zset' then return redis.error_reply('malformed created index') end
    coordination = true
  elseif string.sub(key, 1, string.len('controlplane:job:lease:')) == 'controlplane:job:lease:' then
    if kind ~= 'string' then return redis.error_reply('malformed job lease') end
    coordination = true
  elseif string.sub(key, 1, string.len('controlplane:job:log:')) == 'controlplane:job:log:' then
    if kind ~= 'list' then return redis.error_reply('malformed job log') end
    coordination = true
  end
  if not coordination then
    if kind ~= 'string' then return redis.error_reply('durable record is not a string') end
    local id = string.sub(key, string.len('controlplane:job:') + 1)
    local family = 'jobs'
    if string.sub(key, 1, string.len('controlplane:job:gp-result:')) == 'controlplane:job:gp-result:' then
      id = string.sub(key, string.len('controlplane:job:gp-result:') + 1)
      family = 'results'
    end
    local value = redis.call('GET', key)
    size = size + string.len(value)
    if #entries >= 10000 or size > 8388608 then return redis.error_reply('owned census bound exceeded') end
    table.insert(entries, {family, id, value})
  end
end
return entries
'''


def redis_command(arguments, *, connect=socket.create_connection):
    encoded = [a.encode() if isinstance(a, str) else a for a in arguments]
    payload = b"*"+str(len(encoded)).encode()+b"\r\n" + b"".join(b"$"+str(len(a)).encode()+b"\r\n"+a+b"\r\n" for a in encoded)
    # Fixed loopback destination; no caller-selected URL or forwarded credential.
    with connect(("127.0.0.1", 6379), timeout=5) as connection:
        connection.settimeout(10); connection.sendall(payload)
        stream = connection.makefile("rb")
        budget = [0]
        def read():
            line = stream.readline(1025)
            if not line.endswith(b"\r\n") or len(line) > 1024:
                raise ValueError("Redis framing refused")
            budget[0] += len(line)
            if budget[0] > 9*1024*1024:
                raise ValueError("Redis census response oversized")
            kind, raw = line[:1], line[1:-2]
            if kind == b"-":
                raise ValueError("independent Redis observation refused")
            if kind == b"*":
                count = int(raw)
                if not 0 <= count <= 10000:
                    raise ValueError("Redis census response array unbounded")
                return [read() for _ in range(count)]
            if kind == b"$":
                count = int(raw)
                if not 0 <= count <= 8*1024*1024 or budget[0]+count > 9*1024*1024:
                    raise ValueError("Redis census response bytes unbounded")
                value = stream.read(count+2)
                budget[0] += len(value)
                if len(value) != count+2 or value[-2:] != b"\r\n":
                    raise ValueError("Redis census response truncated")
                return value[:-2]
            raise ValueError("Redis census response type refused")
        return read()


def snapshot(owned_plan_id, *, execute=redis_command):
    rows = execute(["EVAL_RO", SCRIPT, "0"])
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("independent job census missing")
    observed = {"schema": "honua.mcp-execution-persisted/v1", "jobs": [], "results": []}
    owned_jobs = set()
    rows = sorted(rows, key=lambda row: row[0] if isinstance(row, list) and row else b"")
    for row in rows:
        if not isinstance(row, list) or len(row) != 3 or not all(isinstance(v, bytes) for v in row):
            raise ValueError("Redis census malformed")
        family, identifier = row[0].decode(), row[1].decode()
        if family not in ("jobs", "results") or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identifier):
            raise ValueError("Redis census identity malformed")
        value = strict_json(row[2])
        if not isinstance(value, dict):
            raise ValueError("Redis durable record malformed")
        if family == "jobs":
            if value.get("operationId") != identifier:
                raise ValueError("Redis job key and durable identity differ")
            parameters = value.get("spec", {}).get("parameters")
            audit = value.get("audit")
            if not isinstance(parameters, dict) or not isinstance(audit, dict):
                raise ValueError("Redis job spec/audit missing")
            binding = {k: value.get(k) for k in ("operationId", "status", "createdAt")}
            binding.update(idempotencyKey=audit.get("idempotencyKey"), parameters={k: v for k, v in parameters.items() if k.startswith(PREFIX)})
            if parameters.get(PREFIX+"plan_id") == owned_plan_id:
                owned_jobs.add(identifier)
            else:
                binding = {}
        else:
            # Keep only public result fields; raw record bytes are independently
            # hashed to detect changes in fields not projected into MCP results.
            binding = {k: value.get(k) for k in ("resultPackageId", "status", "artifacts", "errors")} if identifier in owned_jobs else {}
        observed[family].append({"jobId": identifier, "sha256": "sha256:"+hashlib.sha256(row[2]).hexdigest(), "binding": binding})
    for family in ("jobs", "results"):
        observed[family].sort(key=lambda row: row["jobId"])
    snapshot_output(observed)
    return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True); parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args()
    try:
        observed = strict_json(args.observations.read_bytes())
        authored = scenario(args.fixture.read_bytes(), strict_json(args.bootstrap.read_bytes()), observed["nonce"], observed["canonicalClient"])
        runtime = inspect_candidate(authored["candidateSourceSha"], authored["candidateImage"])
        if runtime != (observed.get("runtimeRestarted") or observed["runtimeBefore"]):
            raise ValueError("execution candidate changed before independent state observation")
        value = restart(authored, runtime) if args.restart else snapshot(authored["arguments"]["plan"]["planId"])
        print(json.dumps(value, allow_nan=False)); return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        print("owned execution state/restart refused", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())

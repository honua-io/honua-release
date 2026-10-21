"""Independent owned Postgres/Redis health and atomic queue observations.

These are live probes, not persisted health snapshots. Only the explicitly owned
loopback certification stores are contacted; private credentials are not retained.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_execute_plan_state import SCRIPT, redis_command
from mcp_ingest_publish_state import database_query, IDENTITY_SQL
from mcp_map_package_validation import strict_json
from mcp_render_validation import inspect_candidate
from mcp_style_state import restart

DATABASE_SQL = "SELECT json_build_object('database',current_database(),'user',current_user,'probe',1,'postgis',postgis_version(),'area',(SELECT json_agg(row_to_json(r) ORDER BY gid) FROM (SELECT gid,name,ARRAY[ST_XMin(Box2D(geom)),ST_YMin(Box2D(geom)),ST_XMax(Box2D(geom)),ST_YMax(Box2D(geom))] AS envelope,ST_SRID(geom) AS srid,ST_GeometryType(geom) AS \"geometryType\" FROM honua_data.sdkreg_area) r))::text;"


def queue_probe(nonce, *, execute=redis_command):
    if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{12,32}", nonce):
        raise ValueError("health probe nonce is outside the owned namespace")
    # Atomic bounded owned probe. The unique, short-lived certification key is
    # deleted before the queue census; no job/index/candidate record is mutated.
    prefix = "honua:certification:ops-health:" + nonce
    script = "local pong=redis.call('PING').ok\nlocal key='" + prefix + "'\n"
    script += "if not redis.call('SET',key,'owned-health-probe','NX','PX','5000') then return redis.error_reply('probe key exists') end\n"
    script += "local cached=redis.call('GET',key)\nlocal deleted=tostring(redis.call('DEL',key))\n"
    script += "local rows=(function()\n" + SCRIPT + "\nend)()\n"
    script += "return {pong,cached,deleted,rows,redis.call('SMEMBERS','controlplane:job:active')}"
    reply = execute(["EVAL", script, "0"])
    if not isinstance(reply, list) or len(reply) != 5 or reply[:3] != [b"PONG", b"owned-health-probe", b"1"]:
        raise ValueError("actual independent Redis ping/cache probe failed")
    rows, active = reply[3:]
    if not isinstance(rows, list) or not isinstance(active, list) or len(rows) > 10000 or len(active) > 10000:
        raise ValueError("actual bounded Redis census missing")
    jobs = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 3 or not all(isinstance(v, bytes) for v in row):
            raise ValueError("actual Redis census row malformed")
        family, identifier = row[0].decode(), row[1].decode()
        if family not in ("jobs", "results") or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identifier):
            raise ValueError("actual Redis census identity malformed")
        value = strict_json(row[2])
        if family == "jobs":
            if not isinstance(value, dict) or value.get("operationId") != identifier or not isinstance(value.get("spec"), dict):
                raise ValueError("actual durable job and key differ")
            jobs.append({"jobId": identifier, "status": value.get("status"), "backend": value["spec"].get("backend"),
                         "sha256": "sha256:" + hashlib.sha256(row[2]).hexdigest()})
    active_ids = [value.decode() for value in active if isinstance(value, bytes)]
    if len(active_ids) != len(active) or len(set(active_ids)) != len(active_ids):
        raise ValueError("actual Redis active index malformed")
    return {"ping": "PONG", "cacheRoundtrip": True, "probeRemoved": True,
            "jobs": sorted(jobs, key=lambda row: row["jobId"]), "activeIds": sorted(active_ids)}


def observe(nonce, *, password, sql=database_query, redis=redis_command):
    started = datetime.now(timezone.utc).isoformat()
    clock = time.monotonic()
    identity = strict_json(sql(IDENTITY_SQL, password))
    database = strict_json(sql(DATABASE_SQL, password))
    elapsed = (time.monotonic() - clock) * 1000
    redis_value = queue_probe(nonce, execute=redis)
    return {"schema": "honua.mcp-ops-health-observation/v1", "startedAt": started,
            "completedAt": datetime.now(timezone.utc).isoformat(), "databaseIdentity": identity,
            "database": database, "databaseProbeDurationMs": elapsed, "redis": redis_value}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-source-sha", required=True); parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--nonce", required=True); parser.add_argument("--restart", action="store_true")
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args()
    try:
        before = inspect_candidate(args.candidate_source_sha, args.candidate_image)
        if args.restart:
            value = restart({"candidateSourceSha": args.candidate_source_sha, "candidateImage": args.candidate_image}, before)
        else:
            value = observe(args.nonce, password=os.environ.get("HONUA_CERT_DB_PASSWORD"))
            if inspect_candidate(args.candidate_source_sha, args.candidate_image) != before:
                raise ValueError("candidate changed during independent health observation")
        print(json.dumps(value, allow_nan=False)); return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError, subprocess.SubprocessError):
        print("owned independent live health observation refused", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())

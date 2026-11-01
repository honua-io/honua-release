"""Independent, read-only state observations for a genuinely produced result.

Redis records are read atomically with verified key types. Coordination indexes
and leases are excluded explicitly; durable identity/payload multiplicity stays
visible. These observations do not by themselves identify a canonical client.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_execute_plan_state import redis_command, SCRIPT as JOB_CENSUS
from mcp_map_package_validation import strict_json
from mcp_ingest_publish_state import database_query, observe_target, IDENTITY_SQL
from mcp_ingest_publish_validation import registry_output, connection_provenance

# Reuse the already controlled job/result selection inside this same Lua call,
# rather than observing its records at a different instant.
SCRIPT = "local entries = (function()\n" + JOB_CENSUS + "\nend)()\n" + r'''
local size = 0
for _, row in ipairs(entries) do size = size + string.len(row[3]) end
local selections = {
  {'workflows','orchestration:run:', 'orchestration:run:*'},
  {'definitions','orchestration:def:', 'orchestration:def:*'},
  {'proposals','controlplane:proposal:', 'controlplane:proposal:*'},
  {'operations','controlplane:operation-instance:', 'controlplane:operation-instance:*'}
}
for _, selection in ipairs(selections) do
  for _, key in ipairs(redis.call('KEYS', selection[3])) do
    local kind = redis.call('TYPE', key).ok
    local id = string.sub(key, string.len(selection[2]) + 1)
    local coordination = false
    if (selection[1] == 'workflows' and (id == 'active' or string.sub(id,1,3) == 'wf:'))
        or (selection[1] == 'definitions' and id == 'all')
        or ((selection[1] == 'proposals' or selection[1] == 'operations') and
          (id == 'active' or string.sub(id,1,7) == 'active:')) then
      if kind ~= 'set' then return redis.error_reply('malformed owned active index') end
      coordination = true
    elseif selection[1] ~= 'definitions' and string.sub(id,1,6) == 'lease:' then
      if kind ~= 'string' then return redis.error_reply('malformed owned lease') end
      coordination = true
    end
    if not coordination then
      if kind ~= 'string' then return redis.error_reply('durable orchestration record is not string') end
      local value = redis.call('GET',key)
      size = size + string.len(value)
      if #entries >= 10000 or size > 8388608 then return redis.error_reply('owned state bound exceeded') end
      table.insert(entries,{selection[1],id,value})
    end
  end
end
return entries
'''


def read_state(*, execute=redis_command):
    records = execute(["EVAL_RO", SCRIPT, "0"])
    if not isinstance(records, list) or len(records) > 10000:
        raise ValueError("workflow census is missing/unbounded")
    families = {key: [] for key in ("jobs", "results", "workflows", "definitions", "proposals", "operations")}
    parsed, seen = {}, set()
    for row in records:
        if not isinstance(row, list) or len(row) != 3 or not all(isinstance(v, bytes) for v in row):
            raise ValueError("workflow state record malformed")
        family, identifier = row[0].decode(), row[1].decode()
        if (family not in families or not 0 < len(identifier) <= 1024 or any(ord(c) < 32 for c in identifier)
                or (family in ("jobs", "results") and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identifier))
                or (family == "workflows" and not re.fullmatch(r"wf-[a-f0-9]{32}", identifier))
                or (family, identifier) in seen):
            raise ValueError("workflow state identity missing/duplicated")
        value = strict_json(row[2])
        if not isinstance(value, dict):
            raise ValueError("workflow state is not an object")
        identity = {"jobs": "operationId", "workflows": "runId", "definitions": "workflowId",
                    "proposals": "proposalId", "operations": "operationInstanceId"}.get(family)
        if identity and value.get(identity) != identifier:
            raise ValueError("durable workflow key and payload identity differ")
        seen.add((family, identifier)); parsed[(family, identifier)] = value
        families[family].append({"id": identifier, "sha256": "sha256:"+hashlib.sha256(row[2]).hexdigest()})
    for rows in families.values():
        rows.sort(key=lambda row: row["id"])
    return families, parsed


def before_run(workflow_id, *, execute=redis_command):
    if not isinstance(workflow_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", workflow_id):
        raise ValueError("workflow observer requires bounded actual definition identity")
    census, parsed = read_state(execute=execute)
    definition = parsed.get(("definitions", workflow_id))
    if definition is None:
        raise ValueError("actual persisted workflow definition missing before manual run")
    return {"census": census, "definition": definition}


def workflow_state(run_id, workflow_id, *, execute=redis_command):
    if (not isinstance(run_id, str) or not re.fullmatch(r"wf-[a-f0-9]{32}", run_id)
            or not isinstance(workflow_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", workflow_id)):
        raise ValueError("workflow observer requires actual bounded public identities")
    families, parsed = read_state(execute=execute)
    run, definition = parsed.get(("workflows", run_id)), parsed.get(("definitions", workflow_id))
    if run is None or definition is None or run.get("workflowId") != workflow_id:
        raise ValueError("actual requested workflow run/definition is missing")
    steps = run.get("stepStates")
    if not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict) or steps[0].get("stepId") != "sink":
        raise ValueError("owned workflow did not retain its one real sink step")
    job_id = steps[0].get("jobId")
    if job_id is not None and (not isinstance(job_id, str) or not re.fullmatch(r"gp-[a-f0-9]{24,32}", job_id)):
        raise ValueError("actual workflow returned malformed worker job identity")
    return {"schema": "honua.mcp-publish-result-workflow-state/v1", "census": families,
            "run": run, "definition": definition,
            "job": parsed.get(("jobs", job_id)), "result": parsed.get(("results", job_id))}


def connection_observation(authored, password):
    guid = authored["connectionId"]
    if not isinstance(guid, str) or not re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", guid):
        raise ValueError("connection observation requires canonical GUID before SQL")
    query = ("SELECT COALESCE(json_agg(json_build_object('connectionId',connection_id::text,'name',name,'host',host,'port',port,"
             "'databaseName',database_name,'username',username,'provider',provider_name,'sslRequired',ssl_required,"
             "'sslMode',ssl_mode,'isActive',is_active)),'[]'::json)::text FROM honua.data_connections WHERE is_active "
             "AND connection_id='"+guid+"'::uuid;")
    rows = strict_json(database_query(query, password))
    row = registry_output(rows, guid, authored["ownedDatabase"])
    catalog = strict_json(database_query(IDENTITY_SQL, password))
    address, target = observe_target(row, password)
    value = {"selector": guid, "registry": rows, "catalog": catalog, "target": target, "verifiedAddress": address}
    connection_provenance(value, guid, authored["ownedDatabase"])
    return value




def database_snapshot(authored, password):
    """One SQL statement observes source rows, catalog and graph at one MVCC cut."""
    connection = connection_observation(authored, password)
    table, service = authored["table"], authored["serviceName"]
    if not re.fullmatch(r"mcp_result_(sdk|inspector)_[a-f0-9]{12}", table) or not re.fullmatch(r"mcp-result-(sdk|inspector)-[a-f0-9]{12}", service):
        raise ValueError("publication state identifier outside owned fixture")
    def literal(value):
        return "'"+value.replace("'", "''")+"'"
    # Explicitly retain row multiplicity across environments. No DISTINCT can
    # collapse two conflicting primary mappings into one apparently valid row.
    query = ("WITH live AS (SELECT active.environment,active.revision,snapshot.document "
        "FROM honua.metadata_v2_current active JOIN honua.metadata_v2_snapshots snapshot "
        "ON snapshot.environment=active.environment AND snapshot.revision=active.revision), "
        "mappings AS (SELECT live.environment,live.revision,jsonb_build_object("
        "'serviceId',svc#>>'{metadata,id}','serviceName',svc#>>'{metadata,name}',"
        "'layerId',(pub#>>'{identifier,value}')::integer,'storageLayerId',(binding->>'storageLayerId')::integer,"
        "'locator',binding->>'locator','connectionId',binding->>'connectionId') AS publication "
        "FROM live CROSS JOIN LATERAL jsonb_array_elements(document->'services') svc "
        "CROSS JOIN LATERAL jsonb_array_elements(document->'publications') pub "
        "CROSS JOIN LATERAL jsonb_array_elements(document->'storageBindings') binding "
        "WHERE svc#>>'{metadata,name}'="+literal(service)+" AND pub->>'serviceId'=svc#>>'{metadata,id}' "
        "AND pub->>'publicationType'='geoservices-rest-feature-layer' AND pub->>'isPrimary'='true' "
        "AND pub->>'storageBindingId'=binding#>>'{metadata,id}' AND pub->>'resourceId'=binding->>'resourceId'), "
        f'source_rows AS (SELECT id,attributes AS properties,ST_AsGeoJSON(geom)::json AS geometry,ST_SRID(geom) AS srid FROM public."{table}" ORDER BY id LIMIT 3), '
        "features AS (SELECT objectid AS id,layer_id AS \"layerId\",attributes,ST_AsGeoJSON(geometry)::json AS geometry,"
        "ST_SRID(geometry) AS srid,row_to_json(f) AS raw FROM public.features f ORDER BY objectid LIMIT 10001) "
        "SELECT json_build_object("
        "'rows',COALESCE((SELECT json_agg(row_to_json(source_rows) ORDER BY id) FROM source_rows),'[]'::json),"
        "'mappings',COALESCE((SELECT json_agg(row_to_json(mappings)) FROM mappings),'[]'::json),"
        "'graphs',COALESCE((SELECT json_agg(row_to_json(live) ORDER BY environment COLLATE \"C\") FROM live),'[]'::json),"
        "'services',COALESCE((SELECT json_agg(row_to_json(s)) FROM (SELECT * FROM honua.services ORDER BY service_name COLLATE \"C\" LIMIT 1001) s),'[]'::json),"
        "'layers',COALESCE((SELECT json_agg(row_to_json(l)) FROM (SELECT * FROM honua.layers ORDER BY layer_id LIMIT 1001) l),'[]'::json),"
        "'serviceLayers',COALESCE((SELECT json_agg(row_to_json(l)) FROM (SELECT * FROM honua.service_layers ORDER BY service_name COLLATE \"C\",layer_id LIMIT 1001) l),'[]'::json),"
        "'fields',COALESCE((SELECT json_agg(row_to_json(l)) FROM (SELECT * FROM honua.layer_fields ORDER BY layer_id,field_order,field_name COLLATE \"C\" LIMIT 10001) l),'[]'::json),"
        "'tableNames',COALESCE((SELECT json_agg(row_to_json(t)) FROM (SELECT table_schema AS schema,table_name AS name FROM information_schema.tables "
        "WHERE table_schema IN ('public','honua','honua_data') ORDER BY table_schema COLLATE \"C\",table_name COLLATE \"C\" LIMIT 1001) t),'[]'::json),"
        "'features',COALESCE((SELECT json_agg(row_to_json(features) ORDER BY id) FROM features),'[]'::json),"
        # Private encrypted credential/secret-reference columns are hashed below,
        # never retained in the observer's output or diagnostics.
        "'connections',COALESCE((SELECT json_agg(row_to_json(c)) FROM (SELECT * FROM honua.data_connections ORDER BY connection_id LIMIT 1001) c),'[]'::json))::text;")
    text = database_query(query, password)
    if len(text.encode()) > 8*1024*1024:
        raise ValueError("owned publication database observation oversized")
    observed = strict_json(text)
    if not isinstance(observed, dict) or set(observed) != {"rows", "mappings", "graphs", "services", "layers", "serviceLayers", "fields", "tableNames", "features", "connections"}:
        raise ValueError("owned database snapshot incomplete")
    for key, limit in (("services",1000),("layers",1000),("serviceLayers",1000),("fields",10000),("tableNames",1000),("features",10000),("connections",1000),("graphs",1000)):
        if not isinstance(observed[key], list) or len(observed[key]) > limit:
            raise ValueError("owned publication database census exceeded bound")
    digest = lambda value: "sha256:"+hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",",":"), allow_nan=False).encode()).hexdigest()
    connections = []
    for row in observed.pop("connections"):
        if not isinstance(row, dict) or not isinstance(row.get("connection_id"), str):
            raise ValueError("connection census identity malformed")
        connections.append({"connectionId": row["connection_id"], "sha256": digest(row)})
    features, materialized = [], []
    own_layers = {row["publication"]["storageLayerId"] for row in observed["mappings"] if isinstance(row, dict) and isinstance(row.get("publication"), dict)}
    for row in observed.pop("features"):
        if not isinstance(row, dict) or set(row) != {"id", "layerId", "attributes", "geometry", "srid", "raw"}:
            raise ValueError("canonical feature census incomplete")
        features.append({"id": row["id"], "layerId": row["layerId"], "sha256": digest(row["raw"])})
        if row["layerId"] in own_layers:
            materialized.append({k: row[k] for k in ("id", "layerId", "attributes", "geometry", "srid")})
    observed.update(schema="honua.mcp-publish-result-persisted/v1", features=features, materialized=materialized,
                    connectionHashes=connections, connection=connection)
    return observed


def main():
    import argparse
    import os
    import subprocess
    from mcp_publish_result_validation import scenario, safe_response
    from mcp_render_validation import inspect_candidate
    from mcp_style_state import restart
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture",type=Path,required=True); parser.add_argument("--bootstrap",type=Path,required=True)
    parser.add_argument("--observations",type=Path,required=True)
    parser.add_argument("--restart",action="store_true")
    parser.add_argument("--owned-disposable-candidate",action="store_true",required=True)
    args = parser.parse_args()
    try:
        observed = strict_json(args.observations.read_bytes())
        authored = scenario(args.fixture.read_bytes(),strict_json(args.bootstrap.read_bytes()),observed["nonce"],observed["canonicalClient"])
        runtime = inspect_candidate(authored["candidateSourceSha"],authored["candidateImage"])
        if runtime != (observed.get("runtimeRestarted") or observed["runtimeBefore"]):
            raise ValueError("publication candidate changed before independent observation/restart")
        if args.restart:
            value = restart(authored,runtime)
        else:
            password = os.environ.get("HONUA_CERT_DB_PASSWORD")
            if not isinstance(password,str) or not password:
                raise ValueError("owned database credential missing")
            # Each storage observation is internally atomic. These are separate
            # Redis and SQL cuts after the producer has reached quiescence; this
            # receipt does not claim a distributed transaction across stores.
            database = database_snapshot(authored,password)
            census, _ = read_state()
            value = {"schema":"honua.mcp-publish-result-state/v1","database":database,"census":census}
        encoded = json.dumps(value,allow_nan=False)
        safe_response(encoded,[os.environ.get("HONUA_CERT_DB_PASSWORD"),os.environ.get("HONUA_MCP_API_KEY")])
        print(encoded); return 0
    except (ValueError,TypeError,KeyError,AttributeError,OSError,subprocess.SubprocessError,RecursionError):
        print("owned genuine-result state/restart refused",file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())

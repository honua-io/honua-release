#!/usr/bin/env bash
# Deterministic Slice-1 seeder — publishes the exact contracts the drivers + demos assert:
#   - a data connection to the composed PostGIS
#   - honua_data.e2e_src_fs   -> published as service "e2e"/layer 1 (the layer the console live suite drives)
#   - honua_data.maui_zoning  -> published as service "e2e"/layer 2 with a STRING zone_code field
#     (codes 030 / 500) so the three-protocol parity check (GeoServices where == OData $filter ==
#     OGC cql2 filter) has a real target. Closes the #2324 data gap.
#
# Writes $E2E_OUT/seed-manifest.json with the resolved ids so drivers never hardcode.
#
# SQL is applied via $E2E_PSQL (defaults to the compose db service). Publishing goes through the real
# admin API so we exercise the same publish path a user would.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "$HERE/../lib/common.sh"

COMPOSE_FILE="${E2E_COMPOSE_FILE:-$HERE/../compose.candidate.yml}"
E2E_PSQL="${E2E_PSQL:-docker compose -f $COMPOSE_FILE exec -T db psql -U honua -d honua}"
DB_HOST="${E2E_DB_HOST:-db}"   # hostname the SERVER uses to reach PostGIS (compose service name)

psql_apply() { $E2E_PSQL -v ON_ERROR_STOP=1 "$@"; }

echo "== seed: applying deterministic tables via psql =="
psql_apply <<'SQL'
CREATE SCHEMA IF NOT EXISTS honua_data;

DROP TABLE IF EXISTS honua_data.e2e_src_fs;
CREATE TABLE honua_data.e2e_src_fs (
  gid   serial PRIMARY KEY,
  name  text NOT NULL,
  geom  geometry(Point,4326)
);
INSERT INTO honua_data.e2e_src_fs (name, geom) VALUES
 ('alpha', ST_SetSRID(ST_MakePoint(-156.33,20.75),4326)),
 ('bravo', ST_SetSRID(ST_MakePoint(-156.45,20.88),4326));

DROP TABLE IF EXISTS honua_data.maui_zoning;
CREATE TABLE honua_data.maui_zoning (
  gid       serial PRIMARY KEY,
  zone_code text NOT NULL,          -- STRING codes like '030' / '500' (leading-zero significant)
  zone_name text,
  geom      geometry(Point,4326)
);
INSERT INTO honua_data.maui_zoning (zone_code, zone_name, geom) VALUES
 ('030','Residential', ST_SetSRID(ST_MakePoint(-156.30,20.80),4326)),
 ('030','Residential', ST_SetSRID(ST_MakePoint(-156.31,20.81),4326)),
 ('500','Commercial',  ST_SetSRID(ST_MakePoint(-156.40,20.90),4326));
SQL

echo "== seed: creating data connection =="
api_post "/api/v1/admin/connections/" "$(jq -nc --arg h "$DB_HOST" \
  '{name:"e2e-pg",host:$h,port:5432,databaseName:"honua",username:"honua",password:"honua",provider:"PostGIS",sslRequired:false,sslMode:"Disable"}')"
CID="$(jget '.data.connectionId // .connectionId // .data.id')"
[ -z "$CID" ] && { echo "::error:: could not create connection (HTTP $HTTP_CODE): $HTTP_BODY"; exit 1; }
echo "   connection: $CID"

publish_layer() { # table layerName serviceName geomType
  # No explicit `fields` list — let the server introspect the real columns so every attribute
  # (e.g. the string zone_code) is exposed for the three-protocol parity queries.
  api_post "/api/v1/admin/connections/$CID/layers" "$(jq -nc \
    --arg t "$1" --arg ln "$2" --arg sn "$3" --arg gt "$4" \
    '{schema:"honua_data",table:$t,layerName:$ln,serviceName:$sn,geometryColumn:"geom",geometryType:$gt,primaryKey:"gid",srid:4326,enabled:true}')"
  jget '.data.layerId'
}

echo "== seed: publishing layers into service 'e2e' =="
L_SRC="$(publish_layer e2e_src_fs   e2e_src_fs   e2e Point)"
L_ZONING="$(publish_layer maui_zoning maui_zoning e2e Point)"
echo "   e2e_src_fs  -> layerId $L_SRC"
echo "   maui_zoning -> layerId $L_ZONING"

jq -nc \
  --arg cid "$CID" --arg svc "e2e" \
  --argjson srcId "${L_SRC:-null}" --argjson zoningId "${L_ZONING:-null}" \
  '{connectionId:$cid, service:$svc, layers:{e2e_src_fs:$srcId, maui_zoning:$zoningId}}' \
  > "$E2E_OUT/seed-manifest.json"
echo "== seed: wrote $E2E_OUT/seed-manifest.json =="
cat "$E2E_OUT/seed-manifest.json"

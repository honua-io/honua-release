#!/usr/bin/env bash
# Deterministic seeder — publishes the exact contracts the drivers + the honua-site demos assert.
#
# Slice-1 contracts (unchanged in meaning):
#   - a data connection to the composed PostGIS
#   - honua_data.e2e_src_fs   -> published as service "e2e"        (console live suite target)
#   - honua_data.maui_zoning  -> published as service "maui-zoning" with a STRING zone_code field
#     (exactly two rows carry '030') so the three-protocol parity check (GeoServices where ==
#     OData $filter == OGC cql2 filter) has a real target. Closes the #2324 data gap.
#
# S9 extension (the honua-site demo contract) — every value below is READ OFF honua-site, not
# invented; the point is that the demos run UNMODIFIED against this server:
#   - assets/demo/layers.json pins the service ids AND the server-assigned publication ids:
#       maui-parcels=1, maui-zoning=2, maui-roads=3, maui-flood-hazard=4,
#       maui-sea-level-rise=5, maui-place-names=6
#     Honua numbers layers globally in publication order, so the demo layers are published FIRST,
#     in exactly that order, and the resulting ids are asserted below.
#   - assets/demos/two-protocols/config.json  -> maui-zoning layerId 2, string zone_code
#     ('030','010','500','320','215','929'), fields zone_code/zone_dist/cp_area
#   - assets/demos/editing/config.json        -> service maui-inspections, OData Name
#     "maui-inspections", pk `id`, Point/4326, fields name/category/status/note/reported_at with
#     CHECK constraints mirroring categories/statuses/noteMaxLength, AllowAnonymous(+Write)
#   - assets/sdk-samples/.../spatial-analytics-workbench (the bundle demo-analyst-workbench.html
#     actually loads) -> live lane wants OBJECTID/risk/score over the Honolulu AOI extents; the six
#     seeded rows ARE that sample's own fixture assets (id/title/category/risk/zone/score/x/y).
#
# NOT seeded: imagery / hillshade / terrain rasters. The publish API is vector-only
# (schema+table+geometry column), there is no raster ingest path through it, and none of the five
# driven demos requires a raster — esri-leaflet probes the ImageServer tile route and honestly
# reports those two bases absent. demo-imagery-terrain.html is out of S9 scope.
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

-- ── Slice-1 console/source layer ────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS honua_data.e2e_src_fs;
CREATE TABLE honua_data.e2e_src_fs (
  gid   serial PRIMARY KEY,
  name  text NOT NULL,
  geom  geometry(Point,4326)
);
INSERT INTO honua_data.e2e_src_fs (name, geom) VALUES
 ('alpha', ST_SetSRID(ST_MakePoint(-156.33,20.75),4326)),
 ('bravo', ST_SetSRID(ST_MakePoint(-156.45,20.88),4326));

-- ── maui-zoning ─────────────────────────────────────────────────────────────────────────────────
-- Polygons (the demos render fill+line), string zone_code with leading zeros significant.
-- EXACTLY TWO rows carry '030' — the three-protocol parity check asserts that count.
DROP TABLE IF EXISTS honua_data.maui_zoning;
CREATE TABLE honua_data.maui_zoning (
  gid       serial PRIMARY KEY,
  zone_code text NOT NULL,          -- STRING codes like '030' / '500' (leading-zero significant)
  zone_dist text,
  cp_area   text,
  island    text,
  zone_name text,
  lon       double precision,
  lat       double precision,
  geom      geometry(Polygon,4326)
);
INSERT INTO honua_data.maui_zoning (zone_code, zone_dist, cp_area, island, zone_name, lon, lat) VALUES
 ('030','R-3 Residential','Wailuku-Kahului','Maui','Residential',      -156.500, 20.880),
 ('030','R-3 Residential','Wailuku-Kahului','Maui','Residential',      -156.492, 20.880),
 ('010','R-1 Residential','Wailuku-Kahului','Maui','Residential',      -156.484, 20.880),
 ('010','R-1 Residential','Kihei-Makena','Maui','Residential',         -156.476, 20.880),
 ('500','AG Agriculture','Wailuku-Kahului','Maui','Agriculture',       -156.500, 20.872),
 ('500','AG Agriculture','Makawao-Pukalani','Maui','Agriculture',      -156.492, 20.872),
 ('320','B-2 Business Community','Wailuku-Kahului','Maui','Business',  -156.484, 20.872),
 ('215','H-M Hotel','Kihei-Makena','Maui','Hotel',                     -156.476, 20.872),
 ('929','PK Park','Wailuku-Kahului','Maui','Park',                     -156.500, 20.864),
 ('929','PK Park','Paia-Haiku','Maui','Park',                          -156.492, 20.864),
 ('410','M-1 Light Industrial','Wailuku-Kahului','Maui','Industrial',  -156.484, 20.864),
 ('900','P-1 Public','Wailuku-Kahului','Maui','Public',                -156.476, 20.864);
UPDATE honua_data.maui_zoning
   SET geom = ST_SetSRID(ST_MakeEnvelope(lon, lat, lon + 0.007, lat + 0.007), 4326);
ALTER TABLE honua_data.maui_zoning DROP COLUMN lon, DROP COLUMN lat;

-- ── maui-parcels ────────────────────────────────────────────────────────────────────────────────
-- A TMK grid over Kahului/Wailuku. The esri-leaflet "click to query" scene opens at
-- (20.79, -156.46) z15 and the workbench/demo maps open over Kahului, so the grid deliberately
-- spans both. `zone` carries the state land-use district codes '1'..'6' the demos colour by.
DROP TABLE IF EXISTS honua_data.maui_parcels;
CREATE TABLE honua_data.maui_parcels (
  gid      serial PRIMARY KEY,
  tmk      text NOT NULL,
  zone     text NOT NULL,
  gisacres numeric(8,2),
  land_use text,
  geom     geometry(Polygon,4326)
);
INSERT INTO honua_data.maui_parcels (tmk, zone, gisacres, land_use, geom)
SELECT
  format('3-%s-%s-%s', 1 + (i % 9), lpad((j % 60)::text, 3, '0'), lpad(((i * 7 + j) % 200)::text, 3, '0')),
  (1 + ((i * 3 + j) % 6))::text,
  round((0.35 + ((i * 5 + j) % 23) * 0.41)::numeric, 2),
  (ARRAY['Residential','Agricultural','Commercial','Industrial','Conservation','Public'])[1 + ((i * 3 + j) % 6)],
  ST_SetSRID(ST_MakeEnvelope(
    -156.50 + i * 0.006, 20.76 + j * 0.006,
    -156.50 + i * 0.006 + 0.0055, 20.76 + j * 0.006 + 0.0055), 4326)
FROM generate_series(0, 13) AS i, generate_series(0, 22) AS j;

-- ── maui-roads ──────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS honua_data.maui_roads;
CREATE TABLE honua_data.maui_roads (
  gid        serial PRIMARY KEY,
  name       text NOT NULL,
  road_class text,
  geom       geometry(LineString,4326)
);
INSERT INTO honua_data.maui_roads (name, road_class, geom)
SELECT
  format('Route %s', 30 + i),
  (ARRAY['highway','arterial','local'])[1 + (i % 3)],
  ST_SetSRID(ST_MakeLine(
    ST_MakePoint(-156.50 + i * 0.01, 20.76),
    ST_MakePoint(-156.46 + i * 0.01, 20.92)), 4326)
FROM generate_series(0, 9) AS i;

-- ── maui-flood-hazard ───────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS honua_data.maui_flood_hazard;
CREATE TABLE honua_data.maui_flood_hazard (
  gid       serial PRIMARY KEY,
  fld_zone  text NOT NULL,
  geom      geometry(Polygon,4326)
);
INSERT INTO honua_data.maui_flood_hazard (fld_zone, geom)
SELECT
  (ARRAY['AE','VE','X'])[1 + (i % 3)],
  ST_SetSRID(ST_MakeEnvelope(
    -156.49 + i * 0.012, 20.88, -156.49 + i * 0.012 + 0.010, 20.90), 4326)
FROM generate_series(0, 5) AS i;

-- ── maui-sea-level-rise ─────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS honua_data.maui_sea_level_rise;
CREATE TABLE honua_data.maui_sea_level_rise (
  gid      serial PRIMARY KEY,
  scenario text NOT NULL,
  geom     geometry(Polygon,4326)
);
INSERT INTO honua_data.maui_sea_level_rise (scenario, geom)
SELECT
  '3.2ft',
  ST_SetSRID(ST_MakeEnvelope(
    -156.49 + i * 0.014, 20.895, -156.49 + i * 0.014 + 0.012, 20.905), 4326)
FROM generate_series(0, 4) AS i;

-- ── maui-place-names ────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS honua_data.maui_place_names;
CREATE TABLE honua_data.maui_place_names (
  gid   serial PRIMARY KEY,
  name  text NOT NULL,
  class text,
  geom  geometry(Point,4326)
);
INSERT INTO honua_data.maui_place_names (name, class, geom) VALUES
 ('Kahului',      'Populated Place', ST_SetSRID(ST_MakePoint(-156.4700, 20.8893),4326)),
 ('Wailuku',      'Populated Place', ST_SetSRID(ST_MakePoint(-156.5050, 20.8911),4326)),
 ('Kihei',        'Populated Place', ST_SetSRID(ST_MakePoint(-156.4450, 20.7644),4326)),
 ('Paia',         'Populated Place', ST_SetSRID(ST_MakePoint(-156.3697, 20.9033),4326)),
 ('Puunene',      'Populated Place', ST_SetSRID(ST_MakePoint(-156.4506, 20.8536),4326)),
 ('Waikapu',      'Populated Place', ST_SetSRID(ST_MakePoint(-156.5050, 20.8500),4326)),
 ('Maalaea',      'Bay',             ST_SetSRID(ST_MakePoint(-156.5100, 20.7920),4326)),
 ('Kahului Harbor','Harbor',         ST_SetSRID(ST_MakePoint(-156.4750, 20.8990),4326));

-- ── maui-inspections (the editing demo's scratch layer) ─────────────────────────────────────────
-- Contract from assets/demos/editing/config.json: pk `id`, Point/4326, the five field names, and
-- CHECK constraints mirroring categories / statuses / noteMaxLength so the sandbox blast radius is
-- one synthetic table with enum-validated rows.
DROP TABLE IF EXISTS honua_data.maui_inspections;
CREATE TABLE honua_data.maui_inspections (
  id          serial PRIMARY KEY,
  name        text NOT NULL,
  category    text NOT NULL CHECK (category IN ('trail','park','harbor','facility')),
  status      text NOT NULL CHECK (status IN ('ok','needs_attention','urgent')),
  note        text CHECK (note IS NULL OR char_length(note) <= 500),
  reported_at timestamptz NOT NULL DEFAULT now(),
  geom        geometry(Point,4326)
);
INSERT INTO honua_data.maui_inspections (name, category, status, note, reported_at, geom) VALUES
 ('Kahului Harbor pier 2',      'harbor',   'ok',              'routine check clear',        '2026-05-02T18:00:00Z', ST_SetSRID(ST_MakePoint(-156.4750,20.8990),4326)),
 ('Kanaha Beach Park restroom', 'park',     'needs_attention', 'fixture leak reported',      '2026-05-03T18:00:00Z', ST_SetSRID(ST_MakePoint(-156.4400,20.8960),4326)),
 ('Waihee Ridge trailhead',     'trail',    'ok',              NULL,                          '2026-05-04T18:00:00Z', ST_SetSRID(ST_MakePoint(-156.5230,20.9440),4326)),
 ('Iao Valley lookout',         'trail',    'urgent',          'washout after heavy rain',   '2026-05-05T18:00:00Z', ST_SetSRID(ST_MakePoint(-156.5450,20.8820),4326)),
 ('Maalaea small boat harbor',  'harbor',   'ok',              NULL,                          '2026-05-06T18:00:00Z', ST_SetSRID(ST_MakePoint(-156.5100,20.7920),4326)),
 ('Kihei baseyard',             'facility', 'needs_attention', 'gate latch worn',            '2026-05-07T18:00:00Z', ST_SetSRID(ST_MakePoint(-156.4450,20.7644),4326)),
 ('Paia community center',      'facility', 'ok',              NULL,                          '2026-05-08T18:00:00Z', ST_SetSRID(ST_MakePoint(-156.3697,20.9033),4326)),
 ('Hookipa overlook',           'park',     'ok',              'sign replaced',              '2026-05-09T18:00:00Z', ST_SetSRID(ST_MakePoint(-156.3570,20.9350),4326));

-- ── spatial-analytics workbench assets ──────────────────────────────────────────────────────────
-- The SDK sample bundle that demo-analyst-workbench.html loads aggregates count(OBJECTID) and
-- avg(score) grouped by `risk`, inside one of three Honolulu AOI extents. The rows below ARE that
-- sample's own fixture assets. OBJECTID is created quoted so the Esri field name matches exactly.
DROP TABLE IF EXISTS honua_data.workbench_assets;
CREATE TABLE honua_data.workbench_assets (
  "OBJECTID" serial PRIMARY KEY,
  asset_id   text NOT NULL,
  title      text NOT NULL,
  category   text NOT NULL,
  risk       text NOT NULL,
  zone       text,
  score      numeric(6,2) NOT NULL,
  geom       geometry(Point,4326)
);
INSERT INTO honua_data.workbench_assets (asset_id, title, category, risk, zone, score, geom) VALUES
 ('asset-1001','Iwilei electrical substation',    'Critical asset','critical','AE',94, ST_SetSRID(ST_MakePoint(-157.861,21.317),4326)),
 ('parcel-1002','Kakaako mixed-use parcel cluster','Parcel group', 'high',    'VE',82, ST_SetSRID(ST_MakePoint(-157.852,21.301),4326)),
 ('route-1003','Nimitz lifeline segment',         'Transportation','high',    'AE',78, ST_SetSRID(ST_MakePoint(-157.887,21.318),4326)),
 ('facility-1004','Kalihi response warehouse',    'Logistics',     'moderate','X', 61, ST_SetSRID(ST_MakePoint(-157.878,21.335),4326)),
 ('parcel-1005','Ala Moana coastal frontage',     'Parcel group',  'moderate','VE',67, ST_SetSRID(ST_MakePoint(-157.843,21.291),4326)),
 ('facility-1006','Airport fuel isolation valve', 'Critical asset','low',     'X', 39, ST_SetSRID(ST_MakePoint(-157.919,21.322),4326));
SQL

echo "== seed: creating data connection =="
api_post "/api/v1/admin/connections/" "$(jq -nc --arg h "$DB_HOST" \
  '{name:"e2e-pg",host:$h,port:5432,databaseName:"honua",username:"honua",password:"honua",provider:"PostGIS",sslRequired:false,sslMode:"Disable"}')"
CID="$(jget '.data.connectionId // .connectionId // .data.id')"
[ -z "$CID" ] && { echo "::error:: could not create connection (HTTP $HTTP_CODE): $HTTP_BODY"; exit 1; }
echo "   connection: $CID"

publish_layer() { # table layerName serviceName geomType [primaryKey]
  # No explicit `fields` list — let the server introspect the real columns so every attribute
  # (e.g. the string zone_code) is exposed for the three-protocol parity queries.
  api_post "/api/v1/admin/connections/$CID/layers" "$(jq -nc \
    --arg t "$1" --arg ln "$2" --arg sn "$3" --arg gt "$4" --arg pk "${5:-gid}" \
    '{schema:"honua_data",table:$t,layerName:$ln,serviceName:$sn,geometryColumn:"geom",geometryType:$gt,primaryKey:$pk,srid:4326,enabled:true}')"
  jget '.data.layerId'
}

# The demo pages are anonymous — they never ship a credential. demo.honua.io publishes these layers
# with AllowAnonymous (+ AllowAnonymousWrite on the inspections scratch layer), and without it every
# demo request 401s and the OData /Layers catalog comes back empty. Both the SERVICE policy and the
# per-LAYER policy have to be opened: the layer policy is what the OData catalog + OGC collection
# listings filter on.
open_anonymous() { # serviceName layerId allowWrite
  api_json PUT "/api/v1/admin/services/$1/access-policy" \
    "$(jq -nc --argjson w "$3" '{allowAnonymous:true,allowAnonymousWrite:$w}')" >/dev/null
  api_json PUT "/api/v1/admin/services/$1/layers/$2/metadata" \
    "$(jq -nc --argjson w "$3" '{accessPolicy:{allowAnonymous:true,allowAnonymousWrite:$w}}')" >/dev/null
}

# ORDER IS THE CONTRACT: Honua assigns publication ids globally in publish order, and
# honua-site's assets/demo/layers.json pins parcels=1, zoning=2, roads=3, flood=4, slr=5,
# place-names=6. Publish the demo layers first, in that exact order.
echo "== seed: publishing the honua-site demo services =="
L_PARCELS="$(publish_layer maui_parcels        maui-parcels        maui-parcels        Polygon)"
L_ZONING="$(publish_layer  maui_zoning         maui-zoning         maui-zoning         Polygon)"
L_ROADS="$(publish_layer   maui_roads          maui-roads          maui-roads          LineString)"
L_FLOOD="$(publish_layer   maui_flood_hazard   maui-flood-hazard   maui-flood-hazard   Polygon)"
L_SLR="$(publish_layer     maui_sea_level_rise maui-sea-level-rise maui-sea-level-rise Polygon)"
L_PLACES="$(publish_layer  maui_place_names    maui-place-names    maui-place-names    Point)"
L_INSPECT="$(publish_layer maui_inspections    maui-inspections    maui-inspections    Point id)"
L_WORKBENCH="$(publish_layer workbench_assets  workbench-assets    honua-workbench     Point OBJECTID)"

echo "== seed: publishing the Slice-1 source layer into service 'e2e' =="
L_SRC="$(publish_layer e2e_src_fs e2e_src_fs e2e Point)"

echo "   maui-parcels      -> layerId $L_PARCELS"
echo "   maui-zoning       -> layerId $L_ZONING"
echo "   maui-roads        -> layerId $L_ROADS"
echo "   maui-flood-hazard -> layerId $L_FLOOD"
echo "   maui-sea-level-rise -> layerId $L_SLR"
echo "   maui-place-names  -> layerId $L_PLACES"
echo "   maui-inspections  -> layerId $L_INSPECT"
echo "   workbench-assets  -> layerId $L_WORKBENCH"
echo "   e2e_src_fs        -> layerId $L_SRC"

# honua-site pins these three ids; a drift here silently breaks the demos, so say so loudly.
pin_check() { # label actual expected
  [ "$2" = "$3" ] || echo "::warning:: seeded $1 layerId=$2 but honua-site/assets/demo/layers.json pins $3 — S9 demos will not resolve"
}
pin_check maui-parcels    "$L_PARCELS" 1
pin_check maui-zoning     "$L_ZONING"  2
pin_check maui-place-names "$L_PLACES" 6

echo "== seed: opening anonymous access on the demo services =="
open_anonymous maui-parcels        "$L_PARCELS"   false
open_anonymous maui-zoning         "$L_ZONING"    false
open_anonymous maui-roads          "$L_ROADS"     false
open_anonymous maui-flood-hazard   "$L_FLOOD"     false
open_anonymous maui-sea-level-rise "$L_SLR"       false
open_anonymous maui-place-names    "$L_PLACES"    false
open_anonymous maui-inspections    "$L_INSPECT"   true
open_anonymous honua-workbench     "$L_WORKBENCH" false

jq -nc \
  --arg cid "$CID" --arg svc "maui-zoning" \
  --argjson srcId "${L_SRC:-null}" --argjson zoningId "${L_ZONING:-null}" \
  --argjson parcels "${L_PARCELS:-null}" --argjson roads "${L_ROADS:-null}" \
  --argjson flood "${L_FLOOD:-null}" --argjson slr "${L_SLR:-null}" \
  --argjson places "${L_PLACES:-null}" --argjson inspect "${L_INSPECT:-null}" \
  --argjson workbench "${L_WORKBENCH:-null}" \
  '{connectionId:$cid,
    service:$svc,
    layers:{e2e_src_fs:$srcId, maui_zoning:$zoningId},
    slice1:{e2e_src_fs:{service:"e2e", layerId:$srcId}},
    demo:{
      "maui-parcels":        {service:"maui-parcels",        layerId:$parcels},
      "maui-zoning":         {service:"maui-zoning",         layerId:$zoningId},
      "maui-roads":          {service:"maui-roads",          layerId:$roads},
      "maui-flood-hazard":   {service:"maui-flood-hazard",   layerId:$flood},
      "maui-sea-level-rise": {service:"maui-sea-level-rise", layerId:$slr},
      "maui-place-names":    {service:"maui-place-names",    layerId:$places},
      "maui-inspections":    {service:"maui-inspections",    layerId:$inspect},
      "workbench-assets":    {service:"honua-workbench",     layerId:$workbench}
    }}' \
  > "$E2E_OUT/seed-manifest.json"
echo "== seed: wrote $E2E_OUT/seed-manifest.json =="
cat "$E2E_OUT/seed-manifest.json"

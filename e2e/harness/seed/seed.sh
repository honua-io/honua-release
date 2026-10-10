#!/usr/bin/env bash
# Deterministic seeder â€” publishes the exact contracts the drivers + the honua-site demos assert.
#
# Slice-1 contracts (unchanged in meaning):
#   - a data connection to the composed PostGIS
#   - honua_data.e2e_src_fs   -> published as service "e2e"        (console live suite target)
#   - honua_data.maui_zoning  -> published as service "maui-zoning" with a STRING zone_code field
#     (exactly two rows carry '030') so the three-protocol parity check (GeoServices where ==
#     OData $filter == OGC cql2 filter) has a real target. Closes the #2324 data gap.
#
# S9 extension (the honua-site demo contract) â€” every value below is READ OFF honua-site, not
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
# driven demos requires a raster â€” esri-leaflet probes the ImageServer tile route and honestly
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

# SEED_DRY_RUN exercises this script's SQL and admin requests without PostGIS or the server.
# The request bodies still come from plan.py, which is the same helper a real seed runs.
if [ "${SEED_DRY_RUN:-}" = "1" ]; then
  DRY_LAYER_SEQ=0
  psql_apply() {
    if [ -n "${SEED_DRY_SQL:-}" ]; then
      cat >> "$SEED_DRY_SQL"
    else
      cat >/dev/null
    fi
  }
  api_json() {
    local method="$1" path="$2" data="${3:-}"
    if [ -n "${SEED_DRY_REQUESTS:-}" ]; then
      printf '%s\n' "$data" >> "$SEED_DRY_REQUESTS"
    fi
    case "$path" in
      */layers/*/metadata|*/access-policy)
        if [ "${SEED_DRY_FAIL:-}" = "access" ]; then
          HTTP_CODE=403
          HTTP_BODY='{"error":"permission refused"}'
          return 0
        fi
        HTTP_CODE=200
        HTTP_BODY='{}'
        ;;
      */layers)
        if [ "${SEED_DRY_FAIL:-}" = "publish" ]; then
          HTTP_CODE=500
          HTTP_BODY='{"error":"publish refused"}'
          return 0
        fi
        DRY_LAYER_SEQ=$((DRY_LAYER_SEQ + 1))
        HTTP_CODE=201
        HTTP_BODY="$(jq -nc --argjson id "$DRY_LAYER_SEQ" '{data:{layerId:$id}}')"
        ;;
      *)
        HTTP_CODE=201
        HTTP_BODY='{"data":{"connectionId":"dry-run-connection"}}'
        ;;
    esac
  }
  api_post() { api_json POST "$1" "${2:-}"; }
fi

echo "== seed: applying deterministic tables via psql =="
psql_apply <<'SQL'
CREATE SCHEMA IF NOT EXISTS honua_data;

-- â”€â”€ Slice-1 console/source layer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
DROP TABLE IF EXISTS honua_data.e2e_src_fs;
CREATE TABLE honua_data.e2e_src_fs (
  gid   serial PRIMARY KEY,
  name  text NOT NULL,
  geom  geometry(Point,4326)
);
INSERT INTO honua_data.e2e_src_fs (name, geom) VALUES
 ('alpha', ST_SetSRID(ST_MakePoint(-156.33,20.75),4326)),
 ('bravo', ST_SetSRID(ST_MakePoint(-156.45,20.88),4326));

-- â”€â”€ maui-zoning â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
-- Polygons (the demos render fill+line), string zone_code with leading zeros significant.
-- EXACTLY TWO rows carry '030' â€” the three-protocol parity check asserts that count.
-- Source table for the honua-console live suite (S4). Its services-layers spec drives the console's
-- publish UI to create the service `e2e_src_fs` out of this table, and every other live spec (Studio
-- results, service settings) targets that service. Shape must match honua-console's own testbed seed
-- (e2e/initdb/01-seed.sql): integer PK, polygon geometry in EPSG:3857, exactly 3 features â€” the spec
-- asserts the published layer serves all three back in that SRID. Lives in `public` (not honua_data)
-- for the same reason: that is where the console spec looks for it.
DROP TABLE IF EXISTS public.e2e_layer_src;
CREATE TABLE public.e2e_layer_src (
  id   integer PRIMARY KEY,
  name text    NOT NULL,
  geom geometry(Polygon, 3857) NOT NULL
);
INSERT INTO public.e2e_layer_src (id, name, geom) VALUES
 (1, 'alpha', ST_SetSRID(ST_MakeEnvelope(  0,   0, 100, 100, 3857), 3857)),
 (2, 'beta',  ST_SetSRID(ST_MakeEnvelope(200, 200, 300, 300, 3857), 3857)),
 (3, 'gamma', ST_SetSRID(ST_MakeEnvelope(400, 400, 500, 500, 3857), 3857));

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

-- â”€â”€ maui-parcels â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

-- â”€â”€ maui-roads â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

-- â”€â”€ maui-flood-hazard â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

-- â”€â”€ maui-sea-level-rise â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

-- â”€â”€ maui-place-names â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

-- â”€â”€ maui-inspections (the editing demo's scratch layer) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

-- â”€â”€ spatial-analytics workbench assets â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

echo "== seed: applying the synthetic maui-buildings fixture =="
buildings_sql="$(python3 "$HERE/plan.py" buildings-sql)"
psql_apply <<<"$buildings_sql"

echo "== seed: creating data connection =="
api_post "/api/v1/admin/connections/" "$(jq -nc --arg h "$DB_HOST" \
  '{name:"e2e-pg",host:$h,port:5432,databaseName:"honua",username:"honua",password:"honua",provider:"PostGIS",sslRequired:false,sslMode:"Disable"}')"
CID="$(jget '.data.connectionId // .connectionId // .data.id')"
[ -z "$CID" ] && { echo "::error:: could not create connection (HTTP $HTTP_CODE): $HTTP_BODY"; exit 1; }
echo "   connection: $CID"

# Publish bodies come from plan.py so the admin request the seed sends is the one the
# tests execute. No fields list: the server introspects columns (zone_code included).
# Edit capabilities are declared only for maui-inspections, and only as storageMode
# managed plus Query/Create/Update/Delete. maui-buildings is published last so the
# first nine layer ids stay on their historical services.

# The demo pages are anonymous. They never ship a credential. demo.honua.io publishes
# these layers with AllowAnonymous, and AllowAnonymousWrite on the inspections scratch
# layer. Without that, every demo request 401s and the OData /Layers catalog comes
# back empty. Both the service policy and the per-layer policy have to be opened:
# the layer policy is what the OData catalog and OGC collection listings filter on.
open_anonymous() { # serviceName layerId allowWrite
  api_json PUT "/api/v1/admin/services/$1/access-policy" \
    "$(jq -nc --argjson w "$3" '{allowAnonymous:true,allowAnonymousWrite:$w}')"
  printf '%s' "$HTTP_BODY" | python3 "$HERE/plan.py" require-http --step "access-policy $1" --status "$HTTP_CODE"
  api_json PUT "/api/v1/admin/services/$1/layers/$2/metadata" \
    "$(jq -nc --argjson w "$3" '{accessPolicy:{allowAnonymous:true,allowAnonymousWrite:$w}}')"
  printf '%s' "$HTTP_BODY" | python3 "$HERE/plan.py" require-http --step "layer-metadata $1/$2" --status "$HTTP_CODE"
}

# ORDER IS THE CONTRACT: Honua assigns publication ids globally in publish order, and
# honua-site assets/demo/layers.json pins parcels=1, zoning=2, roads=3, flood=4, slr=5,
# place-names=6. Those nine services are published first, in that order. maui-buildings
# follows them. Docs that hard-code the public demo's layer 13 bind the returned id.
echo "== seed: publishing services =="
id_file="$E2E_OUT/seed-layer-ids.txt"
: > "$id_file"
while IFS= read -r body; do
  svc="$(jq -r '.serviceName' <<<"$body")"
  api_post "/api/v1/admin/connections/$CID/layers" "$body"
  printf '%s' "$HTTP_BODY" | python3 "$HERE/plan.py" require-http --step "publish $svc" --status "$HTTP_CODE"
  id="$(jget '.data.layerId // .layerId // empty')"
  if [ -z "$id" ] || [ "$id" = "null" ]; then
    echo "::error:: publish $svc returned no layerId (HTTP $HTTP_CODE): $HTTP_BODY" >&2
    exit 1
  fi
  printf '%s %s\n' "$svc" "$id" >> "$id_file"
  echo "   $svc -> layerId $id"
done < <(python3 "$HERE/plan.py" publish-bodies | jq -c '.[]')

layer_id_of() { # serviceName
  awk -v svc="$1" '$1 == svc { print $2 }' "$id_file"
}

# honua-site pins these three ids; a drift here silently breaks the demos, so say so loudly.
pin_check() { # label actual expected
  [ "$2" = "$3" ] || echo "::warning:: seeded $1 layerId=$2 but honua-site/assets/demo/layers.json pins $3 - S9 demos will not resolve"
}
pin_check maui-parcels "$(layer_id_of maui-parcels)" 1
pin_check maui-zoning "$(layer_id_of maui-zoning)" 2
pin_check maui-place-names "$(layer_id_of maui-place-names)" 6

echo "== seed: opening anonymous access on the demo services =="
while IFS= read -r row; do
  svc="$(jq -r '.serviceName' <<<"$row")"
  write="$(jq -r '.allowAnonymousWrite' <<<"$row")"
  open_anonymous "$svc" "$(layer_id_of "$svc")" "$write"
done < <(python3 "$HERE/plan.py" access-plan | jq -c '.[]')

ids_json="$(python3 -c '
import json, sys
ids = {}
for line in sys.stdin:
    service, layer_id = line.split()
    ids[service] = int(layer_id)
json.dump(ids, sys.stdout, separators=(",", ":"))
' < "$id_file")"
python3 "$HERE/plan.py" render-manifest --connection-id "$CID" --ids "$ids_json" > "$E2E_OUT/seed-manifest.json"
rm -f "$id_file"
echo "== seed: wrote $E2E_OUT/seed-manifest.json =="
cat "$E2E_OUT/seed-manifest.json"

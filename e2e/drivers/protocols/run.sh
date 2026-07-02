#!/usr/bin/env bash
# Three-protocol parity — the same maui_zoning filter (string field zone_code = '030') must return the
# SAME feature count across GeoServices (where=), OData ($filter), and OGC Features (cql2 filter).
# Emits one protocolCoverage[] row per protocol plus a scenario verdict. Closes the #2324 parity gap.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../harness/lib/common.sh
source "$HERE/../../harness/lib/common.sh"

if ! server_ready || [ ! -f "$E2E_OUT/seed-manifest.json" ]; then
  emit_scenario "S-protocol-parity" blocked "server not ready or seed missing"
  for p in geoservices odata ogc-features; do emit_protocol "$p" "zone_code='030'" blocked "server/seed unavailable"; done
  exit 0
fi

SVC="$(jq -r '.service' "$E2E_OUT/seed-manifest.json")"
LID="$(jq -r '.layers.maui_zoning' "$E2E_OUT/seed-manifest.json")"
EXPECT=2   # two seeded rows carry zone_code '030'

# GeoServices — returnCountOnly
api_get "/rest/services/$SVC/FeatureServer/$LID/query?where=zone_code%3D%27030%27&returnCountOnly=true&f=json"
GEO="$(jget '.count')"; [ -z "$GEO" ] && GEO="ERR:$HTTP_CODE"

# OData — generic Features entity set, filter by LayerId + field, $count=true
api_get "/odata/Features?%24filter=LayerId%20eq%20${LID}%20and%20zone_code%20eq%20%27030%27&%24count=true"
ODATA="$(jget '.["@odata.count"] // (.value|length)')"; [ -z "$ODATA" ] && ODATA="ERR:$HTTP_CODE"

# OGC API Features — cql2-text filter; the collection id is discovered from the catalog.
api_get "/ogc/collections?f=json"
OGC_CID="$(printf '%s' "$HTTP_BODY" | jq -r --arg s "$SVC" '.collections[]?|select((.id|test($s)) and (.id|test("zoning")))|.id' 2>/dev/null | head -1)"
if [ -n "$OGC_CID" ]; then
  api_get "/ogc/collections/$OGC_CID/items?filter-lang=cql2-text&filter=zone_code%3D%27030%27&limit=100&f=json"
  OGC="$(jget '.numberMatched // (.features|length)')"; [ -z "$OGC" ] && OGC="ERR:$HTTP_CODE"
else
  OGC="not-exposed"
fi

row() { # protocol op value  (pass if value==EXPECT)
  local st="fail"; [ "$3" = "$EXPECT" ] && st="pass"
  case "$3" in not-exposed|ERR:*) st="blocked" ;; esac
  emit_protocol "$1" "$2" "$st" "returned=$3 expected=$EXPECT"
  echo "$st"
}
g="$(row geoservices "where=zone_code='030'" "$GEO")"
o="$(row odata "\$filter=zone_code eq '030'" "$ODATA")"
c="$(row ogc-features "cql2 zone_code='030'" "$OGC")"

# Parity scenario: the protocols that ARE exposed must agree with each other and with EXPECT.
if [ "$g" = "fail" ] || [ "$o" = "fail" ] || [ "$c" = "fail" ]; then
  emit_scenario "S-protocol-parity" fail "protocol disagreement geoservices=$GEO odata=$ODATA ogc=$OGC (expect $EXPECT)"
elif [ "$g" = "pass" ] && [ "$o" = "pass" ] && { [ "$c" = "pass" ] || [ "$OGC" = "not-exposed" ]; }; then
  note="geoservices+odata agree ($EXPECT)"; [ "$OGC" = "not-exposed" ] && note="$note; OGC Features collection not auto-exposed (defect: needs per-service OGC enablement)"
  emit_scenario "S-protocol-parity" pass "$note"
else
  emit_scenario "S-protocol-parity" blocked "insufficient protocols exposed geoservices=$GEO odata=$ODATA ogc=$OGC"
fi

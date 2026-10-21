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
GEO="$(jget '.count')"; { [ "$HTTP_CODE" != 200 ] || [ -z "$GEO" ]; } && GEO="ERR:$HTTP_CODE"

# OData — use the canonical layer-scoped feature collection, then apply the
# same attribute predicate. This avoids asking the cross-layer adapter to
# strip its synthetic LayerId term before forwarding the dynamic field filter.
api_get "/odata/Layers(${LID})/Features?%24filter=zone_code%20eq%20%27030%27&%24count=true"
ODATA="$(jget '.["@odata.count"] // (.value|length)')"; { [ "$HTTP_CODE" != 200 ] || [ -z "$ODATA" ]; } && ODATA="ERR:$HTTP_CODE"

# OGC API Features uses the published layer ID, not a service-name substring.
# Require that exact identity in the catalog; an unrelated collection cannot prove parity.
api_get "/ogc/features/collections?f=json"
OGC_CID=""
if [ "$HTTP_CODE" = 200 ]; then
  OGC_CID="$(printf '%s' "$HTTP_BODY" | jq -r --arg id "$LID" \
    '[.collections[]? | select(.id == $id)] | if length == 1 then .[0].id else empty end' 2>/dev/null || true)"
fi
if [ -n "$OGC_CID" ]; then
  api_get "/ogc/features/collections/$OGC_CID/items?filter-lang=cql2-text&filter=zone_code%3D%27030%27&limit=100&f=json"
  OGC="$(jget '.numberMatched // (.features|length)')"; { [ "$HTTP_CODE" != 200 ] || [ -z "$OGC" ]; } && OGC="ERR:$HTTP_CODE"
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

# All three declared protocols must execute and agree with the independent fixture count.
if [ "$g" = "fail" ] || [ "$o" = "fail" ] || [ "$c" = "fail" ]; then
  emit_scenario "S-protocol-parity" fail "protocol disagreement geoservices=$GEO odata=$ODATA ogc=$OGC (expect $EXPECT)"
elif [ "$g" = "pass" ] && [ "$o" = "pass" ] && [ "$c" = "pass" ]; then
  emit_scenario "S-protocol-parity" pass "geoservices+odata+ogc-features agree ($EXPECT)"
else
  emit_scenario "S-protocol-parity" blocked "insufficient protocols exposed geoservices=$GEO odata=$ODATA ogc=$OGC"
fi

#!/usr/bin/env bash
# Entitlement certification lane: boot the pinned candidate once per fixture license, seed it with
# the harness seeder, run the fixture's probes, and join the observations into one verdict.
#
# Usage: lane.sh <LicenseTestSupport.cs from the candidate's exact honua-server commit> <out-dir>
#
# The fixture envelopes are minted into a private temporary directory, passed to compose through the
# environment, and deleted on exit; only their fingerprints reach <out-dir>. Writes
# <out-dir>/report.json and prints status=/why= lines for the gate fragment. Exit 0 unless the
# verdict is fail.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
CORPUS="${1:?LicenseTestSupport.cs path required}"
OUT="${2:?output directory required}"
PORT="${E2E_SERVER_PORT:-8080}"
BASE="http://localhost:${PORT}"
API_KEY="${E2E_API_KEY:-honua-console-dev-key}"
mkdir -p "$OUT/observations"

LICENSES="$(mktemp -d)"
trap 'rm -rf "$LICENSES"' EXIT
if ! python3 "$HERE/fixtures.py" --corpus-source "$CORPUS" --out "$LICENSES"; then
  echo "status=blocked"
  echo "why=fixture licenses could not be minted from the server test corpus"
  exit 0
fi

if [ -z "${HONUA_SERVER_IMAGE:-}" ]; then
  HONUA_SERVER_IMAGE="$(python3 - "$REPO_ROOT/platform-manifest.yaml" <<'PY'
import sys, yaml
server = (yaml.safe_load(open(sys.argv[1])).get("components") or {}).get("honua-server") or {}
image, digest = server.get("image", ""), server.get("digest", "")
print(f"{image}@{digest}" if image and digest and "@sha256:" not in image else image)
PY
)"
fi
export HONUA_SERVER_IMAGE HONUA_PUBLIC_BASE_URL="$BASE" E2E_SERVER_PORT="$PORT"
COMPOSE=(docker compose -f "$REPO_ROOT/e2e/harness/compose.candidate.yml" -f "$HERE/compose.licensed.yml")

wait_ready() { # the first boot can race the PostGIS image's init restart; restart the server once
  local i code
  for i in $(seq 1 60); do
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$BASE/healthz/ready" || echo 000)"
    [ "$code" = 200 ] && return 0
    [ "$i" = 30 ] && "${COMPOSE[@]}" restart server >/dev/null 2>&1
    sleep 5
  done
  return 1
}

BOOT_FAILURES=()
for FIXTURE in $(python3 -c "import json;print(' '.join(f['id'] for f in json.load(open('$HERE/fixtures.v1.json'))['fixtures']))"); do
  echo "::group::entitlement fixture: $FIXTURE"
  EDITION="$(python3 -c "import json;print(next(f['edition'] for f in json.load(open('$HERE/fixtures.v1.json'))['fixtures'] if f['id']=='$FIXTURE'))")"
  export COMPOSE_PROJECT_NAME="honua-entitlements-$FIXTURE" HONUA_ENTITLEMENT_EDITION="$EDITION"
  HONUA_ENTITLEMENT_LICENSE="$(cat "$LICENSES/$FIXTURE.honua-license.json")"
  HONUA_ENTITLEMENT_TRUSTED_KEY="$(cat "$LICENSES/trusted-key")"
  export HONUA_ENTITLEMENT_LICENSE HONUA_ENTITLEMENT_TRUSTED_KEY
  mkdir -p "$OUT/$FIXTURE"
  if"${COMPOSE[@]}" up -d && wait_ready \
     && E2E_BASE="$BASE" E2E_API_KEY="$API_KEY" E2E_OUT="$OUT/$FIXTURE" \
        E2E_COMPOSE_FILE="$REPO_ROOT/e2e/harness/compose.candidate.yml" bash "$REPO_ROOT/e2e/harness/seed/seed.sh" >"$OUT/$FIXTURE-seed.log" 2>&1; then
    HONUA_ADMIN_PASSWORD="$API_KEY" python3 "$HERE/run.py" observe --fixture "$FIXTURE" --base-url "$BASE" \
      --license "$LICENSES/$FIXTURE.honua-license.json" --seed-manifest "$OUT/$FIXTURE/seed-manifest.json" \
      --out "$OUT/observations/$FIXTURE.json"
  else
    BOOT_FAILURES+=("$FIXTURE")
    "${COMPOSE[@]}" logs --no-color --tail 60 server 2>&1 | grep -aiE 'licen|exception|error' | head -n 12 | cut -c1-300
  fi
  "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
  unset HONUA_ENTITLEMENT_LICENSE
  echo "::endgroup::"
done

python3 "$HERE/run.py" verdict --observations "$OUT/observations" --out "$OUT/report.json" > "$OUT/verdict.txt"
RC=$?
if [ "${#BOOT_FAILURES[@]}" -gt 0 ]; then
  # A licensed candidate that refuses to boot or seed with a valid fixture license is a defect.
  echo "status=fail"
  echo "why=candidate did not boot and seed with fixture license(s): ${BOOT_FAILURES[*]}"
  exit 1
fi
cat "$OUT/verdict.txt"
exit "$RC"

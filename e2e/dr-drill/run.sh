#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
COMPOSE_FILE="$ROOT/e2e/local-docker/docker-compose.yml"
PROJECT="${HONUA_DR_PROJECT:-honua-dr-${GITHUB_RUN_ID:-local}-$$}"
OUT="${HONUA_DR_OUTPUT:-$ROOT/artifacts/dr-drill-local-docker}"
if [[ -n "${HONUA_SERVER_PORT:-}" ]]; then
  PORT=$HONUA_SERVER_PORT
else
  PORT=$(python3 - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    print(sock.getsockname()[1])
PY
  )
fi
export HONUA_SERVER_PORT="$PORT"

readarray -t LOCK < <(python3 - "$ROOT/platform-manifest.yaml" <<'PY'
import sys, yaml
m=yaml.safe_load(open(sys.argv[1]))
s=m['components']['honua-server']
print(m['platformRelease']); print(s['sha']); print(s['image']+'@'+s['digest']); print(s['digest']); print(s['dbSchema'])
PY
)
RELEASE=${LOCK[0]}; CANDIDATE_SHA=${LOCK[1]}; export HONUA_SERVER_IMAGE=${LOCK[2]}; IMAGE_DIGEST=${LOCK[3]}; EXPECTED_SCHEMA=${LOCK[4]}
mkdir -p "$OUT"
cleanup() { docker compose -p "$PROJECT" -f "$COMPOSE_FILE" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT
dc() { docker compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@"; }
psql_db() { dc exec -T db psql -X -v ON_ERROR_STOP=1 -U honua -d honua "$@"; }

START_NS=$(date +%s%N)
dc up -d --wait
psql_db -f - < "$ROOT/e2e/dr-drill/seed.sql"
SEED_NS=$(date +%s%N)
# Quiesce application writers so the snapshot and pg_dump describe the same recovery point.
dc stop server
BEFORE=$(psql_db -At -f - < "$ROOT/e2e/dr-drill/snapshot.sql")

dc exec -T db pg_dump -U honua -d honua --format=custom --compress=9 --no-owner --no-acl > "$OUT/backup.dump"
BACKUP_NS=$(date +%s%N)
BACKUP_SHA=$(sha256sum "$OUT/backup.dump" | awk '{print $1}')
MANIFEST_SHA=$(sha256sum "$ROOT/platform-manifest.yaml" | awk '{print $1}')

# Destroy the only database copy, including its named volume, then create a clean PostGIS database.
DESTROY_NS=$(date +%s%N)
dc down -v --remove-orphans
dc up -d --wait db
# The PostGIS entrypoint performs one controlled restart after its healthcheck can first succeed.
# Require readiness after that initialization window before streaming the only backup into it.
sleep 3
for _ in $(seq 1 30); do
  if dc exec -T db pg_isready -U honua -d honua >/dev/null 2>&1; then break; fi
  sleep 1
done
dc exec -T db pg_isready -U honua -d honua
dc exec -T db pg_restore -U honua -d honua --clean --if-exists --no-owner --no-acl --exit-on-error < "$OUT/backup.dump"
AFTER=$(psql_db -At -f - < "$ROOT/e2e/dr-drill/snapshot.sql")
dc up -d --wait server

JOURNEY=$(curl --fail --silent --show-error "http://127.0.0.1:${PORT}/rest/services?f=json")
python3 - "$JOURNEY" <<'PY'
import json,sys
d=json.loads(sys.argv[1])
if not isinstance(d, dict) or 'services' not in d:
    raise SystemExit('restored service-catalog customer journey returned an invalid catalog')
PY
GREEN_NS=$(date +%s%N)

python3 - "$BEFORE" "$AFTER" "$EXPECTED_SCHEMA" <<'PY'
import json,sys
a,b=json.loads(sys.argv[1]),json.loads(sys.argv[2])
if a != b: raise SystemExit(f'pre/post restore snapshots differ: {a!r} != {b!r}')
if f".{sys.argv[3]}_" not in str(a['schemaVersion']):
    raise SystemExit(f"schema floor mismatch: {a['schemaVersion']}")
if any(v != 1 for k,v in a['rows'].items() if k != 'tenants') or a['rows']['tenants'] != 2:
    raise SystemExit(f"row denominator failed: {a['rows']}")
if a['tenantIsolation']['crossTenantLeakCount'] != 0: raise SystemExit('tenant isolation failed')
PY

export RELEASE CANDIDATE_SHA IMAGE_DIGEST EXPECTED_SCHEMA BACKUP_SHA MANIFEST_SHA BEFORE AFTER
export RPO_MS=$(( (BACKUP_NS-SEED_NS)/1000000 )) RTO_MS=$(( (GREEN_NS-DESTROY_NS)/1000000 ))
export STARTED_AT=$(date -u -d "@$((START_NS/1000000000))" +%Y-%m-%dT%H:%M:%SZ) COMPLETED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
python3 - "$OUT/receipt.json" <<'PY'
import json,os,sys
r={
 'schema':'honua.dr-drill-receipt/v1','status':'pass','topology':'local-docker',
 'candidate':{'platformRelease':os.environ['RELEASE'],'serverSha':os.environ['CANDIDATE_SHA'],'imageDigest':os.environ['IMAGE_DIGEST'],'dbSchema':os.environ['EXPECTED_SCHEMA'],'releaseLock':{'path':'platform-manifest.yaml','sha256':'sha256:'+os.environ['MANIFEST_SHA']}},
 'backup':{'format':'pg_dump-custom','sha256':'sha256:'+os.environ['BACKUP_SHA'],'supportedCommands':['pg_dump','pg_restore'],'originalDatabaseDestroyed':True,'restoredIntoCleanVolume':True},
 'measurements':{'rpoMs':int(os.environ['RPO_MS']),'rtoMs':int(os.environ['RTO_MS']),'rpoDefinition':'last committed fixture to backup completion','rtoDefinition':'destruction start to restored customer journey green'},
 'verification':{'before':json.loads(os.environ['BEFORE']),'after':json.loads(os.environ['AFTER']),'contentEqual':True,'customerJourney':{'name':'browse service catalog','route':'/rest/services?f=json','responseContract':'HTTP 200 JSON object with services member','status':'pass'}},
 'startedAt':os.environ['STARTED_AT'],'completedAt':os.environ['COMPLETED_AT']}
open(sys.argv[1],'w').write(json.dumps(r,indent=2,sort_keys=True)+'\n')
PY

KEY=${HONUA_DR_SIGNING_KEY:-$OUT/receipt-key.pem}
if [[ ! -f "$KEY" ]]; then openssl genpkey -algorithm ED25519 -out "$KEY"; chmod 600 "$KEY"; fi
openssl pkey -in "$KEY" -pubout -out "$OUT/receipt.pub.pem"
openssl pkeyutl -sign -rawin -inkey "$KEY" -in "$OUT/receipt.json" -out "$OUT/receipt.json.sig"
openssl pkeyutl -verify -rawin -pubin -inkey "$OUT/receipt.pub.pem" -in "$OUT/receipt.json" -sigfile "$OUT/receipt.json.sig"
sha256sum "$OUT/receipt.json" "$OUT/receipt.json.sig" "$OUT/backup.dump" > "$OUT/SHA256SUMS"
printf 'DR drill PASS receipt=%s rpoMs=%s rtoMs=%s\n' "$OUT/receipt.json" "$RPO_MS" "$RTO_MS"

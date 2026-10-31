#!/usr/bin/env bash
# Non-priced real-server smoke: GA seed, typed town setup, probes/readback.
# The caller supplies only an evidence directory; this always creates its own stack.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${OPS_SMOKE_OUT:?OPS_SMOKE_OUT must name a fresh private directory}"
umask 077
mkdir "$OPS_SMOKE_OUT"
export COMPOSE_PROJECT_NAME="ops-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
export E2E_SERVER_PORT="${OPS_SMOKE_PORT:-$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')}"
export E2E_BASE="http://127.0.0.1:$E2E_SERVER_PORT" HONUA_PUBLIC_BASE_URL="http://127.0.0.1:$E2E_SERVER_PORT"
export E2E_API_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export HONUA_SERVER_IMAGE="$(cd "$ROOT" && python3 -c 'from tools.compose_ga_rc1_gate import REPOSITORY, RC1_INDEX_DIGEST; print(REPOSITORY+"@"+RC1_INDEX_DIGEST)')"
export E2E_OUT="$OPS_SMOKE_OUT/seed"
export E2E_COMPOSE_FILE="$ROOT/e2e/harness/compose.candidate.yml"
export E2E_PSQL="docker compose -p $COMPOSE_PROJECT_NAME -f $E2E_COMPOSE_FILE exec -T db psql -U honua -d honua"
mkdir -p "$E2E_OUT"
cleanup() {
  status=$?
  trap - EXIT
  export OPS_SMOKE_CLEANUP=pass
  if ! docker compose -p "$COMPOSE_PROJECT_NAME" -f "$E2E_COMPOSE_FILE" down -v --remove-orphans >/dev/null 2>&1; then
    export OPS_SMOKE_CLEANUP=fail
    echo "trial stack cleanup failed" >&2
    status=1
  fi
  export OPS_SMOKE_EXIT="$status"
  if ! python3 - <<'PY'
import json, os
from pathlib import Path
path = Path(os.environ['OPS_SMOKE_OUT']) / 'smoke-evidence.json'
data = json.loads(path.read_text()) if path.exists() else {'project': os.environ['COMPOSE_PROJECT_NAME'], 'status': 'fail'}
data['cleanup'] = os.environ['OPS_SMOKE_CLEANUP']
data['status'] = 'pass' if data.get('status') == 'operations-pass' and os.environ['OPS_SMOKE_EXIT'] == '0' else 'fail'
path.write_text(json.dumps(data, sort_keys=True) + '\n')
PY
  then
    echo "could not retain smoke cleanup evidence" >&2
    exit 1
  fi
  exit "$status"
}
trap cleanup EXIT
cd "$ROOT"
docker compose -p "$COMPOSE_PROJECT_NAME" -f "$E2E_COMPOSE_FILE" up -d --wait --wait-timeout 180 >"$OPS_SMOKE_OUT/compose.log" 2>&1
bash e2e/harness/seed/seed.sh >"$OPS_SMOKE_OUT/seed.log" 2>&1
python3 certification/agent-ops/real_smoke.py

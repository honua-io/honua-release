#!/usr/bin/env bash
# Deterministic GA Compose gate (honua-release#950, #440).
# Installs the published 2026.1-rc.1 server image, loads the real e2e seed, upgrades to a
# newer trunk build, verifies, rolls back, verifies again, then interrupts a real pending
# migration in the candidate runner. Own compose project and host port (both contain 5).
# Does not stop containers from any other compose project.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
OUT="${E2E_OUT:-$REPO_ROOT/out/compose-ga-rc1}"
PROJECT="honua-ga5-compose-upgrade"
PORT=15085
REGISTRY_NAME="honua-ga5-registry"
REGISTRY_PORT=55000
CHECKOUT="${HONUA_SERVER_CHECKOUT:-}"
mkdir -p "$OUT"

die() { echo "compose-ga-rc1: $*" >&2; exit 2; }

port_free() {
  python3 - "$1" <<'PY'
import socket, sys
sock = socket.socket()
try:
    sock.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
}

scrub_secret() {
  [ -n "${E2E_API_KEY:-}" ] || return 0
  python3 - "$OUT" <<'PY'
import os, sys
from pathlib import Path
secret = os.environ.get("E2E_API_KEY", "")
root = Path(sys.argv[1])
if not secret:
    raise SystemExit(0)
for path in root.rglob("*"):
    if not path.is_file() or path.stat().st_size > 20_000_000:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        continue
    if secret in text:
        path.write_text(text.replace(secret, "[redacted]"), encoding="utf-8")
PY
}

registry_started=0
BUILD_SLOT_FD=""
release_build_slot() {
  if [ -n "${BUILD_SLOT_FD:-}" ]; then
    flock -u "${BUILD_SLOT_FD}" 2>/dev/null || true
    eval "exec ${BUILD_SLOT_FD}>&-" || true
    BUILD_SLOT_FD=""
  fi
}
take_build_slot() {
  local lib="${HONUA_BUILD_SLOT_LIB:-/home/mike/honua-io/honua-flow/tools/lib/build-slot-lib.sh}"
  if [ ! -f "$lib" ]; then
    echo "compose-ga-rc1: no build-slot library; building without a host slot"
    return 0
  fi
  # shellcheck disable=SC1090
  export BUILD_RESERVATION_HELPER="${lib%/*}/build-reservation.py"
  [ -r "$BUILD_RESERVATION_HELPER" ] || die "build slot helper missing; not bypassing a full slot"
  source "$lib"
  export HONUA_BUILD_SLOT_LANE="${HONUA_BUILD_SLOT_LANE:-ga-5}"
  build_slot_take || die "build slot unavailable; not bypassing a full slot"
  echo "compose-ga-rc1: holding build slot ${BUILD_SLOT_NO}"
}
cleanup() {
  local status=$?
  trap - EXIT
  scrub_secret || true
  release_build_slot
  if [ "$registry_started" = 1 ]; then
    docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
command -v gh >/dev/null 2>&1 || die "gh is required to read the 2026.1-rc.1 release"
port_free "$PORT" || die "host port $PORT is in use; refusing to touch another listener"
port_free "$REGISTRY_PORT" || die "registry port $REGISTRY_PORT is in use; refusing to touch another listener"

export COMPOSE_PROJECT_NAME="$PROJECT"
export E2E_SERVER_PORT="$PORT"
export E2E_BASE="http://127.0.0.1:$PORT"
export HONUA_PUBLIC_BASE_URL="$E2E_BASE"
export DOCKER_DEFAULT_PLATFORM="linux/amd64"
export E2E_API_KEY="${E2E_API_KEY:-$(openssl rand -hex 24)}"

gh api repos/honua-io/honua-release/releases/tags/2026.1-rc.1 --jq .body > "$OUT/rc1-release-body.md"
PRIOR_INDEX="$(python3 "$REPO_ROOT/tools/compose_ga_rc1_gate.py" check-release --body-file "$OUT/rc1-release-body.md")"
echo "compose-ga-rc1: release pin accepted"

if [ -z "${GH_TOKEN:-}" ]; then
  GH_TOKEN="$(gh auth token)"
  export GH_TOKEN
fi
export GH_ACTOR="${GH_ACTOR:-$(gh api user --jq .login)}"
printf '%s' "$GH_TOKEN" | docker login ghcr.io -u "$GH_ACTOR" --password-stdin >/dev/null 2>&1 || true
docker pull --platform linux/amd64 "$PRIOR_INDEX"
docker buildx imagetools inspect --raw "$PRIOR_INDEX" > "$OUT/prior-index.json"
python3 "$REPO_ROOT/tools/compose_ga_rc1_gate.py" amd64-child --index "$OUT/prior-index.json" > "$OUT/prior-amd64.txt"
AMD64="$(cat "$OUT/prior-amd64.txt")"
RUNTIME_PRIOR="$PRIOR_INDEX"
platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$PRIOR_INDEX" 2>/dev/null || true)"
if [ "$platform" != linux/amd64 ]; then
  docker pull --platform linux/amd64 "${PRIOR_INDEX%@*}@${AMD64}"
  RUNTIME_PRIOR="${PRIOR_INDEX%@*}@${AMD64}"
  platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$RUNTIME_PRIOR")"
fi
[ "$platform" = linux/amd64 ] || die "rc.1 runtime image is $platform, not linux/amd64"
# cu_identity compares the running container image id to inspect of the ref. An index
# that inspects as linux/amd64 can still start the child. Use the ref whose id matches.
created="$(docker create --platform linux/amd64 "$RUNTIME_PRIOR")" || die "could not create an rc.1 container to read its image id"
running_id="$(docker inspect --format '{{.Image}}' "$created")"
docker rm "$created" >/dev/null
inspected_id="$(docker image inspect --format '{{.Id}}' "$RUNTIME_PRIOR")"
if [ "$running_id" != "$inspected_id" ]; then
  docker pull --platform linux/amd64 "${PRIOR_INDEX%@*}@${AMD64}"
  RUNTIME_PRIOR="${PRIOR_INDEX%@*}@${AMD64}"
  platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$RUNTIME_PRIOR")"
  [ "$platform" = linux/amd64 ] || die "rc.1 amd64 child is $platform, not linux/amd64"
  created="$(docker create --platform linux/amd64 "$RUNTIME_PRIOR")" || die "could not create the rc.1 amd64 child"
  running_id="$(docker inspect --format '{{.Image}}' "$created")"
  docker rm "$created" >/dev/null
  inspected_id="$(docker image inspect --format '{{.Id}}' "$RUNTIME_PRIOR")"
  [ "$running_id" = "$inspected_id" ] || die "rc.1 container image id does not match the runtime ref"
fi
echo "compose-ga-rc1: runtime prior identity matches"

prove_ancestor() {
  local sha="$1"
  if [ -n "$CHECKOUT" ] && git -C "$CHECKOUT" cat-file -e "${sha}^{commit}" 2>/dev/null \
      && git -C "$CHECKOUT" cat-file -e "a4bde4b8c5ee8cc3dec05ad8b3d4f84470c1ea6e^{commit}" 2>/dev/null \
      && git -C "$CHECKOUT" merge-base --is-ancestor a4bde4b8c5ee8cc3dec05ad8b3d4f84470c1ea6e "$sha"; then
    return 0
  fi
  local status
  status="$(gh api "repos/honua-io/honua-server/compare/a4bde4b8c5ee8cc3dec05ad8b3d4f84470c1ea6e...${sha}" --jq .status)"
  [ "$status" = ahead ]
}

schema_of() {
  find "$1/src/Honua.Server/Migrations" -name '[0-9][0-9][0-9]_*.sql' -printf '%f\n' \
    | sed -n 's/^\([0-9][0-9][0-9]\)_.*/\1/p' | sort -n | tail -1
}

if [ -n "${CANDIDATE_IMAGE:-}" ]; then
  [ -n "${CANDIDATE_SHA:-}" ] && [ -n "${CANDIDATE_SCHEMA:-}" ] || die "CANDIDATE_SHA and CANDIDATE_SCHEMA are required with CANDIDATE_IMAGE"
  CANDIDATE="$CANDIDATE_IMAGE"
  CANDIDATE_SHA="$CANDIDATE_SHA"
  CANDIDATE_SCHEMA="$CANDIDATE_SCHEMA"
else
  [ -n "$CHECKOUT" ] && [ -d "$CHECKOUT/src/Honua.Server" ] || die "set CANDIDATE_IMAGE or HONUA_SERVER_CHECKOUT to a honua-server tree"
  CANDIDATE_SHA="$(git -C "$CHECKOUT" rev-parse HEAD)"
  CANDIDATE_SCHEMA="$(schema_of "$CHECKOUT")"
  [ -n "$CANDIDATE_SCHEMA" ] || die "could not read a migration number from the candidate tree"
  docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
  docker run -d --name "$REGISTRY_NAME" -p "127.0.0.1:${REGISTRY_PORT}:5000" registry:2 >/dev/null
  registry_started=1
  take_build_slot
  docker build --progress=plain --platform linux/amd64 \
    --build-arg "HONUA_GIT_SHA=$CANDIDATE_SHA" \
    --secret id=github_token,env=GH_TOKEN \
    --secret id=github_actor,env=GH_ACTOR \
    -f "$CHECKOUT/Dockerfile" \
    -t "127.0.0.1:${REGISTRY_PORT}/honua-server:ga5-trunk" \
    "$CHECKOUT"
  docker push "127.0.0.1:${REGISTRY_PORT}/honua-server:ga5-trunk" > "$OUT/candidate-push.log"
  digest="$(sed -n 's/.*\(sha256:[0-9a-f]\{64\}\).*/\1/p' "$OUT/candidate-push.log" | tail -1)"
  [ -n "$digest" ] || die "local registry push did not report a digest"
  CANDIDATE="127.0.0.1:${REGISTRY_PORT}/honua-server@${digest}"
  release_build_slot
fi

prove_ancestor "$CANDIDATE_SHA" || die "candidate $CANDIDATE_SHA is not a descendant of the rc.1 server commit"
python3 "$REPO_ROOT/tools/compose_ga_rc1_gate.py" check-candidate \
  --image "$CANDIDATE" --sha "$CANDIDATE_SHA" --schema "$CANDIDATE_SCHEMA" --ancestor yes >/dev/null
docker pull --platform linux/amd64 "$CANDIDATE"

export HONUA_PRIOR_SERVER_IMAGE="$RUNTIME_PRIOR"
export HONUA_CANDIDATE_SERVER_IMAGE="$CANDIDATE"
export HONUA_CANDIDATE_DB_SCHEMA="$CANDIDATE_SCHEMA"

run_scenario() {
  local name="$1" scenario="$2"
  local dir="$OUT/$name"
  mkdir -p "$dir"
  echo "compose-ga-rc1: $name ($scenario)"
  set +e
  CHAOS_SCENARIO="$scenario" E2E_OUT="$dir" bash "$HERE/upgrade-chaos.sh" > "$dir/driver.log" 2>&1
  local rc=$?
  set -e
  echo "compose-ga-rc1: $name exit $rc"
  return "$rc"
}

upgrade_rc=0
interrupt_rc=0
run_scenario upgrade compose-upgrade-rollback || upgrade_rc=$?
run_scenario interrupt partial-migration-failure || interrupt_rc=$?

receipt_rc=0
[ -f "$OUT/upgrade/scenario-matrix.json" ] || printf '%s\n' '{"scenarios":[]}' > "$OUT/upgrade/scenario-matrix.json"
[ -f "$OUT/interrupt/scenario-matrix.json" ] || printf '%s\n' '{"scenarios":[]}' > "$OUT/interrupt/scenario-matrix.json"
[ -f "$OUT/upgrade/compose-observed.json" ] || printf '%s\n' '{}' > "$OUT/upgrade/compose-observed.json"
python3 "$REPO_ROOT/tools/compose_ga_rc1_gate.py" receipt \
  --upgrade-matrix "$OUT/upgrade/scenario-matrix.json" \
  --interrupt-matrix "$OUT/interrupt/scenario-matrix.json" \
  --observed "$OUT/upgrade/compose-observed.json" \
  --candidate-image "$CANDIDATE" --candidate-sha "$CANDIDATE_SHA" \
  --candidate-schema "$CANDIDATE_SCHEMA" --ancestor yes \
  --runtime-prior "$RUNTIME_PRIOR" \
  --run "${GATE_RUN_URL:-local}" --commit "${GATE_COMMIT:-$(git -C "$REPO_ROOT" rev-parse HEAD)}" \
  --output "$OUT/compose-ga-rc1-receipt.json" || receipt_rc=$?
echo "compose-ga-rc1: receipt $OUT/compose-ga-rc1-receipt.json (upgrade $upgrade_rc, interrupt $interrupt_rc, receipt $receipt_rc)"
[ "$upgrade_rc" = 0 ] && [ "$interrupt_rc" = 0 ] && [ "$receipt_rc" = 0 ]

#!/usr/bin/env bash
# Build a DEVELOPMENT candidate image of honua-server from an exact trunk commit on the eval hosts,
# for arc rehearsals while no published trunk image exists. Never a release candidate: the lock it
# writes has schema honua.ai-arc.dev-candidate/v1, and every receipt and aggregate made against it is
# marked development / not certifying.
#
#   tools/ai_arc_dev_candidate.sh <honua-server checkout> <40-hex trunk sha> <out-lock.json> host1 [host2 ...]
#
# The image is built once on the first host (DOCKER_HOST=ssh://host1), labelled with the source
# revision, tagged honua-arc-dev/honua-server:<sha12>, and copied byte-for-byte to the other hosts
# (docker save | docker load), so every host runs the same image id. The checkout must be clean at
# that commit; the build context is exactly `git archive <sha>`. The Dockerfile is the production
# serving one (docker/Dockerfile.aot) unless HONUA_DOCKERFILE names another path in the archive.
set -euo pipefail

src=${1:?honua-server checkout} sha=${2:?trunk sha} out=${3:?output lock}
shift 3
[ "$#" -ge 1 ] || { echo "at least one eval host" >&2; exit 2; }
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "sha must be 40 hex" >&2; exit 2; }
git -C "$src" cat-file -e "$sha^{commit}"
tag="honua-arc-dev/honua-server:${sha:0:12}"
first=$1

# The build client runs in the background: it has been seen to hang after the remote build finished.
# The first host is polled for the tag carrying this revision label; as soon as it exists the client
# is no longer needed. A client that fails before the image appears, or the timeout, fails the build.
timeout_s=${HONUA_DEV_BUILD_TIMEOUT:-7200}
poll_s=${HONUA_DEV_BUILD_POLL:-15}
log=$(mktemp)
( git -C "$src" archive --format=tar "$sha" |
    DOCKER_HOST="ssh://$first" docker build --quiet \
      --label "org.opencontainers.image.revision=$sha" \
      --label "org.opencontainers.image.source=https://github.com/honua-io/honua-server" \
      --label "io.honua.arc.development=true" \
      -f "${HONUA_DOCKERFILE:-docker/Dockerfile.aot}" -t "$tag" - >"$log" 2>&1 ) &
client=$!
built() {
  [ "$(DOCKER_HOST="ssh://$first" docker image inspect \
        --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$tag" 2>/dev/null)" = "$sha" ]
}
deadline=$(( $(date +%s) + timeout_s ))
while :; do
  if built; then
    kill "$client" 2>/dev/null || true
    wait "$client" 2>/dev/null || true
    break
  fi
  if ! kill -0 "$client" 2>/dev/null; then
    if wait "$client"; then status=0; else status=$?; fi
    built && break
    echo "docker build exited $status before the image appeared" >&2
    tail -20 "$log" >&2
    exit 1
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    kill "$client" 2>/dev/null || true
    echo "the build did not produce $tag within ${timeout_s}s" >&2
    exit 1
  fi
  sleep "$poll_s"
done
rm -f "$log"

image_id=$(DOCKER_HOST="ssh://$first" docker image inspect --format '{{.Id}}' "$tag")
for host in "${@:2}"; do
  if ssh -o BatchMode=yes "$first" 'command -v zstd' >/dev/null 2>&1 &&
     ssh -o BatchMode=yes "$host" 'command -v zstd' >/dev/null 2>&1; then
    ssh -o BatchMode=yes "$first" "docker save $tag | zstd -1 -T0 -q" |
      ssh -o BatchMode=yes "$host" 'zstd -d -q | docker load -q' >/dev/null
  else
    DOCKER_HOST="ssh://$first" docker save "$tag" | DOCKER_HOST="ssh://$host" docker load --quiet >/dev/null
  fi
  copied=$(DOCKER_HOST="ssh://$host" docker image inspect --format '{{.Id}}' "$tag")
  [ "$copied" = "$image_id" ] || { echo "image id differs on $host" >&2; exit 1; }
done
for host in "$@"; do
  label=$(DOCKER_HOST="ssh://$host" docker image inspect \
          --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$tag")
  [ "$label" = "$sha" ] || { echo "revision label differs on $host" >&2; exit 1; }
done

python3 - "$out" "$sha" "$image_id" "$tag" "$@" <<'PY'
import json, sys
out, sha, image_id, tag, *hosts = sys.argv[1:]
json.dump({'schema': 'honua.ai-arc.dev-candidate/v1', 'development': True,
           'note': 'development image built from honua-server trunk on the eval hosts; not a release candidate',
           'hosts': hosts,
           'components': {'honua-server': {'source': {'repo': 'honua-io/honua-server', 'revision': sha},
                                           'artifacts': [{'kind': 'image', 'digest': image_id, 'reference': tag,
                                                          'local': True}]}}},
          open(out, 'w'), indent=2, sort_keys=True)
PY
echo "$out: $tag $image_id"

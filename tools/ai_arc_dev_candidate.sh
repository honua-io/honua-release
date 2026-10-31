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
# that commit; the build context is exactly `git archive <sha>`.
set -euo pipefail

src=${1:?honua-server checkout} sha=${2:?trunk sha} out=${3:?output lock}
shift 3
[ "$#" -ge 1 ] || { echo "at least one eval host" >&2; exit 2; }
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "sha must be 40 hex" >&2; exit 2; }
git -C "$src" cat-file -e "$sha^{commit}"
tag="honua-arc-dev/honua-server:${sha:0:12}"
first=$1

git -C "$src" archive --format=tar "$sha" |
  DOCKER_HOST="ssh://$first" docker build --quiet \
    --label "org.opencontainers.image.revision=$sha" \
    --label "org.opencontainers.image.source=https://github.com/honua-io/honua-server" \
    --label "io.honua.arc.development=true" \
    -t "$tag" - >/dev/null

image_id=$(DOCKER_HOST="ssh://$first" docker image inspect --format '{{.Id}}' "$tag")
for host in "${@:2}"; do
  DOCKER_HOST="ssh://$first" docker save "$tag" | DOCKER_HOST="ssh://$host" docker load --quiet >/dev/null
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

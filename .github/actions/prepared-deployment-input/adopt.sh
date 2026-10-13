#!/bin/bash
set -euo pipefail
test -z "${CANDIDATE_REF:-}" || { echo 'prepared_ref and candidate_ref are mutually exclusive'; exit 1; }

[[ "$PREPARED_REF" =~ ^[0-9a-f]{40}$ ]]
cd "${CHECKOUT_DIRECTORY:-.}"
checkout="$(pwd -P)"
test "$(git rev-parse --show-toplevel)" = "$checkout"
case "$(git config --get remote.origin.url)" in
  https://github.com/honua-io/honua-release|https://github.com/honua-io/honua-release.git|git@github.com:honua-io/honua-release.git) ;;
  *) echo 'prepared deployment inputs require the canonical honua-release origin'; exit 1 ;;
esac

fetch_trunk() {
  if [ "$(git rev-parse --is-shallow-repository)" = true ]; then
    git fetch --no-tags --unshallow origin "+refs/heads/trunk:refs/remotes/origin/trunk"
  else
    git fetch --no-tags origin "+refs/heads/trunk:refs/remotes/origin/trunk"
  fi
}

cross_repository=false
if [ "${RUN_REPOSITORY:-honua-io/honua-release}" != honua-io/honua-release ] && [ -z "${REVIEWED_RELEASE_SHA:-}" ]; then
  echo 'cross-repository prepared deployment inputs require reviewed_release_sha'; exit 1
fi
if [ -n "${REVIEWED_RELEASE_SHA:-}" ]; then
  [[ "$REVIEWED_RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]
  test "$(git rev-parse HEAD)" = "$REVIEWED_RELEASE_SHA"
  fetch_trunk
  git merge-base --is-ancestor "$REVIEWED_RELEASE_SHA" refs/remotes/origin/trunk || {
    echo 'release checkout is not reviewed canonical trunk history'; exit 1;
  }
  RUN_SHA="$REVIEWED_RELEASE_SHA"
  cross_repository=true
fi

git fetch --depth=2 origin "refs/prepared-deployments/${PREPARED_REF}:refs/prepared-deployments/${PREPARED_REF}"
test "$(git rev-parse "refs/prepared-deployments/${PREPARED_REF}")" = "$PREPARED_REF"
snapshot="$(mktemp -d "${RUNNER_TEMP:-/tmp}/honua-prepared-input.XXXXXX")"
rmdir "$snapshot"
changes="$(mktemp "${RUNNER_TEMP:-/tmp}/honua-prepared-changes.XXXXXX")"
trap 'rm -f "$changes"; git worktree remove --force "$snapshot" >/dev/null 2>&1 || true' EXIT
git worktree add --detach "$snapshot" "$PREPARED_REF"
test "$(git -C "$snapshot" rev-parse HEAD)" = "$PREPARED_REF"
parent="$(git -C "$snapshot" rev-parse HEAD^)"
test "$(git -C "$snapshot" rev-list --parents -n 1 HEAD | wc -w)" -eq 2 || {
  echo 'prepared deployment snapshot must have exactly one parent'; exit 1;
}
if [ "$parent" != "$RUN_SHA" ]; then
  if [ "$cross_repository" != true ] && [ "$RUN_REF" != refs/heads/trunk ]; then
    echo "snapshot parent $parent is not this run's commit $RUN_SHA"; exit 1
  fi
  fetch_trunk
  git merge-base --is-ancestor "$parent" "$RUN_SHA" || {
    echo "snapshot parent $parent is not reviewed trunk history of $RUN_SHA"; exit 1;
  }
fi

# A preparation snapshot carries only inputs; it cannot inject certification or executable code.
git -C "$snapshot" diff --no-renames --name-only -z HEAD^ HEAD > "$changes"
while IFS= read -r -d '' file; do
  case "$file" in
    platform-manifest.yaml|compatibility-matrix.yaml|deployment-preparation.json) ;;
    *) echo "unexpected prepared deployment change: $file"; exit 1 ;;
  esac
done < "$changes"
for file in platform-manifest.yaml compatibility-matrix.yaml deployment-preparation.json; do
  if [ ! -f "$snapshot/$file" ] || [ -L "$snapshot/$file" ]; then
    echo "prepared deployment input must be a regular file: $file"; exit 1
  fi
done
python3 - "$snapshot/deployment-preparation.json" <<'MARKER'
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    marker = json.load(stream)
if (marker.get("schema") != "honua.deployment-preparation/v1"
        or marker.get("status") != "unadmitted"
        or marker.get("candidateQualified") is not False):
    raise SystemExit("prepared deployment marker must remain explicitly unadmitted")
MARKER
for file in platform-manifest.yaml compatibility-matrix.yaml deployment-preparation.json; do
  cp "$snapshot/$file" "$file"
done
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "manifest_path=$checkout/platform-manifest.yaml"
    echo "matrix_path=$checkout/compatibility-matrix.yaml"
    echo "manifest_sha256=$(sha256sum platform-manifest.yaml | cut -d ' ' -f 1)"
    echo "matrix_sha256=$(sha256sum compatibility-matrix.yaml | cut -d ' ' -f 1)"
  } >> "$GITHUB_OUTPUT"
fi

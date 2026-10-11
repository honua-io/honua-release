#!/bin/bash
set -euo pipefail

[[ "$CANDIDATE_REF" =~ ^[0-9a-f]{40}$ ]]
cd "${CHECKOUT_DIRECTORY:-.}"
checkout="$(pwd -P)"
test "$(git rev-parse --show-toplevel)" = "$checkout"
case "$(git config --get remote.origin.url)" in
  https://github.com/honua-io/honua-release|https://github.com/honua-io/honua-release.git|git@github.com:honua-io/honua-release.git) ;;
  *) echo 'candidate inputs require the canonical honua-release origin'; exit 1 ;;
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
  echo 'cross-repository candidate inputs require reviewed_release_sha'; exit 1
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

git fetch --depth=2 origin "refs/nightly-candidates/${CANDIDATE_REF}:refs/nightly-candidates/${CANDIDATE_REF}"
test "$(git rev-parse "refs/nightly-candidates/${CANDIDATE_REF}")" = "$CANDIDATE_REF"
snapshot="$(mktemp -d "${RUNNER_TEMP:-/tmp}/honua-nightly-input.XXXXXX")"
rmdir "$snapshot"
changes="$(mktemp "${RUNNER_TEMP:-/tmp}/honua-candidate-changes.XXXXXX")"
trap 'rm -f "$changes"; git worktree remove --force "$snapshot" >/dev/null 2>&1 || true' EXIT
git worktree add --detach "$snapshot" "$CANDIDATE_REF"
test "$(git -C "$snapshot" rev-parse HEAD)" = "$CANDIDATE_REF"
parent="$(git -C "$snapshot" rev-parse HEAD^)"
test "$(git -C "$snapshot" rev-list --parents -n 1 HEAD | wc -w)" -eq 2 || {
  echo 'candidate snapshot must have exactly one parent'; exit 1;
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

# Preserve the train's data-only allowlist, including its vendored certification inputs.
git -C "$snapshot" diff --no-renames --name-only -z HEAD^ HEAD > "$changes"
while IFS= read -r -d '' file; do
  case "$file" in
    platform-manifest.yaml|compatibility-matrix.yaml) ;;
    certification/protocol-certification-requirements.v1.json|certification/sources/*) ;;
    *) echo "unexpected candidate change: $file"; exit 1 ;;
  esac
done < "$changes"
for file in platform-manifest.yaml compatibility-matrix.yaml; do
  if [ ! -f "$snapshot/$file" ] || [ -L "$snapshot/$file" ]; then
    echo "candidate input must be a regular file: $file"; exit 1
  fi
done
cp "$snapshot/platform-manifest.yaml" platform-manifest.yaml
cp "$snapshot/compatibility-matrix.yaml" compatibility-matrix.yaml
if [ -f "$snapshot/certification/protocol-certification-requirements.v1.json" ]; then
  cp "$snapshot/certification/protocol-certification-requirements.v1.json" certification/protocol-certification-requirements.v1.json
  rm -rf certification/sources
  cp -a "$snapshot/certification/sources" certification/sources
fi
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "manifest_path=$checkout/platform-manifest.yaml"
    echo "matrix_path=$checkout/compatibility-matrix.yaml"
    echo "manifest_sha256=$(sha256sum platform-manifest.yaml | cut -d ' ' -f 1)"
    echo "matrix_sha256=$(sha256sum compatibility-matrix.yaml | cut -d ' ' -f 1)"
  } >> "$GITHUB_OUTPUT"
fi

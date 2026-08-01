#!/usr/bin/env bash
# Fetch one component at the exact full SHA frozen in platform-manifest.yaml.
set -euo pipefail

repo="${1:?repository name required}"
sha="${2:?full commit SHA required}"
dest="${3:?destination required}"

if [[ ! "$repo" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "invalid repository name: $repo" >&2
  exit 2
fi
if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "invalid full commit SHA for $repo: $sha" >&2
  exit 2
fi
if [ -e "$dest" ]; then
  echo "checkout destination already exists: $dest" >&2
  exit 2
fi

git init -q "$dest"
remote="https://github.com/honua-io/${repo}.git"
if [ -n "${GH_TOKEN:-}" ]; then
  remote="https://x-access-token:${GH_TOKEN}@github.com/honua-io/${repo}.git"
fi
git -C "$dest" remote add origin "$remote"
git -C "$dest" fetch --no-tags --depth 1 origin "$sha"
git -C "$dest" checkout -q --detach FETCH_HEAD
# Do not retain the credential-bearing URL in the working checkout.
git -C "$dest" remote set-url origin "https://github.com/honua-io/${repo}.git"

actual="$(git -C "$dest" rev-parse HEAD)"
if [ "$actual" != "$sha" ]; then
  echo "exact-SHA checkout mismatch for $repo: wanted $sha, got $actual" >&2
  exit 1
fi
echo "$repo exact checkout: $actual"

#!/usr/bin/env bash
# Install the pinned gitsign release (Sigstore keyless Git signing) for #236 / founder decision D11.
#
# The binary is downloaded from the sigstore/gitsign GitHub release and must match the SHA-256
# pinned below (copied from that release's checksums.txt). A mismatch, an unknown platform or a
# failed download exits non-zero; nothing is installed unverified.
#
# Usage: tools/install_gitsign.sh <install-dir>
#   Installs <install-dir>/gitsign and, on GitHub Actions, adds <install-dir> to GITHUB_PATH.
set -euo pipefail

VERSION="0.17.1"
declare -A SHA256=(
  [linux_amd64]=69213a8a0813a151e5a47d0060862952ff833a845d57309dff76f7ba6600abae
  [linux_arm64]=477018736a80b36e703dd58db8d6e158a2c1b8b727af0ab8ffdcce9fdf610ada
  [darwin_amd64]=32c469eee2859694a67df0efee1a17f2574600ad73769a81315f360ee75f89e7
  [darwin_arm64]=4994e9629aae6a3fea9c0f770f279b273bad3e86ac8ebd3e9dc90f317428589d
)

dest="${1:?usage: install_gitsign.sh <install-dir>}"
os="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$(uname -m)" in
  x86_64|amd64) arch=amd64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo "install_gitsign: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac
platform="${os}_${arch}"
expected="${SHA256[$platform]:-}"
[ -n "$expected" ] || { echo "install_gitsign: no pinned checksum for $platform" >&2; exit 1; }

mkdir -p "$dest"
tmp="$(mktemp "$dest/.gitsign.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
curl -fsSL --retry 3 -o "$tmp" \
  "https://github.com/sigstore/gitsign/releases/download/v${VERSION}/gitsign_${VERSION}_${platform}"
actual="$(sha256sum "$tmp" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$tmp" | cut -d' ' -f1)"
if [ "$actual" != "$expected" ]; then
  echo "install_gitsign: gitsign ${VERSION} ${platform} checksum $actual != pinned $expected" >&2
  exit 1
fi
chmod 0755 "$tmp"
mv "$tmp" "$dest/gitsign"
trap - EXIT
if [ -n "${GITHUB_PATH:-}" ]; then
  echo "$dest" >> "$GITHUB_PATH"
fi
"$dest/gitsign" --version

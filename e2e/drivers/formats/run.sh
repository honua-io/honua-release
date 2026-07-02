#!/usr/bin/env bash
# S8 wrapper — runs the per-format coverage matrix (Python, stdlib only). Emits formatCoverage[].
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/formats.py"
